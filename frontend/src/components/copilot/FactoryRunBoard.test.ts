import { describe, expect, it } from "vitest";

import type { ContentBlock, Turn } from "@/types";

import { classifyFactoryArtifact, extractFactoryTasks } from "./FactoryRunBoard";

function gen(artifact: string, extra: Partial<ContentBlock> = {}): ContentBlock {
  return {
    type: "tool_use",
    id: `gen-${artifact}`,
    name: "generate_short_drama_factory",
    input: { artifact },
    ...extra,
  };
}

function turn(content: ContentBlock[], timestamp: string): Turn {
  return { type: "assistant", content, timestamp };
}

describe("classifyFactoryArtifact", () => {
  it("按标签归类工件并解析集号", () => {
    expect(classifyFactoryArtifact("character-bible")).toEqual({ kind: "bible", episode: null });
    expect(classifyFactoryArtifact("series-outline")).toEqual({ kind: "outline", episode: null });
    expect(classifyFactoryArtifact("ledger")).toEqual({ kind: "ledger", episode: null });
    expect(classifyFactoryArtifact("episode_02.plan")).toEqual({ kind: "plan", episode: 2 });
    expect(classifyFactoryArtifact("episode_02")).toEqual({ kind: "text", episode: 2 });
    expect(classifyFactoryArtifact("episode_02.machine")).toEqual({ kind: "machine", episode: 2 });
    expect(classifyFactoryArtifact("episode_02.compliance")).toEqual({ kind: "compliance", episode: 2 });
    expect(classifyFactoryArtifact("spinoff")).toEqual({ kind: "artifact", episode: null });
  });
});

describe("extractFactoryTasks", () => {
  it("还原系列工件、机检、分集规划与入库", () => {
    const turns = [
      turn([gen("character-bible", { result: "ok" })], "2026-10-25T05:00:00Z"),
      turn(
        [gen("series-outline", { result: "ok" }), { type: "tool_use", id: "b1", name: "Bash", input: { command: "python validate_episode.py episodes/episode_01.md" }, result: "PASSED" }],
        "2026-10-25T05:04:00Z",
      ),
      turn([gen("episode_01.plan", { result: "ok" }), { type: "tool_use", id: "i1", name: "ingest_short_drama_artifacts", input: { artifacts: [] }, result: "ok" }], "2026-10-25T05:06:00Z"),
    ];

    const tasks = extractFactoryTasks(turns, null, false);
    expect(tasks.map((task) => task.kind)).toEqual(["bible", "outline", "machine", "plan", "ingest"]);
    expect(tasks.every((task) => task.status === "done")).toBe(true);
    expect(tasks[0].startedAt).toBe(Date.parse("2026-10-25T05:00:00Z"));
    expect(tasks[0].finishedAt).toBe(Date.parse("2026-10-25T05:04:00Z"));
  });

  it("运行中没有 result 的块是进行中，会话结束后按失败显示", () => {
    const turns = [turn([gen("character-bible")], "2026-10-25T05:00:00Z")];
    expect(extractFactoryTasks(turns, null, true)[0].status).toBe("running");
    expect(extractFactoryTasks(turns, null, false)[0].status).toBe("failed");
  });

  it("同一工件重试合并为一行，状态取最新一次", () => {
    const turns = [
      turn([gen("episode_01", { is_error: true })], "2026-10-25T05:00:00Z"),
      turn([gen("episode_01", { result: "ok" })], "2026-10-25T05:05:00Z"),
    ];
    const tasks = extractFactoryTasks(turns, null, false);
    expect(tasks).toHaveLength(1);
    expect(tasks[0]).toMatchObject({ kind: "text", episode: 1, status: "done" });
    expect(tasks[0].startedAt).toBe(Date.parse("2026-10-25T05:00:00Z"));
  });

  it("子智能体里的工厂调用带上负责人", () => {
    const turns = [
      turn(
        [
          {
            type: "tool_use",
            id: "agent-1",
            name: "Agent",
            input: { description: "第 2 集正文" },
            sub_turns: [turn([gen("episode_02", { result: "ok" })], "2026-10-25T05:00:00Z")],
          },
        ],
        "2026-10-25T04:59:00Z",
      ),
    ];
    const tasks = extractFactoryTasks(turns, null, false);
    expect(tasks[0]).toMatchObject({ kind: "text", episode: 2, owner: "第 2 集正文" });
  });
});
