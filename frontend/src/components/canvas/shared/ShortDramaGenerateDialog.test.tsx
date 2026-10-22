import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import { useProjectsStore } from "@/stores/projects-store";
import { useAssistantStore } from "@/stores/assistant-store";
import { useShortDramaGenerateStore } from "@/stores/short-drama-generate-store";
import type { ProjectData } from "@/types";
import { buildOriginalShortDramaPrompt, ShortDramaGenerateHost } from "./ShortDramaGenerateDialog";

const project: ProjectData = {
  title: "短剧", style: "", content_mode: "drama", characters: {}, total_episodes: 24,
  episodes: [{ episode: 7, title: "第七集", source_origin: "own", script_file: "" }],
};

describe("ShortDramaGenerateDialog", () => {
  it("单集续写使用项目总集数及工厂上下文，交给独立生成与质量链路", async () => {
    useProjectsStore.getState().setCurrentProject("demo", project);
    useShortDramaGenerateStore.getState().open({ projectName: "demo", episode: 7 });
    vi.spyOn(API, "getStoryContext").mockResolvedValue({ context: "【已有工厂台账】伏笔尚未回收" });
    render(<ShortDramaGenerateHost projectName="demo" episode={7} />);
    fireEvent.change(screen.getByLabelText("题材 / 类型"), { target: { value: "悬疑" } });
    fireEvent.click(screen.getByRole("button", { name: "交给 Agent 生成" }));
    await waitFor(() => expect(useShortDramaGenerateStore.getState().request).toBeNull());
    const prompt = useAssistantStore.getState().input;
    expect(prompt).toContain("全剧规划总集数为 24 集");
    expect(prompt).toContain("本批次未达全剧结尾");
    expect(prompt).toContain("【已有工厂台账】伏笔尚未回收");
    expect(prompt).toContain("generate_short_drama_factory");
    expect(prompt).toContain("AI 医生精修");
    expect(prompt).toContain("validate_series.py");
    expect(prompt).toContain("合规检查");
    expect(prompt).toContain("每个单元或批次通过后立即");
    expect(prompt).toContain("ingest_short_drama_artifacts");
  });

  it("最后一集要求收尾，长档与漫剧仍保留各自篇幅", () => {
    for (const format of ["long", "manju"] as const) {
      const prompt = buildOriginalShortDramaPrompt({
        episodes: [24], totalEpisodes: 24, genre: "悬疑", brief: "", tone: "", format,
      });
      expect(prompt).toContain("最后一集必须收尾闭环");
      expect(prompt).toContain(format === "long" ? "165~195 秒" : "60~90 秒");
    }
  });

  it("上下文加载失败时保留简报，不创建空集或交出不完整指令", async () => {
    useProjectsStore.getState().setCurrentProject("demo", project);
    useShortDramaGenerateStore.getState().open({ projectName: "demo", episode: 0 });
    useAssistantStore.getState().setInput("先前输入");
    vi.spyOn(API, "getStoryContext").mockRejectedValue(new Error("上下文不可读"));
    const create = vi.spyOn(API, "createEpisode");
    render(<ShortDramaGenerateHost projectName="demo" episode={7} />);
    fireEvent.change(screen.getByLabelText("题材 / 类型"), { target: { value: "悬疑" } });
    fireEvent.click(screen.getByRole("button", { name: "交给 Agent 生成" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "交给 Agent 生成" })).toBeEnabled());
    expect(screen.getByLabelText("题材 / 类型")).toHaveValue("悬疑");
    expect(useAssistantStore.getState().input).toBe("先前输入");
    expect(create).not.toHaveBeenCalled();
  });
});
