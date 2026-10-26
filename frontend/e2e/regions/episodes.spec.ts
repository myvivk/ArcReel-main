import type { Page } from "@playwright/test";
import { clearAgentOverlay } from "../support/region-helpers.ts";
import { recorded } from "../support/recorded.ts";
import { defineRegionScenarios } from "../support/scenarios.ts";
import { expect, type ApiOverrides } from "../support/test.ts";

// 工厂集目录：集名搜索、长目录滚动与新建、删除入口。
const PATH = "/app/projects/demo/episodes";
const project = recorded<{ project: { episodes: Record<string, unknown>[] } & Record<string, unknown> }>("project-demo.json");
const API: ApiOverrides = {
  "GET /api/v1/projects/demo/events/stream": { status: 404, body: {} },
};
const populated: ApiOverrides = {
  ...API,
  "GET /api/v1/projects/demo": {
    status: 200,
    body: {
      ...project,
      project: {
        ...project.project,
        episodes: Array.from({ length: 48 }, (_, index) => ({
          ...project.project.episodes[0], episode: index + 1,
          title: index === 47 ? "终章：雨夜里的旧城茶馆与修表匠的约定，罗盘终于指向故乡" : `第 ${index + 1} 集：雨夜`,
          script_file: `scripts/episode_${index + 1}.json`,
        })),
      },
    },
  },
};
const main = (page: Page) => page.getByRole("main");
const ready = async (page: Page) => { await main(page).getByRole("searchbox").waitFor(); };

defineRegionScenarios("工厂集目录", [
  {
    name: "目录提供工厂生成和新建集入口",
    path: PATH, api: API, ready,
    act: async (page) => {
      await clearAgentOverlay(page);
      await expect(main(page).getByRole("link", { name: /第 1 集/ })).toBeVisible();
      await expect(main(page).getByRole("button", { name: "新建一集", exact: true })).toBeVisible();
      await expect(main(page).getByRole("button", { name: /工件目录|导入工厂/ })).toHaveCount(0);
      await expect(main(page).getByRole("button", { name: /上传原文|AI 规划分集/ })).toHaveCount(0);
    },
    screenshot: { name: "episodes-factory", target: main },
  },
  {
    name: "长目录可滚动到最后一集，搜索只显示匹配集",
    path: PATH, api: populated, ready,
    act: async (page) => {
      await clearAgentOverlay(page);
      const last = main(page).getByRole("link", { name: /终章/ });
      await last.scrollIntoViewIfNeeded();
      await expect(last).toBeInViewport();
      await main(page).getByRole("searchbox").fill("终章");
      await expect(main(page).getByRole("listitem")).toHaveCount(1);
    },
  },
  {
    name: "集菜单保留在后面新建与删除",
    path: PATH, api: populated, ready,
    act: async (page) => {
      await clearAgentOverlay(page);
      await main(page).getByRole("listitem").first().getByRole("button").click();
      await expect(page.getByRole("menuitem", { name: "在后面新建一集" })).toBeVisible();
      await expect(page.getByRole("menuitem", { name: "删除这一集" })).toBeVisible();
    },
  },
  {
    name: "新建集对话框的标题与操作可达",
    path: PATH, api: populated, ready,
    act: async (page) => {
      await clearAgentOverlay(page);
      await main(page).getByRole("button", { name: "新建一集", exact: true }).click();
      await expect(page.getByRole("dialog", { name: "新建一集" }).getByLabel("标题")).toBeVisible();
    },
  },
]);
