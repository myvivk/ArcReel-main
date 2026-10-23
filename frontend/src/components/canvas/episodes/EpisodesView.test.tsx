import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { API } from "@/api";
import { useProjectsStore } from "@/stores/projects-store";
import type { ProjectData } from "@/types";
import { EpisodesView } from "./EpisodesView";

const PROJECT: ProjectData = {
  title: "短剧", content_mode: "drama", style: "", characters: {},
  episodes: [
    { episode: 7, title: "开端", script_file: "scripts/episode_7.json", source_origin: "own" },
    { episode: 2, title: "转折", script_file: "scripts/episode_2.json", source_origin: "own" },
  ],
};
function renderView(path = "/episodes") {
  const location = memoryLocation({ path, record: true });
  render(<Router hook={location.hook} searchHook={location.searchHook}><EpisodesView projectName="demo" /></Router>);
  return location;
}
beforeEach(() => {
  vi.restoreAllMocks();
  useProjectsStore.setState(useProjectsStore.getInitialState(), true);
  useProjectsStore.setState({ currentProjectName: "demo", currentProjectData: PROJECT });
});
describe("工厂集目录", () => {
  it("按账本顺序展示并按集标题搜索，链接指向稳定集号", async () => {
    const location = renderView();
    const links = screen.getAllByRole("link");
    expect(links.map(link => link.textContent)).toEqual(["开端", "转折"]);
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "转折" } });
    expect(screen.queryByRole("link", { name: "开端" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "转折" }));
    expect(location.history?.at(-1)).toBe("/episodes/2");
  });
  it("空目录提供工厂生成、工件导入和集账本创建", () => {
    useProjectsStore.setState({ currentProjectData: { ...PROJECT, episodes: [] } });
    renderView();
    expect(screen.getAllByRole("button", { name: "AI 原创剧本" }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("button", { name: "工件目录" }).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "新建一集" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /上传原文|AI 规划分集/ })).not.toBeInTheDocument();
  });
  it("新建查询参数打开账本弹窗并清理地址", async () => {
    const location = renderView("/episodes?create=1");
    expect(await screen.findByRole("dialog", { name: "新建一集" })).toBeInTheDocument();
    expect(location.history?.at(-1)).toBe("/episodes");
    expect(screen.queryByRole("textbox", { name: "原文" })).not.toBeInTheDocument();
  });
  it("历史集定位参数进入对应工厂集页", async () => {
    const location = renderView("/episodes?episode=7");
    await waitFor(() => expect(location.history?.at(-1)).toBe("/episodes/7"));
  });
  it("删除先读取丢失清单，确认后才提交指定 revision", async () => {
    const remove = vi.spyOn(API, "deleteEpisode").mockImplementation(async (_project, episode, revision) => revision
      ? { status: "deleted", impact: { episode, recoverable: false, revision } }
      : { status: "confirmation_required", impact: { episode, recoverable: false, revision: "r1", text: "工厂工件将一并删除" } });
    vi.spyOn(useProjectsStore.getState(), "refreshProject").mockResolvedValue("success");
    renderView();
    await userEvent.click(screen.getByRole("button", { name: "「开端」的操作" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "删除这一集" }));
    const confirmation = await screen.findByRole("alertdialog");
    expect(confirmation).toHaveTextContent("工厂工件将一并删除");
    expect(remove).toHaveBeenCalledWith("demo", 7);
    expect(remove).toHaveBeenCalledTimes(1);
    await userEvent.click(within(confirmation).getByRole("button", { name: "删除" }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith("demo", 7, "r1"));
  });
});
