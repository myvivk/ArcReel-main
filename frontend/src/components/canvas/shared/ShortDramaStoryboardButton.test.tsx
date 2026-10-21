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
  assets_missing_description: [] as Array<{ type: string; name: string; reason: string }>,
};

const MISSING = [
  { type: "character", name: "林辰", reason: "source_missing" },
  { type: "prop", name: "铁门", reason: "source_missing" },
];

function renderButton(storyboardCount = 0, sceneIds: string[] = []) {
  return render(
    <Router>
      <ShortDramaStoryboardButton
        projectName="proj"
        episode={3}
        storyboardCount={storyboardCount}
        sceneIds={sceneIds}
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
    planning_strength: 2,
    planning_cached: false,
    ai_authoring: true,
  });
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

  it("首次分镜直接整体提交，不携带分镜选择，也不调用资产补全", async () => {
    renderButton();
    const dialog = await openDialog();
    fireEvent.click(within(dialog).getByRole("button", { name: "开始分镜" }));

    await waitFor(() => expect(screen.getByText("已生成 5 个分镜")).toBeInTheDocument());
    expect(API.reimportShortDramaSource).toHaveBeenCalledWith(
      "proj",
      3,
      "rev-3",
      false,
      2,
      false,
      undefined,
    );
  });

  it("已有分镜时默认全选，提交走整体重新分镜", async () => {
    renderButton(4, ["E3S1", "E3S2", "E3S3", "E3S4"]);
    fireEvent.click(screen.getByRole("button", { name: "AI 重新分镜" }));
    const dialog = await screen.findByRole("dialog");

    expect(within(dialog).getByText("AI 重新分镜 · 第 3 集")).toBeInTheDocument();
    for (const id of ["S1", "S2", "S3", "S4"]) {
      expect(within(dialog).getByRole("checkbox", { name: id })).toBeChecked();
    }
    fireEvent.click(within(dialog).getByRole("button", { name: "重新分镜" }));

    await waitFor(() =>
      expect(API.reimportShortDramaSource).toHaveBeenCalledWith(
        "proj",
        3,
        "rev-3",
        true,
        2,
        false,
        undefined,
      ),
    );
  });

  it("只勾选部分分镜时按分镜重写，其余分镜保持不变", async () => {
    vi.mocked(API.reimportShortDramaSource).mockResolvedValue({
      episode: 3,
      scenes: 4,
      regenerated_scenes: 2,
      source_path: SOURCE.path,
      source_revision: SOURCE.revision,
      planning_strength: 2,
      planning_cached: false,
      ai_authoring: true,
    });
    renderButton(4, ["E3S1", "E3S2", "E3S3", "E3S4"]);
    fireEvent.click(screen.getByRole("button", { name: "AI 重新分镜" }));
    const dialog = await screen.findByRole("dialog");

    fireEvent.click(within(dialog).getByRole("checkbox", { name: "S2" }));
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "S3" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "重新分镜" }));

    await waitFor(() =>
      expect(API.reimportShortDramaSource).toHaveBeenCalledWith("proj", 3, "rev-3", true, 2, false, [
        "E3S1",
        "E3S4",
      ]),
    );
    await waitFor(() => expect(screen.getByText("已重写 2 个分镜")).toBeInTheDocument());
  });

  it("内容优化强度默认「标准」，点「增强」后按 3 提交", async () => {
    renderButton();
    const dialog = await openDialog();

    expect(within(dialog).getByRole("button", { name: "标准" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(within(dialog).getByRole("button", { name: "增强" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "开始分镜" }));

    await waitFor(() =>
      expect(API.reimportShortDramaSource).toHaveBeenCalledWith("proj", 3, "rev-3", false, 3, false, undefined),
    );
  });

  it("缺描述资产时分镜入口置灰并提示先补齐，弹窗打不开", async () => {
    vi.mocked(API.getShortDramaSource).mockResolvedValue({
      ...SOURCE,
      assets_missing_description: MISSING,
    });
    renderButton();
    await screen.findByTitle(/2 个资产缺外观描述/);

    const button = screen.getByRole("button", { name: "AI 分镜" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("回执带缺描述资产时仍给出黄条与补描述入口（不再自动补全）", async () => {
    vi.mocked(API.reimportShortDramaSource).mockResolvedValue({
      episode: 3,
      scenes: 5,
      source_path: SOURCE.path,
      source_revision: SOURCE.revision,
      planning_strength: 2,
      planning_cached: false,
      assets_without_description: MISSING,
    });
    const fillSpy = vi.spyOn(API, "fillAssetFields");
    renderButton();
    const dialog = await openDialog();
    fireEvent.click(within(dialog).getByRole("button", { name: "开始分镜" }));

    await waitFor(() =>
      expect(screen.getByText(/2 个资产缺描述：林辰（角色）、铁门（道具）/)).toBeInTheDocument(),
    );
    expect(fillSpy).not.toHaveBeenCalled();
    expect(screen.getByRole("link", { name: "去补描述" })).toHaveAttribute(
      "href",
      "/characters?focus=%E6%9E%97%E8%BE%B0",
    );
  });
});
