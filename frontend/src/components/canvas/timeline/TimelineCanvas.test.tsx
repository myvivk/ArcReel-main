import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import { DEMO_PROJECT_NAME } from "@/onboarding/demo-project";
import { useProjectsStore } from "@/stores/projects-store";
import { useTasksStore } from "@/stores/tasks-store";
import { useWorkflowStore } from "@/stores/workflow-store";
import { makeDramaScene } from "@/test/factories";
import type { DramaEpisodeScript, ProjectData } from "@/types";
import { TimelineCanvas } from "./TimelineCanvas";

vi.mock("./ShotSplitView", () => ({
  ShotSplitView: ({
    onUpdatePrompt, onGenerateNarration, onInsertShot, onRemoveShot,
  }: {
    onUpdatePrompt?: unknown; onGenerateNarration?: unknown;
    onInsertShot?: (id: string, source?: string) => Promise<boolean>;
    onRemoveShot?: (id: string) => Promise<boolean>;
  }) => (
    <div data-testid="shots" data-editable={Boolean(onUpdatePrompt)} data-narration={Boolean(onGenerateNarration)}>
      <button onClick={() => { void onInsertShot?.("E1S01", "风停了。"); }}>insert</button>
      <button onClick={() => { void onRemoveShot?.("E1S01"); }}>remove</button>
    </div>
  ),
}));

const PROJECT: ProjectData = {
  title: "Demo", content_mode: "drama", generation_mode: "storyboard", style: "Anime",
  episodes: [{ episode: 1, title: "EP1", script_file: "scripts/episode_1.json" }], characters: {},
};
const SCRIPT: DramaEpisodeScript = {
  episode: 1, title: "EP1", content_mode: "drama",
  short_drama_meta: { source_path: "source/short_drama/episodes/episode_01.md" },
  scenes: [makeDramaScene()],
};
const DEFAULT = {
  view: "board", onViewChange: vi.fn(), projectName: "demo", episode: 1,
  hasShortDramaSource: true, episodeScript: SCRIPT, projectData: PROJECT, scriptFile: "episode_1.json",
} as const;

beforeEach(() => {
  useProjectsStore.setState(useProjectsStore.getInitialState(), true);
  useTasksStore.setState(useTasksStore.getInitialState(), true);
  useWorkflowStore.getState().resetTarget();
  vi.spyOn(API, "getShotGrid").mockResolvedValue(null);
});

describe("factory storyboard canvas", () => {
  it("renders AI storyboards and keeps the factory import and prompt entry points", () => {
    render(<TimelineCanvas {...DEFAULT} />);
    expect(screen.getByTestId("shots")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "编写提示词" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "补齐分镜图" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "AI 生成剧本" })).not.toBeInTheDocument();
  });

  it("returns to factory storyboarding when visual state disappears", () => {
    const { rerender } = render(<TimelineCanvas {...DEFAULT} />);
    rerender(<TimelineCanvas {...DEFAULT} episodeScript={null} />);
    expect(screen.queryByTestId("shots")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "AI 分镜" })).toBeInTheDocument();
  });

  it("forwards visual structure edits to the active episode script", () => {
    const insert = vi.fn().mockResolvedValue(true);
    const remove = vi.fn().mockResolvedValue(true);
    render(<TimelineCanvas {...DEFAULT} onInsertShot={insert} onRemoveShot={remove} />);
    fireEvent.click(screen.getByRole("button", { name: "insert" }));
    fireEvent.click(screen.getByRole("button", { name: "remove" }));
    expect(insert).toHaveBeenCalledWith("E1S01", "风停了。", "episode_1.json");
    expect(remove).toHaveBeenCalledWith("E1S01", "episode_1.json");
  });

  it("reads historical JSON without exposing visual generation or editing", () => {
    render(<TimelineCanvas {...DEFAULT} hasShortDramaSource={false} />);
    expect(screen.queryByTestId("shots")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "编写提示词" })).not.toBeInTheDocument();
    expect(screen.getByText(/source\/short_drama\/episodes\/episode_01.md/)).toBeInTheDocument();
  });

  it("keeps TTS generation available to factory projects and disables writes in the demo", () => {
    const writes = { onUpdatePrompt: vi.fn(), onGenerateNarration: vi.fn(), onGenerateEpisodeNarration: vi.fn() };
    const { rerender } = render(<TimelineCanvas {...DEFAULT} {...writes} />);
    expect(screen.getByTestId("shots")).toHaveAttribute("data-narration", "true");
    expect(screen.getByRole("button", { name: "补齐旁白配音" })).toBeInTheDocument();
    useProjectsStore.setState({ currentProjectName: DEMO_PROJECT_NAME });
    rerender(<TimelineCanvas {...DEFAULT} {...writes} />);
    expect(screen.getByTestId("shots")).toHaveAttribute("data-editable", "false");
    expect(screen.queryByRole("button", { name: "补齐旁白配音" })).not.toBeInTheDocument();
  });
});
