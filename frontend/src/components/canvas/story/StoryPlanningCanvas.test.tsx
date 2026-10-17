import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import type { StoryPlanningArtifacts } from "@/types/story-planning";
import { StoryPlanningCanvas } from "./StoryPlanningCanvas";

const source: StoryPlanningArtifacts = {
  character_bible: { path: "character-bible.md", revision: "r1", kind: "character-bible", episode: null, content: "林夏：记者" },
  series_outline: { path: "series-outline.md", revision: "r2", kind: "series-outline", episode: null, content: "寻找真相" },
  ledger: { path: "ledger.md", revision: "r3", kind: "ledger", episode: null, content: "第 1 集：雨夜" },
  episode_plans: [{ path: "episode-plan/episode_01.plan.md", revision: "r4", kind: "episode-plan", episode: 1, content: "门外重逢" }],
};

describe("StoryPlanningCanvas", () => {
  it("renders the four factory artifact readers", async () => {
    vi.spyOn(API, "getStoryPlanning").mockResolvedValue(source);
    render(<StoryPlanningCanvas projectName="demo" />);
    expect(await screen.findByRole("heading", { name: "故事规划" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "人物圣经" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "全剧大纲" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "连续性台账" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "分集规划" })).toBeInTheDocument();
  });
});
