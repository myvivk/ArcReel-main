import type { Locator, Page } from "@playwright/test";
import { waitForEntrance } from "../support/region-helpers.ts";
import { defineRegionScenarios } from "../support/scenarios.ts";
import { expect, type ApiOverrides } from "../support/test.ts";

// 资产库：铺满档的页面外壳，类型标签带计数，网格滚动到底自动加载下一页；点卡片打开右侧详情 Sheet。

const LIBRARY_PATH = "/app/assets";
const FIRST_PAGE = "GET /api/v1/assets?limit=60&type=character";
const SECOND_PAGE = "GET /api/v1/assets?limit=60&offset=60&type=character";
const UPDATED_AT = "2025-12-28T06:30:00Z";

const LONG_NAME = "旧城茶馆二楼临窗座位上总在午后出现的说书先生";
const LONG_DESCRIPTION =
  "四十岁上下，瘦高，穿洗得发白的灰布长衫，袖口磨出了毛边；左眼角有一颗痣，说话时习惯用折扇轻敲桌面。每天午后准时出现在茶馆二楼临窗的位置，讲到紧要处会突然停下喝茶，让听众干着急。年轻时据说在码头扛过包，后来不知怎么成了说书人，没人知道他的真名。";

function asset(index: number, overrides: Record<string, unknown> = {}) {
  return {
    id: `00000000-0000-4000-9000-${String(index).padStart(12, "0")}`,
    type: "character",
    name: `${LONG_NAME} ${index}`,
    description: LONG_DESCRIPTION,
    voice_style: "低沉略带沙哑，讲到紧要处语速放慢",
    image_path: null,
    audio_path: null,
    source_project: "demo",
    updated_at: UPDATED_AT,
    derivatives: [],
    ...overrides,
  };
}

function listPage(items: unknown[], total: number) {
  return { status: 200, body: { items, total, counts: { character: total, scene: 1280, prop: 36 } } };
}

// 压力变体：75 个名字与描述都很长的角色，分两页返回。
const MANY_ASSETS: ApiOverrides = {
  [FIRST_PAGE]: listPage(
    Array.from({ length: 60 }, (_, i) => asset(i + 1)),
    75,
  ),
  [SECOND_PAGE]: listPage(
    Array.from({ length: 15 }, (_, i) => asset(i + 61)),
    75,
  ),
};

// 会撑高的详情：长描述、参考音频与 8 个衍生。
const RICH_ASSET = asset(1, {
  name: LONG_NAME,
  audio_path: "global_assets/character/voice.wav",
  derivatives: Array.from({ length: 8 }, (_, i) => ({
    name: `衍生外观 ${i + 1}`,
    description: "换上深色长袍与斗笠，腰间多一只酒葫芦，其余保持不变；夜里出场时提一盏纸灯笼。",
    image_path: null,
  })),
});
const RICH_DETAIL: ApiOverrides = { [FIRST_PAGE]: listPage([RICH_ASSET], 1) };

async function libraryReady(page: Page) {
  await page.getByRole("tab", { name: /角色/ }).waitFor();
  await page.getByRole("list").first().waitFor();
}

async function openDetail(page: Page, name: string): Promise<Locator> {
  await page.getByRole("button", { name, exact: true }).click();
  const sheet = page.getByRole("dialog", { name });
  await sheet.waitFor();
  await waitForEntrance(sheet);
  return sheet;
}

defineRegionScenarios("资产库", [
  {
    name: "类型标签显示当前搜索下各类型的数量，网格铺满内容区",
    path: LIBRARY_PATH,
    ready: libraryReady,
    act: async (page) => {
      await expect(page.getByRole("tab", { name: /场景\s*1/ })).toBeVisible();
      await expect(page.getByRole("button", { name: "林夕", exact: true })).toBeVisible();
    },
  },
  {
    name: "条目多且文字长时，滚动到底自动加载下一页",
    path: LIBRARY_PATH,
    api: MANY_ASSETS,
    ready: libraryReady,
    act: async (page) => {
      await expect(page.getByRole("tab", { name: /场景\s*1280/ })).toBeVisible();
      await page.getByRole("button", { name: `${LONG_NAME} 60`, exact: true }).scrollIntoViewIfNeeded();
      const last = page.getByRole("button", { name: `${LONG_NAME} 75`, exact: true });
      await expect(last).toBeAttached();
      await last.scrollIntoViewIfNeeded();
      await expect(last).toBeInViewport();
    },
  },
  {
    name: "详情 Sheet 内容撑高时只有正文滚动，标题与底部操作留在视口内",
    path: LIBRARY_PATH,
    api: RICH_DETAIL,
    ready: libraryReady,
    act: async (page) => {
      const sheet = await openDetail(page, LONG_NAME);
      const edit = sheet.getByRole("button", { name: "编辑" });
      const close = sheet.getByRole("button", { name: "关闭" });
      await expect(edit).toBeInViewport({ ratio: 1 });
      await expect(close).toBeInViewport({ ratio: 1 });

      const last = sheet.getByText("衍生外观 8");
      await last.scrollIntoViewIfNeeded();
      await expect(last).toBeInViewport();
      await expect(edit).toBeInViewport({ ratio: 1 });
      await expect(close).toBeInViewport({ ratio: 1 });
      await sheet.getByRole("heading", { name: LONG_NAME }).scrollIntoViewIfNeeded();
    },
  },
  {
    name: "在详情里进入编辑后，保存栏固定在 Sheet 底部",
    path: LIBRARY_PATH,
    api: RICH_DETAIL,
    ready: libraryReady,
    act: async (page) => {
      const sheet = await openDetail(page, LONG_NAME);
      await sheet.getByRole("button", { name: "编辑" }).click();
      const description = sheet.getByLabel("描述");
      await description.fill(`${LONG_DESCRIPTION}\n\n${LONG_DESCRIPTION}\n\n${LONG_DESCRIPTION}`);
      const save = sheet.getByRole("button", { name: "保存" });
      await expect(save).toBeEnabled();
      await expect(save).toBeInViewport({ ratio: 1 });
    },
  },
  {
    name: "资产库不提供新增资产与应用到项目入口",
    path: LIBRARY_PATH,
    ready: libraryReady,
    act: async (page) => {
      await expect(page.getByRole("button", { name: "新增资产" })).toHaveCount(0);
      await page.getByRole("button", { name: "「林夕」的更多操作" }).click();
      const menu = page.getByRole("menu");
      await menu.waitFor();
      await expect(menu.getByRole("menuitem", { name: "编辑" })).toBeVisible();
      await expect(menu.getByRole("menuitem", { name: "应用到项目…" })).toHaveCount(0);
      await page.keyboard.press("Escape");
      const sheet = await openDetail(page, "林夕");
      await expect(sheet.getByRole("button", { name: "应用到项目…" })).toHaveCount(0);
    },
  },
  {
    name: "删除失败原因很长：正文可聚焦并用键盘滚动，取消仍可达",
    path: LIBRARY_PATH,
    ready: libraryReady,
    api: { "DELETE /api/v1/assets/00000000-0000-4000-8000-000000000002": {
      status: 500, body: { detail: "无法删除资产，存储系统返回诊断：".repeat(120) },
    } },
    act: async (page) => {
      await page.getByRole("button", { name: "「林夕」的更多操作" }).click();
      await page.getByRole("menuitem", { name: "删除" }).click();
      const dialog = page.getByRole("alertdialog", { name: "删除角色「林夕」？" });
      await dialog.getByRole("button", { name: "删除", exact: true }).click();
      await expect(dialog.getByRole("alert")).toContainText("无法删除资产");
      const body = dialog.getByRole("region", { name: "删除角色「林夕」？" });
      await body.focus();
      await body.press("End");
      await expect.poll(() => body.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);
      await expect(dialog.getByRole("button", { name: "取消" })).toBeInViewport({ ratio: 1 });
    },
  },
]);
