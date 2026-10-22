import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import type { ShotGridPlan } from "@/types/shot-grid";
import type { ShotGridDialogRequest } from "@/stores/shot-grid-dialog-store";
import { ShotGridDialog } from "./ShotGridDialog";
import { useProjectsStore } from "@/stores/projects-store";
import { useTasksStore } from "@/stores/tasks-store";

const PROJECT = "demo";
const SCRIPT = "episode_1.json";

function plan(overrides: Partial<ShotGridPlan> = {}): ShotGridPlan {
  return {
    id: "shot_grid_123456789abc",
    episode: 1,
    script_file: SCRIPT,
    shot_id: "E1S01",
    rows: 2,
    cols: 2,
    cells: Array.from({ length: 4 }, (_, index) => ({
      index,
      prompt: "",
      time_ratio: index / 3,
      continuity_notes: "",
      confirmed: false,
      image_path: null,
    })),
    prompt: "第1格（远景）：雨夜街道\n第2格（中景）：霓虹灯下\n第3格（近景）：行人驻足\n第4格（特写）：水洼倒影",
    composite_path: null,
    status: "draft",
    task_id: null,
    created_at: "",
    updated_at: "",
    error_message: null,
    ...overrides,
  };
}

function request(overrides: Partial<ShotGridDialogRequest> = {}): ShotGridDialogRequest {
  return {
    projectName: PROJECT,
    scriptFile: SCRIPT,
    episode: 1,
    shotId: "E1S01",
    plan: null,
    ...overrides,
  };
}

/** 逐格输入框：Label 挂在 Textarea 上，按可及名称取。 */
function promptBox(): HTMLElement {
  return screen.getByLabelText(/逐格镜头提示词/);
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(useProjectsStore.getState(), "refreshProject").mockResolvedValue("success");
  useTasksStore.setState({ tasks: [], optimisticActive: new Set(), optimisticActiveScriptFile: new Set() });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ShotGridDialog", () => {
  it("打开时回填已有记录，AI 生成只回填可编辑文本，不写入或入队", async () => {
    const existing = plan({ rows: 2, cols: 3 });
    const authored = "第1格（远景）：雨夜街道\n第2格（中景）：霓虹灯下\n第3格（近景）：行人驻足\n第4格（特写）：水洼倒影\n第5格（远景）：街角\n第6格（中景）：橱窗";
    vi.spyOn(API, "authorShotGridPrompt").mockResolvedValue({ prompt: authored });
    const save = vi.spyOn(API, "saveShotGrid");
    const generate = vi.spyOn(API, "generateShotGrid");

    render(<ShotGridDialog request={request({ plan: existing })} onClose={vi.fn()} />);

    // 规格随记录选中 2×3，输入框里是这条记录已写成的逐格文本
    expect(screen.getByRole("button", { name: "2×3" })).toHaveAttribute("aria-pressed", "true");
    expect(promptBox()).toHaveValue(existing.prompt);
    expect(screen.getByRole("button", { name: "确定并生成" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "AI 生成" }));

    await waitFor(() => expect(promptBox()).toHaveValue(authored));
    expect(API.authorShotGridPrompt).toHaveBeenCalledWith(PROJECT, {
      script_file: SCRIPT,
      episode: 1,
      shot_id: "E1S01",
      rows: 2,
      cols: 3,
    }, { signal: expect.any(AbortSignal) });
    expect(save).not.toHaveBeenCalled();
    expect(generate).not.toHaveBeenCalled();
    expect(promptBox()).toBeEnabled();
  });

  it("按写出的格数提示空、不足与超出", () => {
    render(<ShotGridDialog request={request()} onClose={vi.fn()} />);

    // 首次生成默认 2×2，四格
    expect(screen.getByRole("button", { name: "2×2" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "确定并生成" })).toBeEnabled();

    fireEvent.change(promptBox(), { target: { value: "第1格（远景）：雨夜街道\n第2格（中景）：霓虹灯下" } });
    expect(screen.getByRole("button", { name: "确定并生成" })).toBeDisabled();

    fireEvent.change(promptBox(), {
      target: { value: "第1格（远景）：雨夜街道\n第2格（中景）：霓虹灯下\n第3格（近景）：行人驻足\n第4格（特写）：水洼倒影" },
    });
    expect(screen.getByText("已写满 4 格。")).toBeInTheDocument();

    fireEvent.change(promptBox(), {
      target: { value: "第1格（远景）：雨夜街道\n第2格（中景）：霓虹灯下\n第3格（近景）：行人驻足\n第4格（特写）：水洼倒影\n第5格（远景）：街角" },
    });
    expect(screen.getByRole("button", { name: "确定并生成" })).toBeDisabled();
  });

  it("确定 = 保存配置并入队；没有改动时取消不写入任何东西", async () => {
    const save = vi.spyOn(API, "saveShotGrid").mockResolvedValue(plan());
    const generate = vi.spyOn(API, "generateShotGrid").mockResolvedValue({ task_id: "t1", status: "queued" });
    const onClose = vi.fn();

    const { unmount } = render(<ShotGridDialog request={request()} onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: "3×3" }));
    fireEvent.click(screen.getByRole("button", { name: "确定并生成" }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(save).toHaveBeenCalledWith(PROJECT, {
      script_file: SCRIPT,
      episode: 1,
      shot_id: "E1S01",
      rows: 3,
      cols: 3,
      prompt: "",
    });
    expect(generate).toHaveBeenCalledWith(PROJECT, SCRIPT, "E1S01");
    unmount();

    const saveAgain = vi.spyOn(API, "saveShotGrid").mockResolvedValue(plan());
    const generateAgain = vi.spyOn(API, "generateShotGrid").mockResolvedValue({ task_id: "t2", status: "queued" });
    // 同一个方法上二次 spy 拿到的是同一个替身，先清掉上一段的调用记录再断言「什么都没写」。
    saveAgain.mockClear();
    generateAgain.mockClear();
    const cancelClose = vi.fn();
    render(<ShotGridDialog request={request({ plan: plan() })} onClose={cancelClose} />);
    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    expect(cancelClose).toHaveBeenCalled();
    expect(saveAgain).not.toHaveBeenCalled();
    expect(generateAgain).not.toHaveBeenCalled();
  });

  it("手改文本后取消不写入", () => {
    const save = vi.spyOn(API, "saveShotGrid");
    const generate = vi.spyOn(API, "generateShotGrid");
    const onClose = vi.fn();

    render(<ShotGridDialog request={request({ plan: plan() })} onClose={onClose} />);
    fireEvent.change(promptBox(), { target: { value: "第1格（远景）：雨夜街道\n第2格（中景）：霓虹灯下" } });
    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    expect(save).not.toHaveBeenCalled();
    expect(generate).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it("已确认的参考素材锁死规格，只能改提示词重新生成", () => {
    render(<ShotGridDialog request={request({ plan: plan({ status: "confirmed" }) })} onClose={vi.fn()} />);

    expect(screen.getByRole("button", { name: "2×2" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "3×3" })).toBeDisabled();
    expect(screen.getByText("已确认的参考素材锁定规格；要换规格请先取消确认。")).toBeInTheDocument();
    expect(promptBox()).toBeEnabled();
    expect(screen.getByRole("button", { name: "确定并生成" })).toBeEnabled();
  });

  it("生成中禁改规格，也不能再次提交", () => {
    render(<ShotGridDialog request={request({ plan: plan({ status: "generating" }) })} onClose={vi.fn()} />);

    expect(screen.getByRole("button", { name: "2×2" })).toBeDisabled();
    expect(screen.getByText("正在生成中，生成完成后可以再次配置。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确定并生成" })).toBeDisabled();
  });

  it("逐格多行正文与重复序号按同一格解析", () => {
    render(<ShotGridDialog request={request()} onClose={vi.fn()} />);
    fireEvent.change(promptBox(), { target: { value: `${plan().prompt}\n同一格的光线描述。\n第4格：重复格` } });
    expect(screen.getByRole("button", { name: "确定并生成" })).toBeEnabled();
    fireEvent.change(promptBox(), { target: { value: "第1格：\n第2格：雨夜" } });
    expect(screen.getByRole("button", { name: "确定并生成" })).toBeDisabled();
  });
});
