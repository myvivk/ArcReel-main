import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { Page } from "@playwright/test";
import { defineRegionScenarios } from "../support/scenarios.ts";
import { RECORDED_DIR, type RecordedResponse } from "../support/recorded.ts";
import { expect, type ApiOverrides } from "../support/test.ts";

// 工厂项目概览：资产完成度、费用与内容入口，以及空项目欢迎页。
const OVERVIEW_PATH = "/app/projects/demo";
const PROJECT = "GET /api/v1/projects/demo";
// 项目事件流是 SSE，没有录制；按不可重试的状态拒绝，页面停在录制的项目数据上。
const EVENT_STREAM: ApiOverrides = {
  "GET /api/v1/projects/demo/events/stream": { status: 404, body: { detail: "页面级套件不回放事件流" } },
};

interface RecordedProject {
  project: Record<string, unknown> & { episodes: Record<string, unknown>[]; status: Record<string, unknown> };
}

const recordedProject = (
  JSON.parse(readFileSync(join(RECORDED_DIR, "project-demo.json"), "utf8")) as RecordedResponse
).body as RecordedProject;
const recordedCost = (
  JSON.parse(readFileSync(join(RECORDED_DIR, "project-demo-cost-estimate.json"), "utf8")) as RecordedResponse
).body as Record<string, unknown>;
const [firstEpisode] = recordedProject.project.episodes;

function withProject(project: Record<string, unknown>): ApiOverrides {
  return { [PROJECT]: { status: 200, body: { ...recordedProject, project: { ...recordedProject.project, ...project } } } };
}

const repeat = (text: string, times: number) => Array.from({ length: times }, () => text).join("");

// 压力变体：长梗概与长世界观、很长的类型与主题、四十集、各类资产图都有过期，费用含多币种与历史支出。
const LONG_PROJECT: ApiOverrides = {
  ...EVENT_STREAM,
  ...withProject({
    title: "长夜将尽：一座边城在三十年战乱里的兴衰与守城人的家族史",
    whole_source_files: [{ source_file: "source/长夜将尽.txt" }],
    overview: {
      synopsis: repeat(
        "边城被围的第三年，守城人的长子在城墙下捡到一封没有署名的降书。他没有交给父亲，而是带着它穿过敌营去找写信的人，一路上见到的村庄、河道与驿站，都在说明这场仗早就不是为了城本身。",
        6,
      ),
      genre: "历史战争 / 家族群像 / 悬疑与权谋交织的长篇叙事",
      theme: "忠诚与背叛的代价，以及一个家族怎样在三代人的选择里慢慢失去自己守护的东西",
      world_setting: repeat(
        "架空的北方王朝末年，边城依山而建，三面环水，城内实行军屯，粮草靠一条随季节改道的运河输送。",
        8,
      ),
    },
    episodes: Array.from({ length: 40 }, (_, i) => ({
      ...firstEpisode,
      episode: i + 1,
      title: `第 ${i + 1} 集`,
      script_file: `scripts/episode_${i + 1}.json`,
      duration_seconds: 95,
    })),
    status: {
      ...recordedProject.project.status,
      assets: {
        character: { total: 128, available: 120, stale: 14 },
        scene: { total: 96, available: 61, stale: 7 },
        prop: { total: 210, available: 210, stale: 23 },
        product: { total: 0, available: 0, stale: 0 },
      },
    },
  }),
  "GET /api/v1/projects/demo/cost-estimate": {
    status: 200,
    body: {
      ...recordedCost,
      project_totals: {
        estimate: { image: { USD: 412.5, CNY: 1280 }, video: { USD: 2310.75, CNY: 8400 }, audio: { CNY: 96 } },
        actual: {
          image: { USD: 120.4 },
          video: { USD: 860.2, CNY: 3120 },
          audio: { CNY: 45.5 },
          characters: { USD: 38.6 },
          scenes: { USD: 22.1 },
          props: { USD: 51.3 },
          unassigned: { USD: 17.25, CNY: 64 },
        },
      },
    },
  },
};

// 压力变体：费用拿不到完整数字。导入的项目没有本机生成记录，六个自定义供应商模型（长名称）没有价格。
const UNPRICED_MODELS = Array.from({ length: 6 }, (_, i) => ({
  call_type: i % 2 === 0 ? "image" : "video",
  provider: `custom-${i + 1}`,
  provider_name: `海外中转网关第 ${i + 1} 号线路（按量计费、需要手动维护价格表）`,
  model: `vendor-preview-${i % 2 === 0 ? "image" : "video"}-generation-model-2026-${String(i + 1).padStart(2, "0")}`,
  count: 12 + i,
}));
const UNPRICED_PROJECT: ApiOverrides = {
  ...LONG_PROJECT,
  "GET /api/v1/projects/demo/cost-estimate": {
    status: 200,
    body: {
      ...recordedCost,
      project_totals: { estimate: { video: { USD: 120.5 } }, actual: {} },
      unpriced: { estimate: UNPRICED_MODELS, actual: [] },
      missing_local_calls: true,
    },
  },
};

const EMPTY_PROJECT: ApiOverrides = {
  ...EVENT_STREAM,
  ...withProject({ overview: null, episodes: [], whole_source_files: [] }),
};

/** 紧凑档的 Agent 面板默认展开并盖住画布右侧，先收起，内容列与画布右侧的操作才完整可见。 */
async function collapseAgentPanel(page: Page) {
  const toggle = page.getByRole("button", { name: "Agent", exact: true });
  if ((await toggle.getAttribute("aria-pressed")) === "true") await toggle.click();
}

async function overviewReady(page: Page) {
  await page.getByRole("heading", { level: 1 }).waitFor();
  // 字体与费用响应都会改变内容高度；等真实页面加载完成再测滚动可达性。
  await page.waitForLoadState("networkidle");
  await page.evaluate(() => document.fonts.ready);
}

defineRegionScenarios("项目概览", [
  {
    name: "工厂项目：页头、资产、费用与工厂入口",
    path: OVERVIEW_PATH,
    api: EVENT_STREAM,
    ready: overviewReady,
    act: async (page) => {
      await expect(page.getByRole("heading", { level: 1, name: "演示项目" })).toBeVisible();
    },
    // 内容列：页头、资产与费用两行、故事设定
    screenshot: { name: "overview-column", target: (page) => page.getByRole("main") },
  },
  {
    name: "费用明细：多币种与历史支出在弹层里分两列，底部链接到使用记录",
    path: OVERVIEW_PATH,
    api: LONG_PROJECT,
    ready: overviewReady,
    act: async (page) => {
      await collapseAgentPanel(page);
      await page.getByRole("button", { name: "明细" }).click();
      const popover = page.getByRole("dialog", { name: "费用明细" });
      await popover.waitFor();
      await expect(popover.getByRole("link", { name: "查看使用记录" })).toBeInViewport();
    },
    screenshot: { name: "overview-cost-details", target: (page) => page.getByRole("dialog", { name: "费用明细" }) },
  },
  {
    name: "费用拿不到完整数字：说明原因，列出没有价格的模型并链接到设置价格",
    path: OVERVIEW_PATH,
    api: UNPRICED_PROJECT,
    ready: overviewReady,
    act: async (page) => {
      await collapseAgentPanel(page);
      await expect(page.getByText("已花 —")).toBeVisible();
      await expect(page.getByText(/本机没有这个项目的生成记录/)).toBeVisible();
      const lastModel = page.getByRole("link", { name: new RegExp(UNPRICED_MODELS[5].model) });
      await expect(lastModel).toHaveAttribute(
        "href",
        `/app/settings?section=providers&custom=6&model=${UNPRICED_MODELS[5].model}`,
      );
      await lastModel.scrollIntoViewIfNeeded();
      await expect(lastModel).toBeInViewport();
    },
    screenshot: {
      name: "overview-cost-unpriced",
      target: (page) => page.getByText("设置价格：").locator("xpath=../../.."),
    },
  },
  {
    name: "空项目欢迎页：标题、工厂生成与工件导入入口",
    path: OVERVIEW_PATH,
    api: EMPTY_PROJECT,
    ready: async (page) => {
      await page.getByRole("heading", { name: "开始制作《演示项目》" }).waitFor();
    },
    act: collapseAgentPanel,
    screenshot: {
      name: "overview-welcome",
      target: (page) => page.getByRole("heading", { name: "开始制作《演示项目》" }).locator("xpath=../.."),
    },
  },
]);
