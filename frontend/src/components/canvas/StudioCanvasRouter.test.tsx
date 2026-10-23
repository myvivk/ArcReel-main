import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { API } from "@/api";
import { useAppStore } from "@/stores/app-store";
import { useConfigStatusStore } from "@/stores/config-status-store";
import { useProjectsStore } from "@/stores/projects-store";
import { selectActiveResourceIds, useTasksStore } from "@/stores/tasks-store";
import { StudioCanvasRouter } from "@/components/canvas/StudioCanvasRouter";
import { episodeEditViewPath } from "@/app-routes";
import { DEMO_PROJECT_NAME } from "@/onboarding/demo-project";
import type { DramaEpisodeScript, EpisodeScript, ProjectData } from "@/types";

// 面板自身的行为在 WorkflowPanel.test.tsx 覆盖；这里只关心路由层递给它什么回调。
vi.mock("@/components/workflow/WorkflowPanel", () => ({
  WorkflowPanel: ({
    onRegenerate,
  }: {
    onRegenerate?: (stepId: string, unitIds: string[]) => void;
  }) => (
    <div data-testid="workflow-panel" data-can-regenerate={onRegenerate ? "yes" : "no"}>
      <button onClick={() => onRegenerate?.("storyboard", ["SEG-1"])}>workflow-regenerate</button>
      <button onClick={() => onRegenerate?.("video", ["SEG-1"])}>workflow-regenerate-video</button>
    </div>
  ),
}));

// 剪辑视图自己读取剪辑时间线，行为在 EditTimelineView.test.tsx 覆盖；这里只关心集页何时挂载它。
vi.mock("./edit/EditTimelineView", () => ({
  EditTimelineView: ({ episode }: { episode: number }) => (
    <div data-testid="edit-timeline-view">episode {episode}</div>
  ),
}));

vi.mock("./overview/OverviewCanvas", () => ({
  OverviewCanvas: () => <div data-testid="overview-canvas">Overview</div>,
}));

vi.mock("./episodes/EpisodesView", () => ({
  EpisodesView: ({ projectName }: { projectName: string }) => (
    <div data-testid="episodes-view">{projectName}</div>
  ),
}));

vi.mock("./timeline/TimelineCanvas", () => ({
  TimelineCanvas: ({
    episodeScript,
    scriptFile,
    durationOptions,
    onUpdatePrompt,
    onMoveShot,
    onInsertShot,
    onRemoveShot,
    onGenerateStoryboard,
    onGenerateVideo,
    onGenerateNarration,
    onGenerateEpisodeNarration,
    view,
  }: {
    view: string;
    episodeScript: unknown;
    scriptFile?: string;
    durationOptions?: number[];
    onUpdatePrompt?: (segmentId: string, patch: Record<string, unknown>, scriptFile?: string) => Promise<boolean>;
    onMoveShot?: (shotId: string, afterId: string | null, scriptFile?: string) => Promise<boolean> | void;
    onInsertShot?: (afterId: string, novelText: string | undefined, scriptFile?: string) => Promise<boolean>;
    onRemoveShot?: (itemId: string, scriptFile?: string) => Promise<boolean>;
    onGenerateStoryboard?: (segmentId: string) => void;
    onGenerateVideo?: (segmentId: string) => void;
    onGenerateNarration?: (segmentId: string) => void;
    onGenerateEpisodeNarration?: (scriptFile?: string) => void;
  }) => (
    <div data-testid="timeline-canvas" data-view={view}>
      <div data-testid="timeline-has-script">{episodeScript ? "yes" : "no"}</div>
      <div data-testid="timeline-duration-options">{(durationOptions ?? []).join(",")}</div>
      <button
        onClick={(e) => {
          const el = e.currentTarget;
          onUpdatePrompt?.("SEG-1", { image_prompt: "new prompt" }, scriptFile).then(
            (refreshed) => el.setAttribute("data-result", String(refreshed)),
            (err: Error) => el.setAttribute("data-error", err.message),
          );
        }}
      >
        update-prompt
      </button>
      <button
        onClick={(e) => {
          const el = e.currentTarget;
          el.setAttribute("data-update-pending", "true");
          void Promise.resolve(
            onUpdatePrompt?.("SEG-1", { image_prompt: "new prompt" }, scriptFile),
          ).then(() => {
            el.setAttribute("data-update-pending", "false");
          });
        }}
      >
        update-prompt-await
      </button>
      <button
        onClick={(e) => {
          const el = e.currentTarget;
          void Promise.resolve(onMoveShot?.("SEG-1", "SEG-2", scriptFile)).then((moved) => {
            el.setAttribute("data-move-result", String(moved));
          });
        }}
      >
        move-shot-later
      </button>
      <button
        onClick={(e) => {
          const el = e.currentTarget;
          void onInsertShot?.("SEG-1", undefined, scriptFile).then((ok) => el.setAttribute("data-result", String(ok)));
        }}
      >
        insert-shot
      </button>
      <button
        onClick={(e) => {
          const el = e.currentTarget;
          void onRemoveShot?.("SEG-1", scriptFile).then((ok) => el.setAttribute("data-result", String(ok)));
        }}
      >
        remove-shot
      </button>
      <button onClick={() => onGenerateStoryboard?.("SEG-1")}>generate-storyboard</button>
      <button onClick={() => onGenerateVideo?.("SEG-1")}>generate-video</button>
      {onGenerateNarration && <button onClick={() => onGenerateNarration("SEG-1")}>generate-narration</button>}
      {onGenerateEpisodeNarration && (
        <button onClick={() => onGenerateEpisodeNarration()}>generate-episode-narration</button>
      )}
    </div>
  ),
}));

// 画廊替身为每个资产渲染一个生成按钮，路由测试经它触发资产图生成回调；
// 版本恢复与重新加载按钮把回调的结算值写到 data-result 上。
vi.mock("./lorebook/AssetGallery", () => ({
  AssetGallery: ({
    assetType,
    assets,
    onGenerate,
    onRestoreVersion,
    onReload,
  }: {
    assetType: string;
    assets: Record<string, unknown>;
    onGenerate: (name: string) => void;
    onRestoreVersion?: () => Promise<unknown> | void;
    onReload?: () => Promise<unknown> | void;
  }) => (
    <div data-testid={`${assetType}-gallery`} data-names={Object.keys(assets).join(",")}>
      {Object.keys(assets).map((name) => (
        <button key={name} onClick={() => onGenerate(name)}>
          generate-{assetType}
        </button>
      ))}
      <button
        onClick={(e) => {
          const el = e.currentTarget;
          void Promise.resolve(onRestoreVersion?.()).then((result) => el.setAttribute("data-result", String(result)));
        }}
      >
        restore-{assetType}
      </button>
      <button
        onClick={(e) => {
          const el = e.currentTarget;
          void Promise.resolve(onReload?.()).then((result) => el.setAttribute("data-result", String(result)));
        }}
      >
        reload-{assetType}
      </button>
    </div>
  ),
}));

const REFRESH_FAILED = "操作已完成，但页面数据刷新失败，请手动刷新查看最新状态";

/** 服务端 video-capabilities 应答：时长收窄结果由服务端按项目分辨率与生成模式算好回传。 */
function fakeVideoCapabilities(allowed: number[], raw: number[] = allowed) {
  return {
    provider_id: "gemini-aistudio",
    model: "veo-3.1",
    supported_durations: raw,
    max_duration: Math.max(...raw),
    max_reference_images: 3,
    first_frame: true,
    last_frame: true,
    source: "registry",
    voice_consistency: "soft",
    duration_constraints: {
      resolution: null,
      uses_reference_images: false,
      allowed,
      excluded: {},
    },
  } as Awaited<ReturnType<typeof API.getVideoCapabilities>>;
}

function makeProjectData(overrides: Partial<ProjectData> = {}): ProjectData {
  return {
    title: "Demo",
    content_mode: "drama",
    style: "Anime",
    episodes: [{ episode: 1, title: "EP1", script_file: "scripts/episode_1.json" }],
    characters: {
      Hero: { description: "hero description" },
    },
    scenes: { Temple: { description: "ancient temple" } },
    props: { Sword: { description: "rusty sword" } },
    ...overrides,
  };
}

function makeScript(): EpisodeScript { return makeDramaScript(); }

function makeDramaScript(): EpisodeScript {
  return {
    episode: 1,
    title: "EP1",
    content_mode: "drama",
    short_drama_meta: {},
    novel: { title: "n", chapter: "1" },
    scenes: [
      {
        scene_id: "SEG-1",
        duration_seconds: 6,
        segment_break: false,
        characters_in_scene: ["Hero"],
        image_prompt: "drama image prompt",
        video_prompt: "drama video prompt",
      },
    ],
  };
}

/** 在集页页头改集标题：点铅笔、输入、回车保存。 */
async function renameEpisode(title: string) {
  fireEvent.click(await screen.findByRole("button", { name: "编辑分集标题" }));
  const input = screen.getByRole("textbox", { name: "编辑分集标题" });
  fireEvent.change(input, { target: { value: title } });
  fireEvent.keyDown(input, { key: "Enter" });
}

function renderAt(path: string) {
  const { hook } = memoryLocation({ path });
  return render(
    <Router hook={hook}>
      <StudioCanvasRouter />
    </Router>,
  );
}

// 复刻真实入口的路由结构：父路由 `/:projectName` nest 出 `projectName` 参数，
// 供 StudioCanvasRouter 在 store 的 currentProjectName 还没同步时兜底判定演示态。
// 返回 `navigate` 以便测试原地切换路由——不卸载组件，复刻同一 StudioCanvasRouter 实例
// 从真实项目切到演示项目时的时序（wouter 路由参数变化不会重新挂载组件）。
function renderAtProjectRoute(projectName: string, subPath: string) {
  const { hook, navigate } = memoryLocation({ path: `/${projectName}${subPath}` });
  const view = render(
    <Router hook={hook}>
      <Route path="/:projectName" nest>
        <StudioCanvasRouter />
      </Route>
    </Router>,
  );
  return { ...view, navigate };
}

describe("StudioCanvasRouter", () => {
  beforeEach(() => {
    useProjectsStore.setState(useProjectsStore.getInitialState(), true);
    useAppStore.setState(useAppStore.getInitialState(), true);
    useConfigStatusStore.setState(useConfigStatusStore.getInitialState(), true);
    useTasksStore.setState(useTasksStore.getInitialState(), true);
    vi.restoreAllMocks();
    vi.spyOn(API, "getShortDramaSource").mockResolvedValue({ episode: 1, path: "source/short_drama/episodes/episode_01.md", markdown: "工厂正文", revision: "r1", exists: true });
  });

  it("shows loading state when currentProjectName is missing", () => {
    renderAt("/");
    expect(screen.getByText("加载中...")).toBeInTheDocument();
  });

  it("keeps showing loading state on a deep link while the project detail request is still in flight", () => {
    // 首屏加载先落地 currentProjectName（数据置空）再等 refreshProject 结算（见
    // router.tsx）：currentProjectName 非空不代表详情已到达。直接打开 /characters 这类
    // 深链时,若只看 currentProjectName 会把空集合渲染成可交互的「空项目」页面。
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: null,
      projectDetailLoading: true,
    });

    renderAt("/characters");

    expect(screen.getByText("加载中...")).toBeInTheDocument();
    expect(screen.queryByTestId("character-gallery")).not.toBeInTheDocument();
  });

  it("routes characters/scenes/props/episodes views correctly", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData(),
      currentScripts: {
        "episode_1.json": makeScript(),
      },
    });

    const viewCharacters = renderAt("/characters");
    expect(screen.getByTestId("character-gallery")).toHaveAttribute("data-names", "Hero");
    viewCharacters.unmount();

    const viewScenes = renderAt("/scenes");
    expect(screen.getByTestId("scene-gallery")).toHaveAttribute("data-names", "Temple");
    viewScenes.unmount();

    const viewProps = renderAt("/props");
    expect(screen.getByTestId("prop-gallery")).toHaveAttribute("data-names", "Sword");
    viewProps.unmount();

    const viewEpisodeList = renderAt("/episodes");
    expect(screen.getByTestId("episodes-view")).toHaveTextContent("demo");
    viewEpisodeList.unmount();

    const viewEpisodes = renderAt("/episodes/1");
    expect(screen.getByTestId("timeline-canvas")).toBeInTheDocument();
    expect(screen.getByTestId("timeline-has-script")).toHaveTextContent("yes");
    viewEpisodes.unmount();

    await waitFor(() => {
      expect(screen.queryByText("加载中...")).not.toBeInTheDocument();
    });
  });

  it("shows an empty state for an unknown workspace path and leads back to the overview", async () => {
    useProjectsStore.setState({ currentProjectName: "demo", currentProjectData: makeProjectData() });

    renderAt("/lorebook");

    expect(screen.getByRole("heading", { name: "这个页面不存在" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: "回到概览" }));
    expect(await screen.findByTestId("overview-canvas")).toBeInTheDocument();
  });

  // 同一 StudioCanvasRouter 实例从真实项目切到演示项目（路由 nest 下 projectName 参数
  // 变化，组件不会重新挂载）时，上一个真实项目遗留的时长能力缓存必须清空，否则真实后端的
  // 时长限制会继续套用到演示的虚构时长上，重新触发「不兼容」误报。
  it("clears cached provider/backend duration capabilities when switching from a real project into the demo route", async () => {
    vi.spyOn(API, "getVideoCapabilities").mockResolvedValue(fakeVideoCapabilities([5, 10]));
    useProjectsStore.setState({
      currentProjectName: "real-project",
      currentProjectData: makeProjectData(),
      currentScripts: { "episode_1.json": makeScript() },
    });

    const view = renderAtProjectRoute("real-project", "/episodes/1");
    await waitFor(() => {
      expect(screen.getByTestId("timeline-duration-options")).toHaveTextContent("5,10");
    });

    // 原地切换到演示项目：不卸载组件，复刻同一实例从真实项目切入演示态的时序，
    // 与 StudioWorkspace 的 effect 同步写入 store 一致。
    useProjectsStore.setState({
      currentProjectName: DEMO_PROJECT_NAME,
      currentProjectData: makeProjectData(),
      currentScripts: { "episode_1.json": makeScript() },
    });
    view.navigate(`/${DEMO_PROJECT_NAME}/episodes/1`);

    await waitFor(() => {
      // 缓存已清空：真实项目遗留的后端能力不再参与时长兼容性比对
      expect(screen.getByTestId("timeline-duration-options")).toHaveTextContent("");
    });
  });

  // 逐个分镜的时长候选取服务端按项目分辨率与生效 generation_mode 收窄后的结果：前端不再持有
  // 收窄规则，也不把分辨率或集号带上——服务端按项目为实际执行模型保存的档位求值。
  it.each([
    ["1080p", [8]],
    ["720p", [4, 6, 8]],
  ])("takes the per-shot duration options from the server-side narrowing at %s", async (resolution, allowed) => {
    const VEO = "gemini-aistudio/veo-3.1";
    const capabilitiesSpy = vi
      .spyOn(API, "getVideoCapabilities")
      .mockResolvedValue(fakeVideoCapabilities(allowed, [4, 6, 8]));
    useProjectsStore.setState({
      currentProjectName: "real-project",
      currentProjectData: makeProjectData({
        video_backend: VEO,
        model_settings: { [VEO]: { resolution } },
      }),
      currentScripts: { "episode_1.json": makeScript() },
    });

    renderAtProjectRoute("real-project", "/episodes/1");
    await waitFor(() => {
      // 精确比对而非包含：全集 "4,6,8" 也含子串 "8"
      expect(screen.getByTestId("timeline-duration-options")).toHaveTextContent(
        new RegExp(`^${allowed.join(",")}$`),
      );
    });
    expect(capabilitiesSpy).toHaveBeenCalledWith("real-project", expect.anything());
    const query = capabilitiesSpy.mock.calls[0]?.[1];
    // 工作台由服务端按 generation_mode 解析真实 i2v/r2v 桶，不用默认层覆盖。
    expect(query?.videoBackend).toBeUndefined();
    expect(query?.resolution).toBeUndefined();
    expect(query?.usesReferenceImages).toBeUndefined();
  });

  // demo→真实项目切换后路由已让 demoMode 变为 false，但 store 的 currentProjectName 还滞留
  // 演示项目名；只看 demoMode 会对着后端不存在的演示项目发一次必然失败的 /video-capabilities。
  it("skips the video-capabilities lookup for the stale demo project name after navigating to a real project, before the store catches up", async () => {
    const capabilitiesSpy = vi
      .spyOn(API, "getVideoCapabilities")
      .mockResolvedValue({ supported_durations: [5, 10] } as Awaited<
        ReturnType<typeof API.getVideoCapabilities>
      >);
    useProjectsStore.setState({
      currentProjectName: DEMO_PROJECT_NAME,
      currentProjectData: makeProjectData(),
      currentScripts: { "episode_1.json": makeScript() },
    });

    const view = renderAtProjectRoute(DEMO_PROJECT_NAME, "/episodes/1");
    await waitFor(() => {
      expect(screen.getByTestId("timeline-canvas")).toBeInTheDocument();
    });

    // 原地切换到真实项目：只挪路由，store 的 currentProjectName 刻意留在演示项目名，
    // 复刻 StudioWorkspace 的 effect 尚未执行完成的时间窗。
    view.navigate("/real-project/episodes/1");

    expect(capabilitiesSpy).not.toHaveBeenCalledWith(DEMO_PROJECT_NAME, expect.anything());
  });

  it("opens the shot editor for an episode whose script is generated", () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData({
        episodes: [
          { episode: 1, title: "EP1", script_file: "scripts/episode_1.json", script_status: "generated" },
        ],
      }),
      currentScripts: { "episode_1.json": makeScript() },
    });

    renderAt("/episodes/1");

    expect(screen.getByTestId("timeline-canvas")).toBeInTheDocument();
    expect(screen.getByTestId("timeline-has-script")).toHaveTextContent("yes");
    expect(screen.queryByTestId("episode-source-review")).not.toBeInTheDocument();
  });

  it("switches an episode with a script between the storyboard and edit views", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData({
        episodes: [
          { episode: 1, title: "EP1", script_file: "scripts/episode_1.json", script_status: "generated" },
        ],
      }),
      currentScripts: { "episode_1.json": makeScript() },
    });
    const { hook, searchHook } = memoryLocation({ path: "/episodes/1" });
    render(
      <Router hook={hook} searchHook={searchHook}>
        <StudioCanvasRouter />
      </Router>,
    );

    expect(screen.getByRole("tab", { name: "分镜" })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("tab", { name: "剪辑" }));

    // 剪辑视图按需加载，首次切入要等 chunk 到达
    expect(await screen.findByTestId("edit-timeline-view")).toHaveTextContent("episode 1");
    expect(screen.queryByTestId("timeline-canvas")).not.toBeInTheDocument();
    expect(screen.getByTestId("workflow-panel")).toBeInTheDocument();
    expect(screen.getByRole("tabpanel", { name: "剪辑" })).toContainElement(screen.getByTestId("edit-timeline-view"));

    fireEvent.click(screen.getByRole("tab", { name: "分镜" }));
    expect(screen.getByTestId("timeline-canvas")).toHaveAttribute("data-view", "board");
    expect(screen.getByRole("tabpanel", { name: "分镜" })).toContainElement(screen.getByTestId("timeline-canvas"));

    // 方向键只移动焦点，切换视图要按下：剪辑视图会卸载画布，不随焦点经过就切走；Tab 只停在选中的那个
    const storyboard = screen.getByRole("tab", { name: "分镜" });
    expect(storyboard).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("tab", { name: "剪辑" })).toHaveAttribute("tabindex", "-1");
    const user = userEvent.setup();
    act(() => storyboard.focus());
    await user.keyboard("{ArrowRight}");
    const edit = screen.getByRole("tab", { name: "剪辑" });
    expect(edit).toHaveFocus();
    expect(storyboard).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{Enter}");
    expect(screen.getByRole("tabpanel", { name: "剪辑" })).toContainElement(screen.getByTestId("edit-timeline-view"));
  });

  it("deep-links each canvas view through ?view= and writes no view for the default one", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData({
        grid_storyboard: true,
        episodes: [
          { episode: 1, title: "EP1", script_file: "scripts/episode_1.json", script_status: "generated" },
        ],
      }),
      currentScripts: { "episode_1.json": makeScript() },
    });
    const { hook, searchHook, history } = memoryLocation({ path: "/episodes/1?view=plan", record: true });
    render(
      <Router hook={hook} searchHook={searchHook}>
        <StudioCanvasRouter />
      </Router>,
    );

    expect(screen.queryByRole("tab", { name: "脚本规划" })).not.toBeInTheDocument();
    expect(await screen.findByTestId("timeline-canvas")).toHaveAttribute("data-view", "board");
    expect(screen.queryByRole("tab", { name: "多宫格分镜图" })).not.toBeInTheDocument();

    // 分镜是有剧本时的缺省视图：地址不带 view，剧本生成后停在缺省视图的页面才会自动落到分镜
    fireEvent.click(screen.getByRole("tab", { name: "剪辑" }));
    await screen.findByTestId("edit-timeline-view");
    fireEvent.click(screen.getByRole("tab", { name: "分镜" }));
    expect(screen.getByTestId("timeline-canvas")).toHaveAttribute("data-view", "board");
    expect(history.at(-1)).toBe("/episodes/1");
  });

  it("opens the board before a script exists and falls back for an unknown view", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData({
        episodes: [
          { episode: 1, title: "EP1", script_file: "scripts/episode_1.json", script_status: "segmented" },
        ],
      }),
      currentScripts: {},
    });

    renderAt("/episodes/1?view=board");
    expect(screen.getByRole("tab", { name: "分镜" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByTestId("timeline-canvas")).toHaveAttribute("data-view", "board");
    cleanup();

    renderAt("/episodes/1?view=nonsense");
    expect(screen.getByRole("tab", { name: "剧本" })).toHaveAttribute("aria-selected", "true");
  });

  it("deletes the episode from the page header menu", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData({
        episodes: [
          { episode: 1, title: "EP1", script_file: "scripts/episode_1.json", script_status: "generated" },
        ],
      }),
      currentScripts: { "episode_1.json": makeScript() },
    });
    const impact = { episode: 1, recoverable: true, revision: "rev-1", text: "删除「EP1」后可以从集原文重新生成。" };
    const remove = vi
      .spyOn(API, "deleteEpisode")
      .mockResolvedValueOnce({ status: "confirmation_required", impact } as never)
      .mockResolvedValueOnce({ status: "deleted", impact } as never);
    useProjectsStore.setState({ refreshProject: vi.fn().mockResolvedValue("success") });
    const user = userEvent.setup();
    const { hook, searchHook, history } = memoryLocation({ path: "/episodes/1", record: true });
    render(
      <Router hook={hook} searchHook={searchHook}>
        <StudioCanvasRouter />
      </Router>,
    );

    await user.click(screen.getByRole("button", { name: "这一集的更多操作" }));
    await user.click(await screen.findByRole("menuitem", { name: "删除这一集" }));
    const dialog = await screen.findByRole("alertdialog", { name: "删除「EP1」" });
    await user.click(within(dialog).getByRole("button", { name: "删除" }));

    // 删掉的是正在看的这一集，回到分集列表
    await waitFor(() => expect(history.at(-1)).toBe("/episodes"));
    expect(remove).toHaveBeenLastCalledWith("demo", 1, "rev-1");
  });

  it("opens the edit view directly from its link", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData({
        episodes: [
          { episode: 1, title: "EP1", script_file: "scripts/episode_1.json", script_status: "generated" },
        ],
      }),
      currentScripts: { "episode_1.json": makeScript() },
    });
    const { hook, searchHook } = memoryLocation({ path: episodeEditViewPath(1) });
    render(
      <Router hook={hook} searchHook={searchHook}>
        <StudioCanvasRouter />
      </Router>,
    );

    expect(screen.getByRole("tab", { name: "剪辑" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByTestId("edit-timeline-view")).toBeInTheDocument();
  });

  it("opens the edit view for an episode that has no script yet", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData({
        episodes: [{ episode: 1, title: "EP1", script_file: "scripts/episode_1.json" }],
      }),
      currentScripts: {},
    });
    const { hook, searchHook } = memoryLocation({ path: episodeEditViewPath(1) });
    render(
      <Router hook={hook} searchHook={searchHook}>
        <StudioCanvasRouter />
      </Router>,
    );

    expect(screen.getByRole("tab", { name: "剪辑" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByTestId("edit-timeline-view")).toBeInTheDocument();
  });

  it("submits character generation, marks the character active and confirms with a toast", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData(),
      currentScripts: { "episode_1.json": makeScript() },
    });
    vi.spyOn(API, "generateCharacter").mockResolvedValue({ success: true, task_id: "t-1", deduped: false, message: "已提交" });

    renderAt("/characters");

    fireEvent.click(screen.getByText("generate-character"));
    await waitFor(() => {
      expect(API.generateCharacter).toHaveBeenCalledWith("demo", "Hero");
      expect(useAppStore.getState().toast?.text).toContain("生成任务已提交");
      expect(useAppStore.getState().toast?.tone).toBe("success");
      // 入队成功后应立即乐观占用该角色，避免 SSE 轮询落地前的空窗被误判为空闲
      // 而并发触发 image_edit（见 tasks-store.ts 乐观占用小节）。
      const { tasks, optimisticActive } = useTasksStore.getState();
      expect(selectActiveResourceIds(tasks, "character", "demo", optimisticActive).has("Hero")).toBe(true);
    });
  });

  it("reports scene and prop generation failures with toast", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData(),
      currentScripts: { "episode_1.json": makeScript() },
    });
    vi.spyOn(API, "generateProjectScene").mockRejectedValue(new Error("scene generate failed"));
    vi.spyOn(API, "generateProjectProp").mockRejectedValue(new Error("prop generate failed"));

    const scenes = renderAt("/scenes");
    fireEvent.click(screen.getByText("generate-scene"));
    await waitFor(() => {
      expect(API.generateProjectScene).toHaveBeenCalledWith("demo", "Temple");
      expect(useAppStore.getState().toast?.text).toContain("提交失败");
    });
    scenes.unmount();
    useAppStore.setState({ toast: null });

    renderAt("/props");
    fireEvent.click(screen.getByText("generate-prop"));
    await waitFor(() => {
      expect(API.generateProjectProp).toHaveBeenCalledWith("demo", "Sword");
      expect(useAppStore.getState().toast?.text).toContain("提交失败");
    });
  });

  it("marks scene generation as optimistically active on submit success", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData(),
      currentScripts: { "episode_1.json": makeScript() },
    });

    vi.spyOn(API, "getProject").mockResolvedValue({
      project: makeProjectData(),
      scripts: { "episode_1.json": makeScript() },
    });
    vi.spyOn(API, "generateProjectScene").mockResolvedValue({ success: true, task_id: "t-1", deduped: false, message: "已提交" });

    renderAt("/scenes");
    fireEvent.click(screen.getByText("generate-scene"));
    await waitFor(() => {
      expect(API.generateProjectScene).toHaveBeenCalledWith("demo", "Temple");
      const { tasks, optimisticActive } = useTasksStore.getState();
      expect(selectActiveResourceIds(tasks, "scene", "demo", optimisticActive).has("Temple")).toBe(true);
    });
  });

  it("marks prop generation as optimistically active on submit success", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData(),
      currentScripts: { "episode_1.json": makeScript() },
    });

    vi.spyOn(API, "getProject").mockResolvedValue({
      project: makeProjectData(),
      scripts: { "episode_1.json": makeScript() },
    });
    vi.spyOn(API, "generateProjectProp").mockResolvedValue({ success: true, task_id: "t-1", deduped: false, message: "已提交" });

    renderAt("/props");
    fireEvent.click(screen.getByText("generate-prop"));
    await waitFor(() => {
      expect(API.generateProjectProp).toHaveBeenCalledWith("demo", "Sword");
      const { tasks, optimisticActive } = useTasksStore.getState();
      expect(selectActiveResourceIds(tasks, "prop", "demo", optimisticActive).has("Sword")).toBe(true);
    });
  });

  it("runs timeline callbacks and handles generation failures", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData(),
      currentScripts: { "episode_1.json": makeScript() },
    });

    vi.spyOn(API, "getProject").mockResolvedValue({
      project: makeProjectData(),
      scripts: { "episode_1.json": makeScript() },
    });
    vi.spyOn(API, "updateScene").mockRejectedValue(new Error("update failed"));
    vi.spyOn(API, "generateStoryboard").mockRejectedValue(new Error("storyboard failed"));
    vi.spyOn(API, "generateVideo").mockRejectedValue(new Error("video failed"));

    renderAt("/episodes/1");

    fireEvent.click(screen.getByText("update-prompt"));
    await waitFor(() => {
      expect(API.updateScene).toHaveBeenCalledWith("demo", "SEG-1", "episode_1.json", {
        image_prompt: "new prompt",
      });
      // 保存失败如实抛给分镜详情，由提示条显示，不另弹全局提示
      expect(screen.getByText("update-prompt")).toHaveAttribute("data-error", "update failed");
    });
    expect(useAppStore.getState().toast).toBeNull();

    fireEvent.click(screen.getByText("generate-storyboard"));
    await waitFor(() => {
      expect(API.generateStoryboard).toHaveBeenCalledWith(
        "demo",
        "SEG-1",
        "drama image prompt",
        "episode_1.json",
      );
      expect(useAppStore.getState().toast?.text).toContain("生成分镜失败");
    });

    fireEvent.click(screen.getByText("generate-video"));
    await waitFor(() => {
      expect(API.generateVideo).toHaveBeenCalledWith(
        "demo",
        "SEG-1",
        "drama video prompt",
        "episode_1.json",
        6,
      );
      expect(useAppStore.getState().toast?.text).toContain("生成视频失败");
    });
  });

  // TimelineCanvas 的 ShotDetail.handleSave / handleRefsSave 靠 await 这个回调维持保存中
  // 状态；handleUpdatePrompt 改回真实结果契约后若在此处包一层 voidPromise（丢弃返回值的
  // 同时也让包装函数立即 resolve），await 会在 PATCH 真正落库前就提前结束。
  it("keeps the update-prompt callback pending until the underlying write settles", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData(),
      currentScripts: { "episode_1.json": makeScript() },
    });

    vi.spyOn(API, "getProject").mockResolvedValue({
      project: makeProjectData(),
      scripts: { "episode_1.json": makeScript() },
    });
    let resolveUpdate: (result: { success: boolean }) => void = () => {};
    vi.spyOn(API, "updateScene").mockReturnValue(
      new Promise((resolve) => {
        resolveUpdate = resolve;
      }),
    );

    renderAt("/episodes/1");

    fireEvent.click(screen.getByText("update-prompt-await"));

    // 放几轮微任务/宏任务过去：底层 PATCH 仍未落地，回调不能提前 resolve——
    // 若用 voidPromise 包一层，包装函数会在这里已经 resolve，暴露不出真正的 bug。
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.getByText("update-prompt-await")).toHaveAttribute("data-update-pending", "true");

    resolveUpdate({ success: true });

    await waitFor(() => {
      expect(screen.getByText("update-prompt-await")).toHaveAttribute(
        "data-update-pending",
        "false",
      );
    });
  });

  it("moves a shot after its anchor", async () => {
    const script = makeDramaScript() as DramaEpisodeScript;
    script.scenes.push({
      segment_break: false,
      characters_in_scene: [],
      scene_id: "SEG-2",
      duration_seconds: 3,
      image_prompt: "p2",
      video_prompt: "v2",
    });
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData({ content_mode: "drama" }),
      currentScripts: { "episode_1.json": script },
    });

    vi.spyOn(API, "getProject").mockResolvedValue({
      project: makeProjectData({ content_mode: "drama" }),
      scripts: { "episode_1.json": script },
    });
    const moveSpy = vi.spyOn(API, "moveScriptItem").mockResolvedValue({ success: true });

    renderAt("/episodes/1");

    fireEvent.click(screen.getByText("move-shot-later"));
    await waitFor(() => {
      expect(moveSpy).toHaveBeenCalledWith("demo", "episode_1.json", "SEG-1", "SEG-2");
    });
    // 重排 + 本地刷新都成功 → 报告移动成功
    await waitFor(() => {
      expect(screen.getByText("move-shot-later")).toHaveAttribute("data-move-result", "true");
    });
  });

  it("reports move failure and toasts when the move request fails", async () => {
    const script = makeDramaScript() as DramaEpisodeScript;
    script.scenes.push({
      segment_break: false,
      characters_in_scene: [],
      scene_id: "SEG-2",
      duration_seconds: 3,
      image_prompt: "p2",
      video_prompt: "v2",
    });
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData({ content_mode: "drama" }),
      currentScripts: { "episode_1.json": script },
    });

    vi.spyOn(API, "getProject").mockResolvedValue({
      project: makeProjectData({ content_mode: "drama" }),
      scripts: { "episode_1.json": script },
    });
    vi.spyOn(API, "moveScriptItem").mockRejectedValue(new Error("server boom"));

    renderAt("/episodes/1");

    fireEvent.click(screen.getByText("move-shot-later"));
    await waitFor(() => {
      expect(screen.getByText("move-shot-later")).toHaveAttribute("data-move-result", "false");
    });
    expect(useAppStore.getState().toast?.text).toContain("server boom");
    expect(useAppStore.getState().toast?.tone).toBe("error");
  });

  it("reports move failure when local refresh fails after a successful move", async () => {
    const script = makeDramaScript() as DramaEpisodeScript;
    script.scenes.push({
      segment_break: false,
      characters_in_scene: [],
      scene_id: "SEG-2",
      duration_seconds: 3,
      image_prompt: "p2",
      video_prompt: "v2",
    });
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData({ content_mode: "drama" }),
      currentScripts: { "episode_1.json": script },
    });

    // 移动接口成功，但项目刷新失败：本地 segments 仍是旧顺序，
    // 必须报告失败，否则调用方会推进 selectedIndex 切到错误分镜
    vi.spyOn(API, "getProject").mockRejectedValue(new Error("network down"));
    vi.spyOn(API, "moveScriptItem").mockResolvedValue({ success: true });

    renderAt("/episodes/1");

    fireEvent.click(screen.getByText("move-shot-later"));
    await waitFor(() => {
      expect(screen.getByText("move-shot-later")).toHaveAttribute("data-move-result", "false");
    });
  });

  it("warns when the local refresh fails after a successful move", async () => {
    const script = makeDramaScript() as DramaEpisodeScript;
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData({ content_mode: "drama" }),
      currentScripts: { "episode_1.json": script },
    });
    vi.spyOn(API, "getProject").mockRejectedValue(new Error("network down"));
    vi.spyOn(API, "moveScriptItem").mockResolvedValue({ success: true });

    renderAt("/episodes/1");

    fireEvent.click(screen.getByText("move-shot-later"));
    await waitFor(() => expect(screen.getByText("move-shot-later")).toHaveAttribute("data-move-result", "false"));
    expect(useAppStore.getState().toast).toMatchObject({ text: REFRESH_FAILED, tone: "warning" });
  });

  it.each([
    ["insert-shot", "insertScriptItem"],
    ["remove-shot", "removeScriptItem"],
  ] as const)(
    "%s reports success once the edit is committed even if the local refresh then fails",
    async (button, apiMethod) => {
      const script = makeDramaScript() as DramaEpisodeScript;
      useProjectsStore.setState({
        currentProjectName: "demo",
        currentProjectData: makeProjectData({ content_mode: "drama" }),
        currentScripts: { "episode_1.json": script },
      });
      vi.spyOn(API, "getProject").mockRejectedValue(new Error("network down"));
      const editSpy = vi.spyOn(API, apiMethod).mockResolvedValue({ success: true } as never);

      renderAt("/episodes/1");

      fireEvent.click(screen.getByText(button));
      await waitFor(() => expect(screen.getByText(button)).toHaveAttribute("data-result", "true"));
      expect(editSpy).toHaveBeenCalledTimes(1);
      expect(useAppStore.getState().toast?.tone).toBe("warning");
    },
  );

  it("regenerates against the script file of the episode being viewed, not the first one loaded", async () => {
    // 多集项目的 currentScripts 装着全部剧集。按第一个键重生，用户在第 2 集按下的
    // 「重新生成」会打到第 1 集的剧本上，重做的是另一集已经付费的产物。
    const episode2 = { ...makeScript(), episode: 2, title: "EP2" };
    const projectData = makeProjectData({
      episodes: [
        { episode: 1, title: "EP1", script_file: "scripts/episode_1.json" },
        { episode: 2, title: "EP2", script_file: "scripts/episode_2.json" },
      ],
    });
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: projectData,
      currentScripts: { "episode_1.json": makeScript(), "episode_2.json": episode2 },
    });
    vi.spyOn(API, "getProject").mockResolvedValue({
      project: projectData,
      scripts: { "episode_1.json": makeScript(), "episode_2.json": episode2 },
    });
    vi.spyOn(API, "generateStoryboard").mockResolvedValue({
      success: true,
      task_id: "t-sb",
      deduped: false,
      message: "已提交",
    });

    renderAt("/episodes/2");

    fireEvent.click(screen.getByText("workflow-regenerate"));
    await waitFor(() => {
      expect(API.generateStoryboard).toHaveBeenCalledWith(
        "demo",
        "SEG-1",
        "drama image prompt",
        "episode_2.json",
      );
    });
  });

  it("routes the panel's video regenerate through the storyboard video entry", async () => {
    const projectData = makeProjectData();
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: projectData,
      currentScripts: { "episode_1.json": makeScript() },
    });
    vi.spyOn(API, "getProject").mockResolvedValue({
      project: projectData,
      scripts: { "episode_1.json": makeScript() },
    });
    vi.spyOn(API, "generateVideo").mockRejectedValue(new Error("provider down"));

    renderAt("/episodes/1");

    fireEvent.click(screen.getByText("workflow-regenerate-video"));
    await waitFor(() => {
      expect(useAppStore.getState().toast?.tone).toBe("error");
      expect(useAppStore.getState().toast?.text).toBe("生成视频失败：provider down");
    });
    expect(API.generateVideo).toHaveBeenCalledWith("demo", "SEG-1", "drama video prompt", "episode_1.json", 6);
  });

  it("resolves drama scenes by scene_id when generating storyboard", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData({ content_mode: "drama" }),
      currentScripts: { "episode_1.json": makeDramaScript() },
    });

    vi.spyOn(API, "getProject").mockResolvedValue({
      project: makeProjectData({ content_mode: "drama" }),
      scripts: { "episode_1.json": makeDramaScript() },
    });
    vi.spyOn(API, "generateStoryboard").mockResolvedValue({
      success: true,
      task_id: "t-sb",
      deduped: false,
      message: "已提交",
    });

    renderAt("/episodes/1");

    fireEvent.click(screen.getByText("generate-storyboard"));
    await waitFor(() => {
      expect(API.generateStoryboard).toHaveBeenCalledWith(
        "demo",
        "SEG-1",
        "drama image prompt",
        "episode_1.json",
      );
      const { tasks, optimisticActive } = useTasksStore.getState();
      expect(selectActiveResourceIds(tasks, "storyboard", "demo", optimisticActive).has("SEG-1")).toBe(true);
    });
  });

  it("reports character generation failure with an error toast", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData(),
      currentScripts: { "episode_1.json": makeScript() },
    });

    vi.spyOn(API, "getProject").mockResolvedValue({
      project: makeProjectData(),
      scripts: { "episode_1.json": makeScript() },
    });
    vi.spyOn(API, "generateCharacter").mockRejectedValue(new Error("character generate failed"));

    renderAt("/characters");

    fireEvent.click(screen.getByText("generate-character"));
    await waitFor(() => {
      expect(API.generateCharacter).toHaveBeenCalledWith("demo", "Hero");
      expect(useAppStore.getState().toast?.text).toContain("提交失败");
      expect(useAppStore.getState().toast?.tone).toBe("error");
    });
  });

  it("saves the episode title from the page header without a success toast", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData(),
      currentScripts: { "episode_1.json": makeScript() },
    });

    vi.spyOn(API, "getProject").mockResolvedValue({
      project: makeProjectData(),
      scripts: { "episode_1.json": makeScript() },
    });
    vi.spyOn(API, "updateEpisode").mockResolvedValue({ success: true });

    renderAt("/episodes/1");

    await renameEpisode("新标题");
    await waitFor(() => {
      expect(API.updateEpisode).toHaveBeenCalledWith("demo", 1, { title: "新标题" });
      expect(API.getProject).toHaveBeenCalled();
    });
    // 即时生效的修改成功不提示，标题回到展示态
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "编辑分集标题" })).not.toBeInTheDocument());
    expect(useAppStore.getState().toast).toBeNull();
  });

  it("leaves the edit state and warns when the refresh after saving the title fails", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData(),
      currentScripts: { "episode_1.json": makeScript() },
    });
    vi.spyOn(API, "getProject").mockRejectedValue(new Error("network down"));
    vi.spyOn(API, "updateEpisode").mockResolvedValue({ success: true });

    renderAt("/episodes/1");

    await renameEpisode("新标题");
    await waitFor(() =>
      expect(useAppStore.getState().toast).toMatchObject({ text: REFRESH_FAILED, tone: "warning" }),
    );
    // 标题已保存，不停在编辑态引人重复提交
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "编辑分集标题" })).not.toBeInTheDocument());
  });

  it.each(["restore-character", "reload-character"])(
    "%s warns and reports not-synced when the refresh after the write fails",
    async (button) => {
      useProjectsStore.setState({
        currentProjectName: "demo",
        currentProjectData: makeProjectData(),
        currentScripts: { "episode_1.json": makeScript() },
      });
      vi.spyOn(API, "getProject").mockRejectedValue(new Error("network down"));

      renderAt("/characters");

      fireEvent.click(screen.getByText(button));
      await waitFor(() => expect(screen.getByText(button)).toHaveAttribute("data-result", "false"));
      expect(useAppStore.getState().toast).toMatchObject({ text: REFRESH_FAILED, tone: "warning" });
    },
  );

  it("drops an unsaved title draft when switching to another episode", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData({
        episodes: [
          { episode: 1, title: "EP1", script_file: "scripts/episode_1.json", script_status: "generated" },
          { episode: 2, title: "EP2", script_file: "scripts/episode_2.json", script_status: "generated" },
        ],
      }),
      currentScripts: { "episode_1.json": makeScript(), "episode_2.json": makeScript() },
    });
    const { hook, searchHook, navigate } = memoryLocation({ path: "/episodes/1" });
    render(
      <Router hook={hook} searchHook={searchHook}>
        <StudioCanvasRouter />
      </Router>,
    );

    fireEvent.click(screen.getByRole("button", { name: "编辑分集标题" }));
    fireEvent.change(screen.getByRole("textbox", { name: "编辑分集标题" }), { target: { value: "写给第一集的标题" } });
    act(() => navigate("/episodes/2"));

    expect(screen.queryByRole("textbox", { name: "编辑分集标题" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "EP2" })).toBeInTheDocument();
  });

  it("reports episode title update failure with an error toast", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData(),
      currentScripts: { "episode_1.json": makeScript() },
    });

    vi.spyOn(API, "getProject").mockResolvedValue({
      project: makeProjectData(),
      scripts: { "episode_1.json": makeScript() },
    });
    vi.spyOn(API, "updateEpisode").mockRejectedValue(new Error("episode title failed"));

    renderAt("/episodes/1");

    await renameEpisode("新标题");
    await waitFor(() => {
      expect(API.updateEpisode).toHaveBeenCalledWith("demo", 1, { title: "新标题" });
      expect(useAppStore.getState().toast?.text).toContain("更新分集标题失败");
      expect(useAppStore.getState().toast?.tone).toBe("error");
    });
    // 失败时保持编辑态，输入不丢
    expect(screen.getByRole("textbox", { name: "编辑分集标题" })).toHaveValue("新标题");
  });

  it("hides narration generation entries for post-production projects", () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData({ narration_delivery: "post_production" }),
      currentScripts: { "episode_1.json": makeScript() },
    });
    vi.spyOn(API, "getProject").mockResolvedValue({
      project: makeProjectData({ narration_delivery: "post_production" }),
      scripts: { "episode_1.json": makeScript() },
    });

    renderAt("/episodes/1");

    expect(screen.queryByText("generate-narration")).not.toBeInTheDocument();
    expect(screen.queryByText("generate-episode-narration")).not.toBeInTheDocument();
  });

  it("submits narration generation and shows a success toast", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData({ narration_delivery: "use_tts" }),
      currentScripts: { "episode_1.json": makeScript() },
    });

    vi.spyOn(API, "getProject").mockResolvedValue({
      project: makeProjectData({ narration_delivery: "use_tts" }),
      scripts: { "episode_1.json": makeScript() },
    });
    vi.spyOn(API, "generateNarrationAudio").mockResolvedValue({
      success: true,
      task_id: "t-1",
      deduped: false,
      message: "已提交",
    });

    renderAt("/episodes/1");

    fireEvent.click(screen.getByText("generate-narration"));
    await waitFor(() => {
      expect(API.generateNarrationAudio).toHaveBeenCalledWith("demo", "SEG-1", "episode_1.json");
      expect(useAppStore.getState().toast?.text).toContain("旁白配音");
      expect(useAppStore.getState().toast?.tone).toBe("success");
    });
  });

  it("reports narration generation failure with an error toast", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData({ narration_delivery: "use_tts" }),
      currentScripts: { "episode_1.json": makeScript() },
    });

    vi.spyOn(API, "getProject").mockResolvedValue({
      project: makeProjectData({ narration_delivery: "use_tts" }),
      scripts: { "episode_1.json": makeScript() },
    });
    vi.spyOn(API, "generateNarrationAudio").mockRejectedValue(new Error("tts failed"));

    renderAt("/episodes/1");

    fireEvent.click(screen.getByText("generate-narration"));
    await waitFor(() => {
      expect(useAppStore.getState().toast?.text).toContain("生成旁白配音失败");
      expect(useAppStore.getState().toast?.tone).toBe("error");
    });
  });

  it("submits episode narration batch and reports the submitted count", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData({ narration_delivery: "use_tts" }),
      currentScripts: { "episode_1.json": makeScript() },
    });

    vi.spyOn(API, "getProject").mockResolvedValue({
      project: makeProjectData({ narration_delivery: "use_tts" }),
      scripts: { "episode_1.json": makeScript() },
    });
    vi.spyOn(API, "generateEpisodeNarrationAudio").mockResolvedValue({
      success: true,
      task_ids: ["t-1", "t-2"],
      task_ids_by_segment: { "seg-1": "t-1", "seg-2": "t-2" },
      deduped: false,
      message: "已提交",
    });

    renderAt("/episodes/1");

    fireEvent.click(screen.getByText("generate-episode-narration"));
    await waitFor(() => {
      expect(API.generateEpisodeNarrationAudio).toHaveBeenCalledWith("demo", "episode_1.json");
      expect(useAppStore.getState().toast?.text).toContain("已提交 2 个旁白配音生成任务");
      expect(useAppStore.getState().toast?.tone).toBe("success");
    });
  });

  it("tells the user when episode narration has nothing missing", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData({ narration_delivery: "use_tts" }),
      currentScripts: { "episode_1.json": makeScript() },
    });

    vi.spyOn(API, "getProject").mockResolvedValue({
      project: makeProjectData({ narration_delivery: "use_tts" }),
      scripts: { "episode_1.json": makeScript() },
    });
    vi.spyOn(API, "generateEpisodeNarrationAudio").mockResolvedValue({
      success: true,
      task_ids: [],
      task_ids_by_segment: {},
      deduped: false,
      message: "无需补缺",
    });

    renderAt("/episodes/1");

    fireEvent.click(screen.getByText("generate-episode-narration"));
    await waitFor(() => {
      expect(useAppStore.getState().toast?.text).toContain("所有分镜均已生成旁白配音");
      expect(useAppStore.getState().toast?.tone).toBe("success");
    });
  });

  it("blocks narration generation when no audio provider is configured", async () => {
    useProjectsStore.setState({
      currentProjectName: "demo",
      currentProjectData: makeProjectData({ narration_delivery: "use_tts" }),
      currentScripts: { "episode_1.json": makeScript() },
    });
    useConfigStatusStore.setState({
      initialized: true,
      availableMediaTypes: ["image", "video", "text"],
    });

    vi.spyOn(API, "getProject").mockResolvedValue({
      project: makeProjectData({ narration_delivery: "use_tts" }),
      scripts: { "episode_1.json": makeScript() },
    });
    const generateSpy = vi.spyOn(API, "generateNarrationAudio");

    renderAt("/episodes/1");

    fireEvent.click(screen.getByText("generate-narration"));
    await waitFor(() => {
      expect(useAppStore.getState().toast?.text).toContain("音频供应商");
      expect(useAppStore.getState().toast?.tone).toBe("error");
    });
    expect(generateSpy).not.toHaveBeenCalled();
  });
});
