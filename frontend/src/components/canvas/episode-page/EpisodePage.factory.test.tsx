import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import { LeaveGuardProvider } from "@/components/shared/edit-unit/LeaveGuard";
import type { ProjectData } from "@/types";
import { EpisodePage } from "./EpisodePage";

const project: ProjectData = {
  title: "短剧", style: "", content_mode: "drama", characters: {},
  episodes: [{ episode: 1, title: "第一集", source_origin: "own", source_kind: "screenplay", script_file: "" }],
};

function renderEpisode() {
  vi.spyOn(API, "getWorkflowPlan").mockRejectedValue(new Error("workflow unavailable"));
  return render(
    <LeaveGuardProvider>
      <EpisodePage projectName="demo" episode={1} projectData={project} script={null} demo={false}
        onSaveTitle={vi.fn()} onViewUnit={vi.fn()} renderCanvas={() => <p>分镜画布</p>} />
    </LeaveGuardProvider>,
  );
}

describe("EpisodePage 工厂原文", () => {
  it("没有视觉脚本时优先只读显示完整工厂原文", async () => {
    const markdown = "[人物]：林夏（主角｜短发）\n[场景]：旧楼\n[道具]：钥匙｜铜色\n△ 敲门。\n林夏：谁？";
    vi.spyOn(API, "getShortDramaSource").mockResolvedValue({
      episode: 1, path: "source/short_drama/episodes/episode_01.md", markdown, revision: "r1", exists: true,
    });
    renderEpisode();
    const original = await screen.findByText(markdown, { collapseWhitespace: false });
    expect(original).toHaveTextContent(markdown, { normalizeWhitespace: false });
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByText("分镜画布")).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "脚本规划" })).not.toBeInTheDocument();
  });

  it("没有工厂工件时提供工厂生成和导入，不进入原文确认", async () => {
    vi.spyOn(API, "getShortDramaSource").mockResolvedValue({
      episode: 1, path: "source/short_drama/episodes/episode_01.md", markdown: "", revision: null, exists: false,
    });
    const readLegacy = vi.spyOn(API, "getSourceContent");
    renderEpisode();
    expect(await screen.findByRole("button", { name: "AI 原创剧本" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "本集原文" })).not.toBeInTheDocument();
    expect(readLegacy).not.toHaveBeenCalled();
  });
});
