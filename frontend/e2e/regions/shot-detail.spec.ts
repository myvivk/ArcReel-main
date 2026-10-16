import type { Page } from "@playwright/test";
import { box, clearAgentOverlay, waitForEntrance } from "../support/region-helpers.ts";
import { defineRegionScenarios } from "../support/scenarios.ts";
import { recorded } from "../support/recorded.ts";
import { expect, type ApiOverrides } from "../support/test.ts";

const BOARD_PATH = "/app/projects/demo/episodes/1";
const project = recorded<{
  project: Record<string, unknown>;
  scripts: Record<string, Record<string, unknown>>;
}>("project-demo.json");
const LONG_PROMPT = "林夕站在茶馆窗前，手持罗盘。窗外雨丝斜落，马灯的暖光映在石板路上。".repeat(5);
const API: ApiOverrides = {
  "GET /api/v1/projects/demo/events/stream": { status: 404, body: { detail: "页面级套件不回放事件流" } },
  "GET /api/v1/projects/demo": {
    status: 200,
    body: {
      ...project,
      project: {
        ...project.project, content_mode: "drama",
        characters: { 林夕: { description: "少女" }, 陈默: { description: "青年" } },
        scenes: { 茶馆: { description: "临街茶馆" } },
        props: { 罗盘: { description: "青铜罗盘" } },
      },
      scripts: {
        "episode_1.json": {
          ...project.scripts["episode_1.json"], content_mode: "drama", segments: undefined,
          scenes: [1, 2, 3].map((index) => ({
            scene_id: `E1S0${index}`, duration_seconds: 8, segment_break: false,
            characters_in_scene: ["林夕", "陈默"], scenes: ["茶馆"], props: ["罗盘"],
            image_prompt: LONG_PROMPT, video_prompt: "镜头缓慢推近，罗盘指针转动。",
            utterances: [{ kind: "voiceover", text: "雨下了一夜。" }],
            source_text: "不应出现在详情中的原文", note: "不应出现在详情中的备注",
          })),
        },
      },
    },
  },
};
const detail = (page: Page) => page.getByRole("tabpanel");
const materials = (page: Page) => page.getByRole("region", { name: "参考素材" });
const ready = async (page: Page) => {
  await page.getByRole("heading", { name: "引用", exact: true }).waitFor();
  await expect(materials(page).getByRole("button", { name: "3×3" })).toBeEnabled();
  await clearAgentOverlay(page);
};

defineRegionScenarios("分镜详情", [
  {
    name: "固定纵向排列引用、分镜提示词、参考素材、视频提示词",
    path: BOARD_PATH, api: API, ready,
    act: async (page) => {
      const headings = ["引用", "提示词", "分镜提示词", "参考素材", "视频提示词"];
      const positions = [];
      for (const name of headings) {
        positions.push(await box(page.getByRole("heading", { name, exact: true })));
      }
      for (let index = 1; index < positions.length; index++) {
        expect(positions[index].y).toBeGreaterThan(positions[index - 1].y);
      }
      await expect(detail(page).getByRole("button", { name: "备注", exact: true })).toBeVisible();
      await expect(detail(page).getByRole("button", { name: "上传分镜图", exact: true })).toHaveCount(0);
      await expect(detail(page).getByRole("button", { name: "上传视频", exact: true })).toBeVisible();
      await expect(detail(page).getByRole("button", { name: /^尾帧/ })).toBeVisible();
      await expect(detail(page).getByRole("button", { name: "对应原文与参考", exact: true })).toHaveCount(0);
      await expect(detail(page).getByRole("button", { name: "结构化", exact: true })).toHaveCount(0);
      await expect(detail(page).getByRole("button", { name: "文本", exact: true })).toHaveCount(0);
      await expect(materials(page).getByText(/^关键帧 K\d$/)).toHaveCount(9);
      await page.getByRole("heading", { name: "视频提示词", exact: true }).scrollIntoViewIfNeeded();
      await expect(page.getByRole("textbox", { name: "视频提示词" })).toBeInViewport();
    },
  },
  ...[["2×2", 4], ["2×3", 6], ["3×3", 9]].map(([label, count]) => ({
    name: `${label} 规格显示 ${count} 个关键帧位置`,
    path: BOARD_PATH, api: API, ready,
    act: async (page: Page) => {
      await materials(page).getByRole("button", { name: String(label) }).click();
      await expect(materials(page).getByText(/^关键帧 K\d$/)).toHaveCount(Number(count));
    },
  })),
  {
    name: "引用资产插入分镜提示词并进入未保存状态",
    path: BOARD_PATH, api: API, ready,
    act: async (page) => {
      await page.getByRole("button", { name: "引入引用资产" }).click();
      await page.getByRole("menuitem", { name: "罗盘", exact: true }).click();
      await expect(page.getByRole("textbox", { name: "分镜提示词" })).toHaveValue(`${LONG_PROMPT} 罗盘@[罗盘] `);
      await expect(page.getByRole("button", { name: "保存", exact: true })).toBeVisible();
      await expect(materials(page).getByRole("button", { name: "保存并生成" })).toBeAttached();
    },
  },
  {
    name: "未保存提示词切镜时确认，放弃后进入下一镜",
    path: BOARD_PATH, api: API, ready,
    act: async (page) => {
      const prompt = page.getByRole("textbox", { name: "分镜提示词" });
      await prompt.fill("雨停后的旧城");
      await prompt.blur();
      await page.keyboard.press("j");
      const dialog = page.getByRole("alertdialog", { name: "「S01」有未保存的修改" });
      await expect(dialog.getByRole("button", { name: "保存并切换" })).toBeVisible();
      await waitForEntrance(dialog);
    },
  },
  {
    name: "引用编辑仍可用",
    path: BOARD_PATH, api: API, ready,
    act: async (page) => {
      await page.getByRole("button", { name: "编辑引用" }).click();
      const dialog = page.getByRole("dialog", { name: /引用/ });
      await dialog.getByRole("button", { name: /陈默/ }).click();
      await expect(dialog.getByText("引用有改动，确定后生效")).toBeVisible();
    },
  },
]);
