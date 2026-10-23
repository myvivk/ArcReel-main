// 引导开启时预取设置页 chunk：设置页承载引导锚点（settingsProviders、settingsAgent），
// 懒加载后锚点要等 chunk 到达才出现，引导的锚点等待有上限，只能靠提前拉取兜住。
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

const systemConfigPageLoaded = vi.hoisted(() => vi.fn());
const preloaded = vi.hoisted(() => vi.fn());

// 预取的决定在 effect 里同步发出：包一层 preload 记下调用，不必等 chunk 异步求值才能断言「没有预取」
vi.mock("@/utils/lazy-component", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/utils/lazy-component")>();
  return {
    lazyNamed: ((...args: Parameters<typeof actual.lazyNamed>) => {
      const component = actual.lazyNamed(...args);
      const preload = component.preload;
      component.preload = () => {
        preloaded(args[1]);
        preload();
      };
      return component;
    }) as typeof actual.lazyNamed,
  };
});

vi.mock("@/components/pages/SystemConfigPage", () => {
  // 模块求值即代表设置页 chunk 已被拉取
  systemConfigPageLoaded();
  return { SystemConfigPage: () => <div data-testid="system-config-page" /> };
});

vi.mock("@/components/pages/ProjectsPage", () => ({
  ProjectsPage: () => <div data-testid="projects-page" />,
}));

vi.mock("@/onboarding/OnboardingTour", () => ({
  OnboardingTour: () => null,
}));

let modules: [
  typeof import("@/router"),
  typeof import("@/stores/auth-store"),
  typeof import("@/stores/config-status-store"),
  typeof import("@/stores/onboarding-store"),
];

function renderProjectsWithTour(active: boolean) {
  const [{ AppRoutes }, { useAuthStore }, { useConfigStatusStore }, { useOnboardingStore }] = modules;
  useAuthStore.setState({ isAuthenticated: true, isLoading: false });
  // 让 ConfigStatusLoader 的 fetch() 短路，避免触发未 mock 的接口
  useConfigStatusStore.setState({ initialized: true });
  useOnboardingStore.setState({ active });

  const { hook } = memoryLocation({ path: "/app/projects" });
  render(
    <Router hook={hook}>
      <AppRoutes />
    </Router>,
  );
  return screen.findByTestId("projects-page");
}

describe("onboarding chunk preload", () => {
  beforeEach(async () => {
    vi.resetModules();
    systemConfigPageLoaded.mockClear();
    preloaded.mockClear();
    // 每个用例从空请求缓存开始；模块装载属于准备步骤，预取行为在渲染后观察。
    modules = await Promise.all([
      import("@/router"),
      import("@/stores/auth-store"),
      import("@/stores/config-status-store"),
      import("@/stores/onboarding-store"),
    ]);
  });

  it("fetches the settings page chunk while the tour is active, before the user opens settings", async () => {
    await renderProjectsWithTour(true);

    expect(preloaded).toHaveBeenCalledWith("SystemConfigPage");
    await vi.waitFor(() => expect(systemConfigPageLoaded).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("system-config-page")).not.toBeInTheDocument();
  });

  it("leaves the settings page chunk alone when the tour is inactive", async () => {
    await renderProjectsWithTour(false);

    expect(preloaded).not.toHaveBeenCalled();
    expect(systemConfigPageLoaded).not.toHaveBeenCalled();
  });
});
