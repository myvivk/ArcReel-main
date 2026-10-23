import type { Page } from "@playwright/test";
import { box, clearAgentOverlay, waitForEntrance } from "../support/region-helpers.ts";
import { defineRegionScenarios } from "../support/scenarios.ts";
import { recorded } from "../support/recorded.ts";
import { expect, type ApiOverrides } from "../support/test.ts";
import type { ShotGridPlan } from "../../src/types/shot-grid.ts";

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
          ...project.scripts["episode_1.json"], content_mode: "drama",
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
const draftGrid: ShotGridPlan = {
  id: "shot_grid_000000000001", episode: 1, script_file: "episode_1.json", shot_id: "E1S01",
  rows: 3, cols: 3, prompt: "", cell_paths: [],
  cells: Array.from({ length: 9 }, (_, index) => ({
    index, prompt: "", time_ratio: index / 8, continuity_notes: "", confirmed: false, image_path: null,
  })),
  composite_path: null, status: "draft", task_id: null,
  created_at: "", updated_at: "", error_message: null,
};
const NO_GRID: ApiOverrides = Object.fromEntries(
  ["E1S01", "E1S02", "E1S03", "E1S05"].map((id) => [
    `GET /api/v1/projects/demo/shot-grids?script_file=episode_1.json&shot_id=${id}`,
    { status: 200, body: null },
  ]),
);
const ready = async (page: Page) => {
  await page.getByRole("heading", { name: "引用", exact: true }).waitFor();
  await expect(materials(page).getByRole("button", { name: "配置参考素材" })).toBeEnabled();
  await clearAgentOverlay(page);
};

defineRegionScenarios("分镜详情", [
  {
    name: "固定纵向排列引用、分镜提示词、参考素材、视频提示词",
    path: BOARD_PATH, api: { ...API, ...NO_GRID }, ready,
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
      await expect(materials(page).getByRole("button", { name: "配置参考素材" })).toBeVisible();
      await expect(materials(page).getByText("根据分镜提示词生成连续关键帧，按从左到右、从上到下的顺序排列。")).toBeVisible();
      await page.getByRole("heading", { name: "视频提示词", exact: true }).scrollIntoViewIfNeeded();
      await expect(page.getByRole("textbox", { name: "视频提示词" })).toBeInViewport();
    },
  },
  {
    name: "参考素材先配置再入队：默认 2×2，确认时保存新规格",
    path: BOARD_PATH,
    api: {
      ...API, ...NO_GRID,
      "PUT /api/v1/projects/demo/shot-grids": { status: 200, body: draftGrid },
      "POST /api/v1/projects/demo/shot-grids/generate?script_file=episode_1.json&shot_id=E1S01": {
        status: 200, body: { task_id: "task_e2e", status: "queued" },
      },
    },
    ready,
    act: async (page) => {
      await materials(page).getByRole("button", { name: "配置参考素材" }).click();
      const dialog = page.getByRole("dialog", { name: "配置参考素材" });
      await expect(dialog.getByRole("button", { name: "2×2" })).toHaveAttribute("aria-pressed", "true");
      await dialog.getByRole("button", { name: "3×3" }).click();
      const saved = page.waitForRequest((request) => request.method() === "PUT" && request.url().endsWith("/shot-grids"));
      const queued = page.waitForRequest((request) => request.method() === "POST" && request.url().includes("/shot-grids/generate"));
      await dialog.getByRole("button", { name: "确定并生成" }).click();
      expect((await saved).postDataJSON()).toMatchObject({
        script_file: "episode_1.json", episode: 1, shot_id: "E1S01", rows: 3, cols: 3,
      });
      expect((await queued).url()).toContain("shot_id=E1S01");
      await expect(page.getByRole("dialog")).toHaveCount(0);
      await expect(materials(page).getByRole("textbox")).toHaveCount(0);
    },
  },
  {
    name: "取消参考素材配置不保存，重新打开仍使用原规格和空文本",
    path: BOARD_PATH, api: { ...API, ...NO_GRID }, ready,
    act: async (page) => {
      const writes: string[] = [];
      page.on("request", (request) => {
        if (request.url().includes("/shot-grids") && ["PUT", "POST"].includes(request.method())) {
          writes.push(request.url());
        }
      });
      await materials(page).getByRole("button", { name: "配置参考素材" }).click();
      const dialog = page.getByRole("dialog", { name: "配置参考素材" });
      await dialog.getByRole("button", { name: "3×3" }).click();
      await dialog.getByRole("textbox", { name: "逐格镜头提示词（9 格）" }).fill("第1格（远景）：茶馆雨夜");
      await dialog.getByRole("button", { name: "取消", exact: true }).click();
      await expect(dialog).toHaveCount(0);
      await materials(page).getByRole("button", { name: "配置参考素材" }).click();
      await expect(dialog.getByRole("button", { name: "2×2" })).toHaveAttribute("aria-pressed", "true");
      await expect(dialog.getByRole("textbox", { name: "逐格镜头提示词（4 格）" })).toHaveValue("");
      expect(writes).toEqual([]);
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
    },
  },
  {
    name: "已确认的参考素材锁死规格",
    path: BOARD_PATH,
    api: {
      ...API, ...NO_GRID,
      "GET /api/v1/projects/demo/shot-grids?script_file=episode_1.json&shot_id=E1S01": {
        status: 200,
        body: { ...draftGrid, status: "confirmed", cells: draftGrid.cells.map((cell) => ({ ...cell, confirmed: true })) },
      },
    },
    ready,
    act: async (page) => {
      await expect(materials(page).getByText("参考素材已确认，可引入视频提示词")).toBeVisible();
      await materials(page).getByRole("button", { name: "配置参考素材" }).click();
      const dialog = page.getByRole("dialog", { name: "配置参考素材" });
      await expect(dialog.getByRole("button", { name: "3×3" })).toHaveAttribute("aria-pressed", "true");
      await expect(dialog.getByRole("button", { name: "2×2" })).toBeDisabled();
      await expect(dialog.getByText("已确认的参考素材锁定规格；要换规格请先取消确认。")).toBeVisible();
      await dialog.getByRole("button", { name: "取消", exact: true }).click();
    },
  },
  {
    name: "引用资产插入分镜提示词并进入未保存状态",
    path: BOARD_PATH, api: { ...API, ...NO_GRID }, ready,
    act: async (page) => {
      await page.getByRole("button", { name: "引入引用资产" }).click();
      await page.getByRole("dialog").getByRole("button", { name: "罗盘", exact: true }).click();
      await expect(page.getByRole("textbox", { name: "分镜提示词" })).toHaveValue(`${LONG_PROMPT} 罗盘@[罗盘] `);
      await expect(page.getByRole("button", { name: "保存", exact: true })).toBeVisible();
      await expect(detail(page).getByRole("button", { name: "保存并生成" }).first()).toBeAttached();
    },
  },
  {
    name: "未保存提示词切镜时显示离开确认",
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
