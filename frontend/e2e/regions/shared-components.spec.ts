import type { Page } from "@playwright/test";
import { clearAgentOverlay, waitForEntrance } from "../support/region-helpers.ts";
import { recorded } from "../support/recorded.ts";
import { defineRegionScenarios } from "../support/scenarios.ts";
import { expect, type ApiOverrides } from "../support/test.ts";

// 集页共用的提示词对话框与 Agent 附加指令输入。
const EPISODE_PATH = "/app/projects/demo/episodes/1";
const PROJECT = "GET /api/v1/projects/demo";

interface RecordedProject {
  project: Record<string, unknown> & { episodes: Record<string, unknown>[] };
  scripts: Record<string, Record<string, unknown>>;
}

const recordedProject = recorded<RecordedProject>("project-demo.json");
const recordedScript = recordedProject.scripts["episode_1.json"];

// 项目事件流是 SSE，没有录制；按不可重试的状态拒绝，页面停在替换后的数据上。
const BASE: ApiOverrides = {
  "GET /api/v1/projects/demo/events/stream": { status: 404, body: { detail: "页面级套件不回放事件流" } },
};

const shotId = (n: number) => `E1S${String(n).padStart(2, "0")}`;
// 分镜图用内联 SVG：getFileUrl 原样使用 data: 地址，不发请求，截图稳定。
function portraitSvg(hue: number) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="360" height="640" viewBox="0 0 90 160"><rect width="90" height="160" fill="hsl(${hue} 30% 22%)"/><circle cx="45" cy="62" r="20" fill="hsl(${hue} 35% 55%)"/><rect x="18" y="96" width="54" height="48" rx="6" fill="hsl(${hue} 30% 40%)"/></svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

// 四十条分镜的正式脚本，第一条已有分镜图：「编写提示词」的自选列表与查看大图都用它。
const SHOT_COUNT = 40;
const scenes = Array.from({ length: SHOT_COUNT }, (_, i) => ({
  scene_id: shotId(i + 1),
  episode: 1,
  duration_seconds: 4,
  segment_break: i % 8 === 0,
  utterances: [],
  characters_in_scene: [],
  image_prompt: i % 3 === 0 ? "" : `${shotId(i + 1)} 的分镜图提示词`,
  video_prompt: i % 3 === 0 ? "" : `${shotId(i + 1)} 的视频提示词`,
  ...(i === 0
    ? {
        generated_assets: {
          storyboard_image: portraitSvg(210),
          storyboard_last_image: null,
          grid_id: null,
          grid_cell_index: null,
          video_clip: null,
          video_thumbnail: null,
          video_uri: null,
          status: "storyboard_ready",
        },
      }
    : {}),
}));

const SCRIPTED: ApiOverrides = {
  ...BASE,
  [PROJECT]: {
    status: 200,
    body: {
      ...recordedProject,
      scripts: { "episode_1.json": { ...recordedScript, scenes } },
    },
  },
};

const authoringDialog = (page: Page) => page.getByRole("dialog", { name: "编写提示词" });
defineRegionScenarios("共享组件：集页对话框与提示", [
  {
    name: "「编写提示词」：自选多条列出四十条分镜，只在列表内滚动",
    path: EPISODE_PATH,
    api: SCRIPTED,
    ready: async (page) => {
      await page.getByRole("button", { name: "编写提示词" }).first().waitFor();
    },
    act: async (page) => {
      await clearAgentOverlay(page);
      await page.getByRole("button", { name: "编写提示词" }).first().click();
      const dialog = authoringDialog(page);
      await waitForEntrance(dialog);
      await dialog.getByRole("radio", { name: "自选多条" }).click();
      const list = dialog.getByRole("list", { name: "自选多条" });
      await expect(list.getByRole("checkbox")).toHaveCount(SHOT_COUNT);
      await expect(dialog.getByRole("button", { name: "交给 Agent" })).toBeInViewport();
    },
    screenshot: { name: "shared-prompt-authoring", target: authoringDialog },
  },
  {
    name: "交给 Agent 预填多行附加指令后输入框自动撑高",
    path: EPISODE_PATH,
    api: SCRIPTED,
    ready: async (page) => {
      await page.getByRole("button", { name: "编写提示词" }).first().waitFor();
    },
    act: async (page) => {
      const composer = page.getByRole("combobox", { name: "Agent 输入" });
      const emptyHeight = await composer.evaluate((el) => el.clientHeight);
      await clearAgentOverlay(page);
      await page.getByRole("button", { name: "编写提示词" }).first().click();
      const dialog = authoringDialog(page);
      const instruction = "每集保留一个悬念。\n优先使用已经登场的角色。\n不要重复上一集内容。";
      await dialog.getByRole("textbox").fill(instruction);
      await dialog.getByRole("button", { name: "交给 Agent" }).click();
      await expect(composer).toHaveValue(new RegExp(instruction));
      await expect.poll(() => composer.evaluate((el) => el.clientHeight)).toBeGreaterThan(emptyHeight);
      const sizing = await composer.evaluate((el) => ({
        content: el.scrollHeight, visible: el.clientHeight, overflow: getComputedStyle(el).overflowY,
      }));
      if (sizing.content > sizing.visible + 1) {
        expect(["auto", "scroll"]).toContain(sizing.overflow);
      }
    },
  },
]);
