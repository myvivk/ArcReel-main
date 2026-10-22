import { readFileSync } from "node:fs";
import { join } from "node:path";
import { defineRegionScenarios } from "../support/scenarios.ts";
import { RECORDED_DIR, type RecordedResponse } from "../support/recorded.ts";

const recorded = (JSON.parse(readFileSync(join(RECORDED_DIR, "project-demo.json"), "utf8")) as RecordedResponse).body as {
  project: Record<string, unknown>;
};

defineRegionScenarios("故事规划", [
  {
    name: "工厂原文四项阅读器",
    path: "/app/projects/demo/story",
    api: {
      "GET /api/v1/projects/demo": { status: 200, body: { ...recorded, project: { ...recorded.project, content_mode: "drama" } } },
      "GET /api/v1/projects/demo/events/stream": { status: 404, body: {} },
    },
    ready: async (page) => { await page.getByRole("tab", { name: "人物圣经" }).waitFor(); },
    act: async (page) => {
      await page.getByRole("tab", { name: "全剧大纲" }).click();
      await page.getByRole("tab", { name: "连续性台账" }).click();
      await page.getByRole("tab", { name: "分集规划" }).click();
    },
  },
]);
