import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import { EpisodeAssetGenerateDialog } from "./EpisodeAssetGenerateDialog";
import { useAppStore } from "@/stores/app-store";
import { useAssetSheetBatchStore } from "@/stores/asset-sheet-batch-store";
import { useProjectsStore } from "@/stores/projects-store";
import type { EpisodeAssetPlan, ProjectData } from "@/types";

const PLAN: EpisodeAssetPlan = {
  total: 12,
  by_type: { character: 5, scene: 4, prop: 3 },
  text_calls: 12,
  image_calls: 12,
};

function generation(overrides: Record<string, unknown> = {}) {
  return {
    batch_id: "b1",
    members: [
      {
        unit_id: "character/阿岚",
        asset_type: "character",
        name: "阿岚",
        derivative: null,
        task_id: "t1",
        deduped: false,
        status: "queued",
      },
    ],
    registered: { character: ["阿岚"] },
    description_filled: [{ asset_type: "character", name: "阿岚" }],
    description_fill_failed: [],
    conflicts: [],
    without_description: [],
    ...overrides,
  } as never;
}

beforeEach(() => {
  useAppStore.setState(useAppStore.getInitialState(), true);
  useAssetSheetBatchStore.setState({ batches: {}, trackedTaskIds: new Set() });
  const project: ProjectData = { title: "短剧", style: "", content_mode: "drama", characters: {}, episodes: [] };
  useProjectsStore.getState().setCurrentProject("demo", project);
  vi.spyOn(API, "getProject").mockResolvedValue({ project, scripts: {} });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("EpisodeAssetGenerateDialog", () => {
  it("shows the batch list and only generates after confirming", () => {
    const generate = vi.spyOn(API, "generateEpisodeAssets").mockResolvedValue(generation());
    const onClose = vi.fn();
    const onSubmitted = vi.fn();

    render(
      <EpisodeAssetGenerateDialog
        projectName="demo"
        episode={3}
        episodeTitle="雨夜对峙"
        plan={PLAN}
        onClose={onClose}
        onSubmitted={onSubmitted}
      />,
    );

    // 弹窗只摆范围与花费，不收集任何输入：清单是只读的。
    expect(screen.getByText("第 3 集 · 雨夜对峙")).toBeInTheDocument();
    expect(screen.getByText("将处理 12 个资产")).toBeInTheDocument();
    expect(screen.getByText("角色 5")).toBeInTheDocument();
    expect(screen.getByText("场景 4")).toBeInTheDocument();
    expect(screen.getByText("道具 3")).toBeInTheDocument();
    expect(screen.getByText("预计调用 文本模型 12 次 · 图像模型 12 次")).toBeInTheDocument();
    expect(generate).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "确定" }));

    return waitFor(() => {
      expect(generate).toHaveBeenCalledWith("demo", 3);
      expect(useAssetSheetBatchStore.getState().batches.b1?.members).toHaveLength(1);
      expect(onSubmitted).toHaveBeenCalledTimes(1);
      expect(onClose).toHaveBeenCalledTimes(1);
    });
  });

  it("reports the assets left without a description as a warning", async () => {
    vi.spyOn(API, "generateEpisodeAssets").mockResolvedValue(
      generation({
        description_filled: [],
        description_fill_failed: [{ asset_type: "prop", name: "玉佩", reason: "generation_failed" }],
        without_description: [
          { unit_id: "prop/玉佩", asset_type: "prop", name: "玉佩", derivative: null },
        ],
      }),
    );

    render(
      <EpisodeAssetGenerateDialog
        projectName="demo"
        episode={1}
        episodeTitle="第一集"
        plan={PLAN}
        onClose={vi.fn()}
        onSubmitted={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "确定" }));

    await waitFor(() => expect(useAppStore.getState().toast).not.toBeNull());
    // 生成描述失败是单资产失败：整批照常提交，缺描述的项如实提示、不静默吞掉。
    expect(useAppStore.getState().toast?.text).toContain("仍缺描述");
  });

  it("sends nothing when cancelled", () => {
    const generate = vi.spyOn(API, "generateEpisodeAssets").mockResolvedValue(generation());
    const onClose = vi.fn();

    render(
      <EpisodeAssetGenerateDialog
        projectName="demo"
        episode={1}
        episodeTitle="第一集"
        plan={PLAN}
        onClose={onClose}
        onSubmitted={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    expect(generate).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it("keeps tracking committed tasks when the project refresh fails", async () => {
    vi.spyOn(API, "generateEpisodeAssets").mockResolvedValue(generation());
    vi.mocked(API.getProject).mockRejectedValue(new Error("offline"));
    render(<EpisodeAssetGenerateDialog projectName="demo" episode={1} episodeTitle="第一集" plan={PLAN}
      onClose={vi.fn()} onSubmitted={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "确定" }));
    await waitFor(() => expect(useAppStore.getState().toast?.tone).toBe("warning"));
    expect(useAssetSheetBatchStore.getState().batches.b1?.members).toHaveLength(1);
    expect(useAppStore.getState().toast?.text).toContain("刷新");
  });
});
