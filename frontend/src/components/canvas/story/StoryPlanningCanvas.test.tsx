import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import { useProjectsStore } from "@/stores/projects-store";
import type { StorySnapshot } from "@/types/story-planning";
import { StoryPlanningCanvas } from "./StoryPlanningCanvas";

const source: StorySnapshot = {
  revision: "r1", stale_ids: [],
  plan: { premise: "寻找真相", context_budget: 2400, records: [{
    id: "e1", kind: "event", text: "找到信件", episode: 1, status: "pending",
    characters: [], source_file: "episode_1.json", source_id: "E1S01", source_revision: "s1",
  }] },
};

describe("StoryPlanningCanvas", () => {
  it("keeps confirmation unsaved until explicit save and submits the current revision", async () => {
    useProjectsStore.setState({ currentProjectData: null });
    vi.spyOn(API, "getStoryPlanning").mockResolvedValue(source);
    const saved = structuredClone(source);
    saved.revision = "r2";
    saved.plan.records[0].status = "confirmed";
    const save = vi.spyOn(API, "saveStoryPlanning").mockResolvedValue(saved);
    const user = userEvent.setup();
    render(<StoryPlanningCanvas projectName="demo" />);
    await screen.findByRole("heading", { name: "故事规划" });
    await user.click(screen.getByRole("tab", { name: "分集剧情" }));
    expect(screen.getByRole("textbox", { name: "记录内容" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "确认记录" }));
    expect(save).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "从剧本提取事件" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith(
      "demo", saved.plan, "r1", expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ));
    expect(await screen.findByRole("button", { name: "标为待确认" })).toBeEnabled();
  });

  it("shows changed sources in review and prevents confirming them", async () => {
    vi.spyOn(API, "getStoryPlanning").mockResolvedValue({ ...source, stale_ids: ["e1"] });
    const user = userEvent.setup();
    render(<StoryPlanningCanvas projectName="demo" />);
    await screen.findByRole("heading", { name: "故事规划" });
    await user.click(screen.getByRole("tab", { name: "待核对 (1)" }));
    expect(screen.getByText("来源已变化")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认记录" })).toBeDisabled();
    expect(screen.getByRole("link", { name: "查看来源集" })).toHaveAttribute("href", "/episodes/1");
  });
});
