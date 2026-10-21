import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { Router } from "wouter";
import { API } from "@/api";
import { useProjectsStore } from "@/stores/projects-store";
import { ShortDramaStoryboardButton } from "./ShortDramaStoryboardButton";

const SOURCE = {
  episode: 3,
  path: "source/short_drama/episodes/episode_03.md",
  markdown: "第【3】集：仓库\n[道具]：铁门｜锈迹斑斑\n△ 推门。",
  revision: "rev-3",
  exists: true,
};

const MISSING = [
  { type: "character", name: "林辰", reason: "source_missing" },
  { type: "prop", name: "铁门", reason: "source_missing" },
];

function renderButton(storyboardCount = 0) {
  return render(
    <Router>
      <ShortDramaStoryboardButton
        projectName="proj"
        episode={3}
        storyboardCount={storyboardCount}
      />
    </Router>,
  );
}

async function openDialog() {
  fireEvent.click(screen.getByRole("button", { name: "AI 分镜" }));
  return await screen.findByRole("dialog");
}

beforeEach(() => {
  vi.spyOn(API, "getShortDramaSource").mockResolvedValue(SOURCE);
  vi.spyOn(useProjectsStore.getState(), "refreshProject").mockResolvedValue("success");
  vi.spyOn(API, "reimportShortDramaSource").mockResolvedValue({
    episode: 3,
    scenes: 5,
    source_path: SOURCE.path,
    source_revision: SOURCE.revision,
    assets_without_description: MISSING,
    planning_strength: 2,
    planning_cached: false,
  });
  vi.spyOn(API, "fillAssetFields").mockResolvedValue({ success: true });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("AI 分镜弹窗", () => {
  it("把集号与源稿体量插值进标题与摘要，不留原始占位符", async () => {
    renderButton();
    const dialog = await openDialog();

    expect(within(dialog).getByText("AI 分镜 · 第 3 集")).toBeInTheDocument();
    expect(
      within(dialog).getByText(/source\/short_drama\/episodes\/episode_03\.md：3 行 \/ \d+ 字/),
    ).toBeInTheDocument();
    expect(within(dialog).queryByText(/\{episode\}/)).not.toBeInTheDocument();
  });

  it("默认勾选补齐：逐个调用 AI 补全并显示补齐结果", async () => {
    renderButton();
    const dialog = await openDialog();
    fireEvent.click(within(dialog).getByRole("button", { name: "开始分镜" }));

    await waitFor(() => expect(screen.getByText("已生成 5 个分镜")).toBeInTheDocument());
    await waitFor(() =>
      expect(API.fillAssetFields).toHaveBeenCalledWith("proj", "character", "林辰"),
    );
    expect(API.fillAssetFields).toHaveBeenCalledWith("proj", "prop", "铁门");
    expect(API.fillAssetFields).toHaveBeenCalledTimes(2);
    await waitFor(() =>
      expect(screen.getByText("已用 AI 补齐 2 个资产描述")).toBeInTheDocument(),
    );
    expect(screen.queryByText(/个资产缺描述/)).not.toBeInTheDocument();
  });

  it("取消勾选时不动资产，只给出缺描述黄条与补描述入口", async () => {
    renderButton();
    const dialog = await openDialog();
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "同时用 AI 补齐缺描述资产" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "开始分镜" }));

    await waitFor(() =>
      expect(API.reimportShortDramaSource).toHaveBeenCalledWith("proj", 3, "rev-3", false, 2, false, false),
    );
    expect(API.fillAssetFields).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.getByText(/2 个资产缺描述：林辰（角色）、铁门（道具）/)).toBeInTheDocument(),
    );
    expect(screen.getByRole("link", { name: "去补描述" })).toHaveAttribute(
      "href",
      "/characters?focus=%E6%9E%97%E8%BE%B0",
    );
  });

  it("补全失败的资产留在黄条里，不吞掉分镜结果", async () => {
    vi.mocked(API.fillAssetFields).mockImplementation(async (_project, assetType) => {
      if (assetType === "prop") throw new Error("model unavailable");
      return { success: true };
    });
    renderButton();
    const dialog = await openDialog();
    fireEvent.click(within(dialog).getByRole("button", { name: "开始分镜" }));

    await waitFor(() =>
      expect(screen.getByText("已用 AI 补齐 1 个资产描述")).toBeInTheDocument(),
    );
    expect(screen.getByText(/1 个资产缺描述：铁门（道具）/)).toBeInTheDocument();
    expect(screen.getByText("已生成 5 个分镜")).toBeInTheDocument();
  });

  it("已有分镜时叫「AI 重新分镜」，未勾选覆盖不能提交", async () => {
    renderButton(4);
    fireEvent.click(screen.getByRole("button", { name: "AI 重新分镜" }));
    const dialog = await screen.findByRole("dialog");

    expect(within(dialog).getByText("AI 重新分镜 · 第 3 集")).toBeInTheDocument();
    const submit = within(dialog).getByRole("button", { name: "重新分镜" });
    expect(submit).toBeDisabled();

    fireEvent.click(within(dialog).getByRole("checkbox", { name: "我确认整体替换本集分镜" }));
    expect(submit).toBeEnabled();
    fireEvent.click(submit);
    await waitFor(() =>
      expect(API.reimportShortDramaSource).toHaveBeenCalledWith("proj", 3, "rev-3", true, 2, false, false),
    );
  });

  it("内容优化强度默认「标准」，点「增强」后按 3 提交", async () => {
    renderButton();
    const dialog = await openDialog();

    expect(within(dialog).getByRole("button", { name: "标准" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(within(dialog).getByRole("button", { name: "增强" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "开始分镜" }));

    await waitFor(() =>
      expect(API.reimportShortDramaSource).toHaveBeenCalledWith("proj", 3, "rev-3", false, 3, false, false),
    );
  });

  it("AI 规划不合格回落机械切分时，把原因写进弹窗而不是报错", async () => {
    vi.mocked(API.reimportShortDramaSource).mockResolvedValue({
      episode: 3,
      scenes: 4,
      source_path: SOURCE.path,
      source_revision: SOURCE.revision,
      planning_strength: 2,
      planning_cached: false,
      ai_authoring: false,
      planning_fallback: "scene 1 镜头1 镜头首行缺景别或运镜",
    });
    renderButton();
    const dialog = await openDialog();
    fireEvent.click(within(dialog).getByRole("button", { name: "开始分镜" }));

    await waitFor(() =>
      expect(screen.getByText(/AI 校验未通过，已回落机械切分：scene 1 镜头1/)).toBeInTheDocument(),
    );
    expect(screen.getByText("已生成 4 个分镜")).toBeInTheDocument();
  });
});
