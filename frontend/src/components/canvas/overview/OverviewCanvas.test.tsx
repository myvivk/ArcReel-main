import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

import { API } from "@/api";
import { LeaveGuardProvider } from "@/components/shared/edit-unit/LeaveGuard";
import { useAppStore } from "@/stores/app-store";
import { useCostStore } from "@/stores/cost-store";
import { useProjectsStore } from "@/stores/projects-store";
import type { CostEstimateResponse, ProjectData } from "@/types";

import { OverviewCanvas } from "./OverviewCanvas";

function makeProjectData(overrides: Partial<ProjectData> = {}): ProjectData {
  return {
    title: "Demo",
    content_mode: "drama",
    style: "Anime",
    aspect_ratio: "9:16",
    overview: {
      synopsis: "summary",
      genre: "fantasy",
      theme: "growth",
      world_setting: "palace",
    },
    episodes: [
      { episode: 1, title: "EP1", script_file: "scripts/episode_1.json", duration_seconds: 50 },
      { episode: 2, title: "EP2", script_file: "scripts/episode_2.json", duration_seconds: 40 },
    ],
    characters: {},
    scenes: {},
    props: {},
    ...overrides,
  };
}


function withRouter(children: ReactNode, location = memoryLocation({ path: "/", record: true })) {
  return (
    <Router hook={location.hook}>
      <LeaveGuardProvider>{children}</LeaveGuardProvider>
    </Router>
  );
}

function renderOverview(props: Partial<Parameters<typeof OverviewCanvas>[0]> = {}) {
  const location = memoryLocation({ path: "/", record: true });
  const element = (next: Partial<Parameters<typeof OverviewCanvas>[0]>) =>
    withRouter(<OverviewCanvas projectName="demo" projectData={makeProjectData()} {...next} />, location);
  const view = render(element(props));
  return { ...view, location, rerender: (next: Partial<Parameters<typeof OverviewCanvas>[0]>) => view.rerender(element(next)) };
}


function setCost(
  projectName: string,
  actual: Record<string, Record<string, number>>,
  overrides: Partial<CostEstimateResponse> = {},
) {
  useCostStore.setState({
    costData: {
      project_name: projectName,
      models: { image: { provider: "p", model: "m" }, video: { provider: "p", model: "m" } },
      episodes: [],
      project_totals: { estimate: { image: { USD: 2 }, video: { USD: 3 } }, actual },
      unpriced: { estimate: [], actual: [] },
      missing_local_calls: false,
      ...overrides,
    },
  });
}

describe("OverviewCanvas", () => {
  beforeEach(() => {
    useAppStore.setState(useAppStore.getInitialState(), true);
    useProjectsStore.setState(useProjectsStore.getInitialState(), true);
    useCostStore.setState(useCostStore.getInitialState(), true);
    vi.restoreAllMocks();
    vi.spyOn(API, "getProject").mockResolvedValue({ project: makeProjectData(), scripts: {} });
  });

  it("summarizes mode, aspect ratio, episode count and script length in the header", () => {
    renderOverview();

    expect(screen.getByRole("heading", { level: 1, name: "Demo" })).toBeInTheDocument();
    expect(screen.getByText("剧情演绎 · 竖屏 9:16 · 2 集 · 脚本时长 1:30")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "项目设置" })).toHaveAttribute("href", "/app/projects/demo/settings");
  });

  it("rounds a fractional script length before splitting it into minutes and seconds", () => {
    renderOverview({
      projectData: makeProjectData({
        episodes: [
          { episode: 1, title: "EP1", script_file: "scripts/episode_1.json", duration_seconds: 59.6 },
          { episode: 2, title: "EP2", script_file: "scripts/episode_2.json", duration_seconds: 60 },
        ],
      }),
    });

    expect(screen.getByText("剧情演绎 · 竖屏 9:16 · 2 集 · 脚本时长 2:00")).toBeInTheDocument();
  });

  it("links each asset kind to its gallery and appends the outdated count", () => {
    renderOverview({
      projectData: makeProjectData({
        status: {
          needs_repair: false,
          repair_reason: null,
          source_remaining: false,
          episodes_summary: {} as never,
          assets: {
            character: { total: 11, available: 11, stale: 2 },
            scene: { total: 16, available: 9, stale: 0 },
            prop: { total: 13, available: 13, stale: 0 },
            product: { total: 1, available: 1, stale: 0 },
          },
        },
      }),
    });

    expect(screen.getByRole("link", { name: "角色 11/11 · 2 过期" })).toHaveAttribute("href", "/characters");
    expect(screen.getByRole("link", { name: "场景 9/16" })).toHaveAttribute("href", "/scenes");
    expect(screen.getByRole("link", { name: "道具 13/13" })).toHaveAttribute("href", "/props");
    // 商品只属于广告项目
    expect(screen.queryByRole("link", { name: /商品/ })).not.toBeInTheDocument();
  });

  it("shows estimate and spend on one line and breaks them down in the details popover", async () => {
    const user = userEvent.setup();
    setCost("demo", { video: { USD: 1 }, unassigned: { USD: 1.25 } });
    renderOverview();

    expect(screen.getByText("预估 $5.00")).toBeInTheDocument();
    expect(screen.getByText("已花 $2.25")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "明细" }));
    const popover = await screen.findByRole("dialog", { name: "费用明细" });
    expect(within(popover).getByText("历史支出（未归属当前脚本）")).toBeInTheDocument();
    expect(within(popover).getByRole("link", { name: "查看使用记录" })).toHaveAttribute(
      "href",
      "/app/settings?section=usage&u_project=demo",
    );
  });

  it("shows a dash instead of another project's cost left in the store", () => {
    setCost("another-project", { video: { USD: 9 } });
    renderOverview();

    expect(screen.getByText("预估 —")).toBeInTheDocument();
    expect(screen.getByText("已花 —")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "明细" })).not.toBeInTheDocument();
  });

  it("shows a real zero spend as 0 but an unrecorded spend as a dash with the reason", () => {
    setCost("demo", {});
    const view = renderOverview();
    expect(screen.getByText("已花 0")).toBeInTheDocument();

    setCost("demo", {}, { missing_local_calls: true });
    view.rerender({});
    expect(screen.getByText("已花 —")).toBeInTheDocument();
    expect(screen.getByText(/本机没有这个项目的生成记录/)).toBeInTheDocument();
  });

  it("explains unpriced estimates and calls and links each custom model to its price", () => {
    const relayImage = { call_type: "image", provider: "custom-3", provider_name: "Relay", model: "img" } as const;
    setCost(
      "demo",
      { video: { USD: 1 } },
      {
        project_totals: { estimate: {}, actual: { video: { USD: 1 } } },
        unpriced: { estimate: [{ ...relayImage, count: 2 }], actual: [{ ...relayImage, count: 1 }] },
      },
    );
    renderOverview();

    // 全部预估都没有价格时写「—」；部分调用没有价格时仍显示已计价的金额
    expect(screen.getByText("预估 —")).toBeInTheDocument();
    expect(screen.getByText("已花 $1.00")).toBeInTheDocument();
    expect(screen.getByText("有 2 项预估的模型没有价格，未计入预估。")).toBeInTheDocument();
    expect(screen.getByText("有 1 次调用的模型没有价格，未计入已花。")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Relay / img" })).toHaveAttribute(
      "href",
      "/app/settings?section=providers&custom=3&model=img",
    );
  });

  it("marks only the unpriced media type as unknown in the details popover", async () => {
    const user = userEvent.setup();
    const relay = { provider: "custom-3", provider_name: "Relay", count: 1 } as const;
    setCost(
      "demo",
      {},
      {
        project_totals: { estimate: { image: { USD: 2 } }, actual: { image: { USD: 4 } } },
        unpriced: {
          estimate: [{ ...relay, call_type: "audio", model: "tts" }],
          actual: [
            { ...relay, call_type: "image", model: "img" },
            { ...relay, call_type: "video", model: "vid" },
          ],
        },
      },
    );
    renderOverview();

    await user.click(screen.getByRole("button", { name: "明细" }));
    const popover = await screen.findByRole("dialog", { name: "费用明细" });
    // 按预估、已花的列序取同名行的值；某列没有这一行时不出现
    const values = (label: string) =>
      within(popover)
        .getAllByText(label, { selector: "dt" })
        .map((term) => term.nextElementSibling?.textContent);

    // 同一类型部分没计价时仍显示已计价的金额
    expect(values("分镜")).toEqual(["$2.00", "$4.00"]);
    expect(values("视频")).toEqual(["0", "—"]);
    // 只有没计价的配音时也要列出这一行，不能让它从明细里消失
    expect(values("旁白配音")).toEqual(["—"]);
  });

  it("does not fetch cost on a read-only project and cancels a real project's queued request", async () => {
    vi.useFakeTimers();
    const getCostEstimate = vi.spyOn(API, "getCostEstimate");
    try {
      const view = renderOverview({ projectName: "real-project" });
      view.rerender({ projectName: "onboarding_demo", readOnly: true });
      await vi.advanceTimersByTimeAsync(600);

      expect(getCostEstimate).not.toHaveBeenCalled();
      expect(useCostStore.getState().costData).toBeNull();
      expect(screen.getByText(/演示项目不在本机生成/)).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

});
