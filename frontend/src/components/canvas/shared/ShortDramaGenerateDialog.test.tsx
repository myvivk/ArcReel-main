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

const emptyProject: ProjectData = {
  title: "短剧", style: "", content_mode: "drama", characters: {}, total_episodes: null,
  episodes: [],
};

describe("ShortDramaGenerateDialog", () => {
  it("已开写的系列不再询问题材与档位，单集重写沿用系列设定", async () => {
    useProjectsStore.getState().setCurrentProject("demo", project);
    useShortDramaGenerateStore.getState().open({ projectName: "demo", episode: 7 });
    vi.spyOn(API, "getStoryContext").mockResolvedValue({ context: "【已有工厂台账】伏笔尚未回收" });
    render(<ShortDramaGenerateHost projectName="demo" episode={7} />);
    expect(screen.queryByLabelText("题材 / 类型")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("篇幅档位")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("风格基调（可选）")).not.toBeInTheDocument();
    expect(screen.getByText(/系列设定已在首次交稿确定/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "交给 Agent 生成" }));
    await waitFor(() => expect(useShortDramaGenerateStore.getState().request).toBeNull());
    const prompt = useAssistantStore.getState().input;
    expect(prompt).toContain("不得重新立项");
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

  it("空项目首写仍可填写题材与篇幅档位", () => {
    useProjectsStore.getState().setCurrentProject("demo", emptyProject);
    useShortDramaGenerateStore.getState().open({ projectName: "demo", episode: 0 });
    render(<ShortDramaGenerateHost projectName="demo" episode={0} />);
    expect(screen.getByLabelText("题材 / 类型")).toBeInTheDocument();
    expect(screen.getByLabelText("篇幅档位")).toBeInTheDocument();
    expect(screen.queryByText(/系列设定/)).not.toBeInTheDocument();
  });

  it("只建了空集、还没交过稿时仍是首写，必须填题材才能交给 Agent", () => {
    useProjectsStore.getState().setCurrentProject("demo", {
      ...project,
      short_drama_genre: null,
      episodes: [{ episode: 1, title: "第一集", source_origin: "none", script_file: "" }],
    });
    useShortDramaGenerateStore.getState().open({ projectName: "demo", episode: 0 });
    render(<ShortDramaGenerateHost projectName="demo" episode={0} />);
    expect(screen.getByLabelText("题材 / 类型")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "交给 Agent 生成" })).toBeDisabled();
  });

  it("已保存的系列设定只读展示，不再提供选择", () => {
    useProjectsStore.getState().setCurrentProject("demo", {
      ...project,
      short_drama_genre: "悬疑",
      short_drama_tone: "冷冽压迫",
      short_drama_format: "long",
    });
    useShortDramaGenerateStore.getState().open({ projectName: "demo", episode: 7 });
    render(<ShortDramaGenerateHost projectName="demo" episode={7} />);
    expect(screen.queryByLabelText("题材 / 类型")).not.toBeInTheDocument();
    expect(screen.getByText("悬疑")).toBeInTheDocument();
    expect(screen.getByText("冷冽压迫")).toBeInTheDocument();
    expect(screen.getByText(/系列设定已保存到项目/)).toBeInTheDocument();
  });

  it("首写提交时把系列设定写回项目", async () => {
    useProjectsStore.getState().setCurrentProject("demo", {
      ...project,
      episodes: [{ episode: 7, title: "第七集", source_origin: "none", script_file: "" }],
    });
    useShortDramaGenerateStore.getState().open({ projectName: "demo", episode: 7 });
    const update = vi.spyOn(API, "updateProject").mockResolvedValue({ success: true, project: project });
    vi.spyOn(API, "getStoryContext").mockResolvedValue({ context: "" });
    vi.spyOn(useProjectsStore.getState(), "refreshProject").mockResolvedValue("success");
    render(<ShortDramaGenerateHost projectName="demo" episode={7} />);
    fireEvent.change(screen.getByLabelText("题材 / 类型"), { target: { value: "悬疑" } });
    fireEvent.click(screen.getByRole("button", { name: "交给 Agent 生成" }));
    await waitFor(() => expect(useShortDramaGenerateStore.getState().request).toBeNull());
    expect(update).toHaveBeenCalledWith(
      "demo",
      expect.objectContaining({ short_drama_genre: "悬疑", short_drama_format: "standard" }),
      expect.anything(),
    );
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

  it("续写提示词要求沿用已有设定，不重新立项", () => {
    const prompt = buildOriginalShortDramaPrompt({
      episodes: [8, 9],
      totalEpisodes: 24,
      genre: "",
      brief: "",
      tone: "",
      format: "standard",
      continuation: true,
    });
    expect(prompt).toContain("续写");
    expect(prompt).toContain("不得重新立项");
    expect(prompt).toContain("全剧规划总集数为 24 集（首写已定，本批不得改动）");
    expect(prompt).toContain("第 8、9 集");
    expect(prompt).not.toContain("【创意简报】");
    expect(prompt).not.toContain("题材 / 类型：");
  });

  it("批量续写仅可改连写集数，显示已有集数并沿用总集数", async () => {
    useProjectsStore.getState().setCurrentProject("demo", project);
    useShortDramaGenerateStore.getState().open({ projectName: "demo", episode: 0 });
    vi.spyOn(API, "getStoryContext").mockResolvedValue({ context: "【已有工厂台账】" });
    const updateProject = vi.spyOn(API, "updateProject");
    vi.spyOn(API, "createEpisode")
      .mockResolvedValueOnce({ episode: 8 })
      .mockResolvedValueOnce({ episode: 9 });
    vi.spyOn(useProjectsStore.getState(), "refreshProject").mockResolvedValue("success");
    render(<ShortDramaGenerateHost projectName="demo" episode={0} />);

    expect(screen.getByLabelText("已有集数")).toHaveValue(7);
    expect(screen.getByLabelText("总集数")).toHaveValue(24);
    expect(screen.getByLabelText("总集数")).toHaveAttribute("readonly");
    expect(screen.queryByLabelText("题材 / 类型")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("创意简报 / 梗概（可选）")).not.toBeInTheDocument();
    expect(screen.getByText(/当前已存在 7 集/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("连写集数"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "交给 Agent 生成" }));
    await waitFor(() => expect(useShortDramaGenerateStore.getState().request).toBeNull());
    expect(updateProject).not.toHaveBeenCalled();
    const prompt = useAssistantStore.getState().input;
    expect(prompt).toContain("续写");
    expect(prompt).toContain("不得重新立项");
    expect(prompt).toContain("全剧规划总集数为 24 集（首写已定，本批不得改动）");
  });

  it("续写连写集数超过剩余容量时不可提交", () => {
    useProjectsStore.getState().setCurrentProject("demo", {
      ...project,
      total_episodes: 8,
    });
    useShortDramaGenerateStore.getState().open({ projectName: "demo", episode: 0 });
    render(<ShortDramaGenerateHost projectName="demo" episode={0} />);
    // maxWritable = 8 - 7 = 1；键入 2 会被钳到 1，不应出现溢出文案，提交应可用
    fireEvent.change(screen.getByLabelText("连写集数"), { target: { value: "2" } });
    expect(screen.getByLabelText("连写集数")).toHaveValue(1);
    expect(screen.getByRole("button", { name: "交给 Agent 生成" })).toBeEnabled();
  });

  it("已达总集数时禁止续写", () => {
    useProjectsStore.getState().setCurrentProject("demo", {
      ...project,
      total_episodes: 7,
    });
    useShortDramaGenerateStore.getState().open({ projectName: "demo", episode: 0 });
    render(<ShortDramaGenerateHost projectName="demo" episode={0} />);
    expect(screen.getByText(/已达全剧规划总集数 7/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "交给 Agent 生成" })).toBeDisabled();
  });

  it("首写上下文加载失败时保留简报，不创建空集或交出不完整指令", async () => {
    useProjectsStore.getState().setCurrentProject("demo", emptyProject);
    useShortDramaGenerateStore.getState().open({ projectName: "demo", episode: 0 });
    useAssistantStore.getState().setInput("先前输入");
    vi.spyOn(API, "getStoryContext").mockRejectedValue(new Error("上下文不可读"));
    const create = vi.spyOn(API, "createEpisode");
    render(<ShortDramaGenerateHost projectName="demo" episode={0} />);
    fireEvent.change(screen.getByLabelText("题材 / 类型"), { target: { value: "悬疑" } });
    fireEvent.click(screen.getByRole("button", { name: "交给 Agent 生成" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "交给 Agent 生成" })).toBeEnabled());
    expect(screen.getByLabelText("题材 / 类型")).toHaveValue("悬疑");
    expect(useAssistantStore.getState().input).toBe("先前输入");
    expect(create).not.toHaveBeenCalled();
  });
});
