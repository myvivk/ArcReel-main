import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { API, ApiRequestError } from "@/api";
import { usePromptAuthoringStore, type PromptAuthoringOpenRequest } from "@/stores/prompt-authoring-store";
import { useTasksStore } from "@/stores/tasks-store";
import type { PromptOverwrite } from "@/types";
import { PromptAuthoringHost } from "./PromptAuthoringDialog";

const ACCEPTED = { batch: { members: [] } } as unknown as Awaited<ReturnType<typeof API.authorPrompts>>;

const SCRIPT = {
  scenes: [
    { scene_id: "E1S01", image_prompt: "", video_prompt: "", pending_authoring: true },
    { scene_id: "E1S02", image_prompt: "雨夜", video_prompt: "推近" },
    { scene_id: "E1S03", image_prompt: "", video_prompt: "", pending_authoring: true },
  ],
};

function openDialog(
  scope: "pending" | "current" | "custom" = "pending",
  request: Partial<PromptAuthoringOpenRequest> = {},
) {
  usePromptAuthoringStore.getState().open({ projectName: "proj", episode: 1, scope, ...request });
  render(<PromptAuthoringHost projectName="proj" episode={1} script={SCRIPT} />);
}

beforeEach(() => {
  useTasksStore.getState().setTasks([]);
});

afterEach(() => {
  vi.restoreAllMocks();
  usePromptAuthoringStore.getState().close();
});

describe("编写提示词", () => {
  it("自选多条只提交勾选的条目，按剧本顺序", async () => {
    const user = userEvent.setup();
    const submit = vi.spyOn(API, "authorPrompts").mockResolvedValue(ACCEPTED);
    openDialog("custom");

    const dialog = await screen.findByRole("dialog", { name: "编写提示词" });
    const list = within(dialog).getByRole("list", { name: "自选多条" });
    // 默认勾选待编写的 E1S01、E1S03；再勾上 E1S02，去掉 E1S01
    await user.click(within(list).getByRole("checkbox", { name: /E1S02/ }));
    await user.click(within(list).getByRole("checkbox", { name: /E1S01/ }));
    await user.click(within(dialog).getByRole("button", { name: "AI 编写" }));

    await waitFor(() =>
      expect(submit).toHaveBeenCalledWith("proj", 1, {
        entry_ids: ["E1S02", "E1S03"],
        rewrite: false,
        fields: ["image_prompt"],
        instructions: null,
        overwrite_revision: null,
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("重写会覆盖已有提示词时先确认，确认后带着清单版本重新提交", async () => {
    const user = userEvent.setup();
    const overwrite: PromptOverwrite = { revision: "rev-7", entries: [{ id: "E1S02", fields: ["image_prompt"] }], text: "将覆盖 E1S02 的分镜图与视频提示词。" };
    const submit = vi
      .spyOn(API, "authorPrompts")
      .mockRejectedValueOnce(new ApiRequestError("需要确认覆盖", { prompt_overwrite: overwrite }, 409))
      .mockResolvedValueOnce(ACCEPTED);
    openDialog();

    const dialog = await screen.findByRole("dialog", { name: "编写提示词" });
    await user.click(within(dialog).getByRole("checkbox", { name: "覆盖已有提示词" }));
    await user.click(within(dialog).getByRole("button", { name: "AI 重写" }));

    const confirm = await screen.findByRole("alertdialog", { name: "确认覆盖已有提示词" });
    expect(within(confirm).getByText(/覆盖 S02 的分镜图/)).toBeInTheDocument();
    await user.click(within(confirm).getByRole("button", { name: "AI 重写" }));

    await waitFor(() =>
      expect(submit).toHaveBeenLastCalledWith("proj", 1, {
        entry_ids: null,
        rewrite: true,
        fields: ["image_prompt"],
        instructions: null,
        overwrite_revision: "rev-7",
      }),
    );
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  });

  it("当前条目可默认打开为 AI 重写，用于标准化已有提示词", async () => {
    const user = userEvent.setup();
    const submit = vi.spyOn(API, "authorPrompts").mockResolvedValue(ACCEPTED);
    openDialog("current", { currentEntryId: "E1S02", defaultRewrite: true });

    const dialog = await screen.findByRole("dialog", { name: "编写提示词" });
    expect(within(dialog).getByRole("checkbox", { name: "覆盖已有提示词" })).toBeChecked();
    await user.click(within(dialog).getByRole("button", { name: "AI 重写" }));

    await waitFor(() =>
      expect(submit).toHaveBeenCalledWith("proj", 1, {
        entry_ids: ["E1S02"],
        rewrite: true,
        fields: ["image_prompt"],
        instructions: null,
        overwrite_revision: null,
      }),
    );
  });

  it("当前条目入口可锁定为只编写视频提示词", async () => {
    const user = userEvent.setup();
    const submit = vi.spyOn(API, "authorPrompts").mockResolvedValue(ACCEPTED);
    openDialog("current", {
      currentEntryId: "E1S02",
      defaultRewrite: true,
      defaultFields: ["video_prompt"],
      lockFields: true,
    });

    const dialog = await screen.findByRole("dialog", { name: "编写提示词" });
    expect(within(dialog).getByText("视频提示词")).toBeInTheDocument();
    expect(within(dialog).queryByRole("radio", { name: "分镜提示词" })).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "AI 重写" }));

    await waitFor(() =>
      expect(submit).toHaveBeenCalledWith("proj", 1, {
        entry_ids: ["E1S02"],
        rewrite: true,
        fields: ["video_prompt"],
        instructions: null,
        overwrite_revision: null,
      }),
    );
  });
});
