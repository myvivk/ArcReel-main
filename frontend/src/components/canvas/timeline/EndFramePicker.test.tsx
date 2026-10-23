import { fireEvent, render, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import { useProjectsStore } from "@/stores/projects-store";
import type { EpisodeScript, ProjectData } from "@/types";
import type { ShotGridPlan } from "@/types/shot-grid";
import { EndFramePicker } from "./EndFramePicker";

const PROJECT = "demo";
const SCRIPT = "episode_1.json";

const script = {
  episode: 1,
  title: "第一集",
  scenes: [
    {
      scene_id: "E1S01",
      novel_text: "",
      image_prompt: "",
      video_prompt: "",
      generated_assets: { storyboard_image: "storyboards/E1S01_v1.png" },
    },
    // 没有分镜图的分镜不进选项
    { scene_id: "E1S02", image_prompt: "", video_prompt: "" },
  ],
} as unknown as EpisodeScript;

function grid(cellPath: string): ShotGridPlan {
  return { id: "g1", episode: 1, script_file: SCRIPT, shot_id: "E1S01", rows: 2, cols: 2,
    composite_path: "shot_grids/g1.png", status: "confirmed", created_at: "", updated_at: "", error_message: null,
    cells: [
      { index: 0, prompt: "远景", time_ratio: 0, continuity_notes: "", image_path: cellPath, confirmed: true },
      { index: 1, prompt: "中景", time_ratio: 0.5, continuity_notes: "", image_path: null, confirmed: false },
    ],
  };
}

function renderPicker(props: Partial<Parameters<typeof EndFramePicker>[0]> = {}) {
  return render(
    <EndFramePicker
      projectName={PROJECT}
      scriptFile={SCRIPT}
      contentMode="drama"
      aspectRatio="9:16"
      onClose={vi.fn()}
      onPickProjectImage={vi.fn()}
      onPickUpload={vi.fn()}
      {...props}
    />,
  );
}

beforeEach(() => {
  vi.spyOn(API, "getShotGrid").mockImplementation(async (_project, _script, id) =>
    id === "E1S01" ? grid("shot_grids/g1_cell_0.png") : null,
  );
  useProjectsStore.setState({
    currentProjectName: PROJECT,
    currentScripts: { [SCRIPT]: script },
    currentProjectData: {
      characters: { 张三: { description: "", character_sheet: "characters/zhangsan.png" } },
      scenes: { 客厅: { description: "", scene_sheet: "scenes/living_room.png" } },
    } as unknown as ProjectData,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  useProjectsStore.setState(useProjectsStore.getInitialState(), true);
});

describe("EndFramePicker 项目内通道", () => {
  it("按来源分组：本集分镜图 / 本集分镜切图", async () => {
    const { findByText, getByText, queryByRole, queryByText } = renderPicker();

    await findByText("本集分镜切图");
    expect(getByText("本集分镜图")).toBeInTheDocument();

    // 无分镜图的分镜不出现
    expect(queryByRole("button", { name: /分镜 S02/ })).not.toBeInTheDocument();

    // 角色/场景分组已移除：即使 currentProjectData 里有对应素材也不展示
    expect(queryByText("角色")).not.toBeInTheDocument();
    expect(queryByText("场景")).not.toBeInTheDocument();
  });

  it("宫格切图只取本集、且跳过未切出图的格子", async () => {
    const { findByRole, queryByRole } = renderPicker();

    expect(await findByRole("button", { name: /2×2 第 1 格/ })).toBeInTheDocument();
    expect(queryByRole("button", { name: /2×2 第 2 格/ })).not.toBeInTheDocument();
  });

  it("选中后确认，把项目内相对路径交回父级", async () => {
    const onPickProjectImage = vi.fn();
    const { getByRole, findByRole } = renderPicker({ onPickProjectImage });

    const cell = await findByRole("button", { name: /分镜 S01/ });
    fireEvent.click(cell);
    expect(cell).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(getByRole("button", { name: "设为尾帧" }));
    expect(onPickProjectImage).toHaveBeenCalledWith("storyboards/E1S01_v1.png");
  });

  it("未选中时确认不可用", async () => {
    const { getByRole, findByText } = renderPicker();
    await findByText("本集分镜图");
    expect(getByRole("button", { name: "设为尾帧" })).toBeDisabled();
  });
});

describe("EndFramePicker 上传通道", () => {
  it("选定文件后交回父级（与项目内通道同一落点）", async () => {
    const onPickUpload = vi.fn();
    // Dialog 走 portal，file input 不在 render 的 container 子树内
    const { baseElement, findByText } = renderPicker({ onPickUpload });
    await findByText("本集分镜图");

    const input = baseElement.querySelector<HTMLInputElement>('input[type="file"]');
    expect(input).not.toBeNull();
    const file = new File(["x"], "end.png", { type: "image/png" });
    fireEvent.change(input!, { target: { files: [file] } });
    expect(onPickUpload).toHaveBeenCalledWith(file);
  });

  it("提交在途时两个通道的入口都不可点", async () => {
    const { getByRole, findByText } = renderPicker({ submitting: true });
    await findByText("本集分镜图");

    expect(getByRole("button", { name: /上传/ })).toBeDisabled();
    expect(getByRole("button", { name: /设置中/ })).toBeDisabled();
  });

  it("禁用态（能力不支持 / 占用）下上传与确认不可点，取消仍可点", async () => {
    const { getByRole, findByRole, findByText } = renderPicker({ disabled: true });
    await findByText("本集分镜图");

    const cell = await findByRole("button", { name: /分镜 S01/ });
    fireEvent.click(cell);

    expect(getByRole("button", { name: /上传/ })).toBeDisabled();
    expect(getByRole("button", { name: "设为尾帧" })).toBeDisabled();
    expect(getByRole("button", { name: "取消" })).toBeEnabled();
  });

  it("提交在途时忽略 Esc 关闭，落定后可以关掉", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    const { findByText, rerender } = renderPicker({ onClose, submitting: true });
    await findByText("本集分镜图");

    await user.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();

    rerender(
      <EndFramePicker
        projectName={PROJECT}
        scriptFile={SCRIPT}
        contentMode="drama"
        aspectRatio="9:16"
        onClose={onClose}
        onPickProjectImage={vi.fn()}
        onPickUpload={vi.fn()}
      />,
    );
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("宫格接口失败不阻断其余分组", async () => {
    vi.spyOn(API, "getShotGrid").mockRejectedValue(new Error("boom"));
    const { findByText, queryByText } = renderPicker();

    await findByText("本集分镜图");
    await waitFor(() => {
      expect(queryByText("本集分镜切图")).not.toBeInTheDocument();
    });
  });
});
