import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import type { ShotGridPlan } from "@/types/shot-grid";
import { ReferenceMaterialsSection } from "./ReferenceMaterialsSection";
import { useProjectsStore } from "@/stores/projects-store";
import { useTasksStore } from "@/stores/tasks-store";

const PROJECT = "demo";

function plan(overrides: Partial<ShotGridPlan> = {}): ShotGridPlan {
  return {
    id: "shot_grid_123456789abc",
    episode: 1,
    script_file: "episode_1.json",
    shot_id: "E1S01",
    rows: 2,
    cols: 2,
    cells: [],
    prompt: "",
    composite_path: "shot_grids/shot_grid_123456789abc.png",
    status: "completed",
    task_id: null,
    created_at: "",
    updated_at: "",
    error_message: null,
    ...overrides,
  };
}

function renderSection(readOnly = false) {
  return render(
    <ReferenceMaterialsSection
      projectName={PROJECT}
      scriptFile="episode_1.json"
      episode={1}
      shotId="E1S01"
      readOnly={readOnly}
      beforeGenerate={async () => true}
      onPlanChange={vi.fn()}
      onBusyChange={vi.fn()}
    />,
  );
}

beforeEach(() => {
  vi.spyOn(useProjectsStore.getState(), "refreshProject").mockResolvedValue("success");
  useTasksStore.setState({ tasks: [], optimisticActive: new Set(), optimisticActiveScriptFile: new Set() });
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("ReferenceMaterialsSection", () => {
  it("宫格图缩略图按容器等比缩放，点击进查看器", async () => {
    vi.spyOn(API, "getShotGrid").mockResolvedValue(plan());

    renderSection();

    const thumbnail = await screen.findByRole("button", { name: "点击查看大图" });
    const image = thumbnail.querySelector("img");
    expect(image).toHaveClass("size-full", "object-contain");

    fireEvent.click(thumbnail);

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "镜头关键帧宫格图" })).toBeInTheDocument();
  });

  it("未确认时只提供确认入口，不提供视频提示词入口", async () => {
    vi.spyOn(API, "getShotGrid").mockResolvedValue(plan());
    renderSection();
    expect(await screen.findByRole("button", { name: "确认参考素材" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "生成视频提示词" })).not.toBeInTheDocument();
  });

  it("确认失败显示原因，保留宫格和重试入口", async () => {
    vi.spyOn(API, "getShotGrid").mockResolvedValue(plan());
    vi.spyOn(API, "confirmShotGrid").mockRejectedValue(new Error("切分失败"));
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "确认参考素材" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("切分失败");
    expect(screen.getByRole("button", { name: "确认参考素材" })).toBeEnabled();
  });

  it("取消确认后关闭视频提示词入口，允许重新确认", async () => {
    const get = vi.spyOn(API, "getShotGrid").mockResolvedValue(plan({ status: "confirmed" }));
    const unconfirm = vi.spyOn(API, "unconfirmShotGrid").mockImplementation(async () => {
      get.mockResolvedValue(plan());
      return { success: true, grid: plan() };
    });
    renderSection();
    expect(await screen.findByRole("button", { name: "生成视频提示词" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "取消确认" }));
    await waitFor(() => expect(unconfirm).toHaveBeenCalledWith(PROJECT, "shot_grid_123456789abc"));
    expect(await screen.findByRole("button", { name: "确认参考素材" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "生成视频提示词" })).not.toBeInTheDocument();
  });

  it("只读页面可看结果，不提供确认或生成操作", async () => {
    vi.spyOn(API, "getShotGrid").mockResolvedValue(plan());
    renderSection(true);
    expect(await screen.findByRole("button", { name: "点击查看大图" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "确认参考素材" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "配置参考素材" })).not.toBeInTheDocument();
  });
});
