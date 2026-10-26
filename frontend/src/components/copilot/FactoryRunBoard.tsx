import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, ChevronDown, ChevronUp, ListTree, Loader2, PenLine } from "lucide-react";
import { useTranslation } from "react-i18next";
import { cn } from "cn";

import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import type { ContentBlock, Turn } from "@/types";

// ---------------------------------------------------------------------------
// FactoryRunBoard — v0 只读「制作编排」面板。
//
// 数据只来自当前会话的 timeline：Claude Code 的 Task/工具调用在投影后已经是
// 「tool_use 块 + result/is_error」的形状，这里从中还原工厂链路：
//   generate_short_drama_factory（按 artifact 标签分人物圣经/大纲/台账/分集规划/正文…）
//   Bash 里的 validate_episode / validate_series
//   ingest_short_drama_artifacts
// 不做后端调度，也不改生成链路；目标就是把「现在跑到哪、每件多久」显示出来。
// ---------------------------------------------------------------------------

export type FactoryTaskKind =
  | "bible"
  | "outline"
  | "ledger"
  | "plan"
  | "text"
  | "machine"
  | "compliance"
  | "series_validate"
  | "ingest"
  | "artifact";

export type FactoryTaskStatus = "running" | "done" | "failed";

export interface FactoryTask {
  /** 稳定标识：同一工件重复生成时合并为一行，保留最早开始与最新状态。 */
  key: string;
  kind: FactoryTaskKind;
  /** 集号；系列工件与入库为 null。 */
  episode: number | null;
  status: FactoryTaskStatus;
  /** 子智能体名（Task 卡片描述），主时间线为 null。 */
  owner: string | null;
  startedAt: number | null;
  finishedAt: number | null;
  order: number;
}

const FACTORY_GEN_TOOL = "generate_short_drama_factory";
const INGEST_TOOL = "ingest_short_drama_artifacts";

const EPISODE_RE = /(?:episode|ep)[\s_-]*0*(\d+)/i;

/** 从 artifact 标签与工具名判断任务类型；标签是 Agent 自填的自由文本，只能按关键词归类。 */
export function classifyFactoryArtifact(artifact: string): { kind: FactoryTaskKind; episode: number | null } {
  const text = artifact.trim();
  const lower = text.toLowerCase();
  const episode = EPISODE_RE.exec(lower)?.[1];
  const episodeId = episode === undefined ? null : Number(episode);
  if (/character|bible|人设|人物圣经/.test(lower)) return { kind: "bible", episode: null };
  if (/outline|大纲/.test(lower)) return { kind: "outline", episode: null };
  if (/ledger|台账/.test(lower)) return { kind: "ledger", episode: null };
  if (/machine|机检/.test(lower)) return { kind: "machine", episode: episodeId };
  if (/compliance|合规/.test(lower)) return { kind: "compliance", episode: episodeId };
  if (/plan|规划/.test(lower)) return { kind: "plan", episode: episodeId };
  if (episodeId !== null) return { kind: "text", episode: episodeId };
  return { kind: "artifact", episode: episodeId };
}

function parseTime(value: string | null | undefined): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

interface RawCall {
  artifact: string;
  start: number | null;
  end: number | null;
  status: FactoryTaskStatus;
  owner: string | null;
}

function blockStatus(block: ContentBlock, active: boolean): FactoryTaskStatus {
  if (block.is_error === true) return "failed";
  if (block.result !== undefined && block.result !== null) return "done";
  return active ? "running" : "failed";
}

/**
 * 把会话时间线里的工厂调用还原成任务列表。同一工件重复出现时合并：
 * 开始时间取最早一次，结束时间与状态取最新一次（重试成功后显示完成）。
 */
export function extractFactoryTasks(turns: Turn[], draftTurn: Turn | null, active: boolean): FactoryTask[] {
  const allTurns = draftTurn ? [...turns, draftTurn] : turns;
  const calls = new Map<string, RawCall>();
  let order = 0;

  const visit = (list: Turn[], owner: string | null) => {
    for (let index = 0; index < list.length; index += 1) {
      const turn = list[index];
      if (!Array.isArray(turn.content)) continue;
      const start = parseTime(turn.timestamp);
      // 工具调用的结束时间用下一条时间戳近似：Agent 收到结果后的下一条消息即调用结束。
      const end = parseTime(list[index + 1]?.timestamp);
      for (const block of turn.content) {
        if (block.type === "tool_use" && Array.isArray(block.sub_turns) && block.sub_turns.length > 0) {
          const subOwner =
            typeof block.input?.description === "string" ? block.input.description : block.name ?? "subagent";
          visit(block.sub_turns, subOwner);
          continue;
        }
        if (block.type !== "tool_use") continue;
        if (!block.name) continue;
        let artifact: string | null = null;
        if (block.name === FACTORY_GEN_TOOL) {
          artifact = typeof block.input?.artifact === "string" ? block.input.artifact : "";
        } else if (block.name === INGEST_TOOL) {
          artifact = "ingest";
        } else if (block.name === "Bash") {
          const command = typeof block.input?.command === "string" ? block.input.command : "";
          if (/validate_episode/i.test(command)) {
            const episode = EPISODE_RE.exec(command)?.[1];
            artifact = `episode_${episode ?? ""} machine_validate`;
          } else if (/validate_series/i.test(command)) {
            artifact = "series validate";
          }
        }
        if (artifact === null) continue;
        const status = blockStatus(block, active);
        const key = artifact.trim().toLowerCase() || `${block.name}-${order}`;
        const existing = calls.get(key);
        if (existing) {
          existing.status = status;
          existing.end = end ?? existing.end;
          if (existing.start === null) existing.start = start;
        } else {
          calls.set(key, {
            artifact,
            start,
            end,
            status,
            owner,
          });
          order += 1;
        }
      }
    }
  };

  visit(allTurns, null);

  return [...calls.entries()].map(([key, call], index) => {
    const { kind, episode } =
      key === "ingest"
        ? { kind: "ingest" as const, episode: null }
        : key === "series validate"
          ? { kind: "series_validate" as const, episode: null }
          : classifyFactoryArtifact(call.artifact);
    return {
      key,
      kind,
      episode,
      status: call.status,
      owner: call.owner,
      startedAt: call.start,
      finishedAt: call.status === "running" ? null : call.end,
      order: index,
    };
  });
}

function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function StatusIcon({ status }: { status: FactoryTaskStatus }) {
  const { t } = useTranslation("dashboard");
  if (status === "running") {
    return <Loader2 aria-label={t("factory_board_running")} className="size-3.5 shrink-0 animate-spin text-primary" />;
  }
  if (status === "failed") {
    return <AlertTriangle aria-label={t("factory_board_failed")} className="size-3.5 shrink-0 text-destructive" />;
  }
  return <Check aria-label={t("factory_board_done")} className="size-3.5 shrink-0 text-muted-foreground" />;
}

interface FactoryRunBoardProps {
  turns: Turn[];
  draftTurn: Turn | null;
  /** 会话仍在跑：没有 result 的工具块才是「进行中」，否则按中断/失败显示。 */
  active: boolean;
}

export function FactoryRunBoard({ turns, draftTurn, active }: FactoryRunBoardProps) {
  const { t } = useTranslation("dashboard");
  const [open, setOpen] = useState(true);
  const tasks = useMemo(() => extractFactoryTasks(turns, draftTurn, active), [turns, draftTurn, active]);
  const hasRunning = tasks.some((task) => task.status === "running");
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!hasRunning) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [hasRunning]);

  if (tasks.length === 0) return null;

  const label = (task: FactoryTask): string => {
    const episode = task.episode === null ? null : t("factory_board_episode", { episode: task.episode });
    switch (task.kind) {
      case "bible":
        return t("factory_story_bible");
      case "outline":
        return t("factory_story_outline");
      case "ledger":
        return t("factory_story_ledger");
      case "plan":
        return [episode, t("factory_story_plans")].filter(Boolean).join(" · ");
      case "text":
        return [episode, t("factory_board_text")].filter(Boolean).join(" · ");
      case "machine":
        return [episode, t("factory_board_machine")].filter(Boolean).join(" · ");
      case "compliance":
        return [episode, t("factory_board_compliance")].filter(Boolean).join(" · ");
      case "series_validate":
        return t("factory_board_series_validate");
      case "ingest":
        return t("factory_board_ingest");
      default:
        return t("factory_board_artifact");
    }
  };

  const running = tasks.find((task) => task.status === "running") ?? null;
  const done = tasks.filter((task) => task.status === "done").length;
  const failed = tasks.filter((task) => task.status === "failed").length;

  const groups: { key: string; title: string; tasks: FactoryTask[] }[] = [];
  const push = (key: string, title: string, task: FactoryTask) => {
    const group = groups.find((entry) => entry.key === key);
    if (group) group.tasks.push(task);
    else groups.push({ key, title, tasks: [task] });
  };
  for (const task of tasks) {
    if (task.kind === "bible" || task.kind === "outline" || task.kind === "ledger" || task.kind === "series_validate") {
      push("series", t("factory_board_series_group"), task);
    } else if (task.kind === "ingest") {
      push("ingest", t("factory_board_ingest_group"), task);
    } else if (task.episode !== null) {
      push(`episode-${task.episode}`, t("factory_board_episode", { episode: task.episode }), task);
    } else {
      push("other", t("factory_board_other_group"), task);
    }
  }

  return (
    <div className="shrink-0 border-b border-border" data-testid="factory-run-board">
      <Collapsible open={open} onOpenChange={setOpen}>
        <CollapsibleTrigger render={<Button variant="ghost" size="sm" className="w-full justify-start" />}>
          <ListTree aria-hidden className="text-muted-foreground" />
          <span className="shrink-0 font-medium text-foreground">{t("factory_board_title")}</span>
          <span className="min-w-0 flex-1 truncate text-left font-normal text-muted-foreground">
            {running
              ? `${label(running)}${running.startedAt !== null ? ` · ${formatElapsed(now - running.startedAt)}` : ""}`
              : t("factory_board_summary", { done, total: tasks.length })}
          </span>
          {failed > 0 && (
            <span className="shrink-0 text-xs text-destructive">{t("factory_board_failed_count", { count: failed })}</span>
          )}
          {open ? (
            <ChevronDown aria-hidden data-icon="inline-end" className="text-muted-foreground" />
          ) : (
            <ChevronUp aria-hidden data-icon="inline-end" className="text-muted-foreground" />
          )}
        </CollapsibleTrigger>
        <CollapsibleContent>
          <div
            role="region"
            aria-label={t("factory_board_title")}
            // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- 只读滚动区需要键盘聚焦才能滚动
            tabIndex={0}
            className="relative max-h-64 overflow-y-auto border-t border-border px-2 py-1.5 outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
          >
            {groups.map((group) => (
              <section key={group.key} className="py-1">
                <h4 className="px-1 pb-0.5 text-xs font-medium text-subtle-foreground">{group.title}</h4>
                <ol className="flex flex-col">
                  {group.tasks.map((task) => {
                    const elapsed =
                      task.startedAt === null
                        ? null
                        : task.status === "running"
                          ? now - task.startedAt
                          : task.finishedAt !== null
                            ? task.finishedAt - task.startedAt
                            : null;
                    return (
                      <li
                        key={task.key}
                        className={cn(
                          "flex items-center gap-2 rounded px-1 py-0.5 text-xs leading-normal",
                          task.status === "running" && "bg-primary/5",
                        )}
                      >
                        <StatusIcon status={task.status} />
                        <span
                          className={cn(
                            "min-w-0 flex-1 truncate",
                            task.status === "done" && "text-muted-foreground",
                            task.status === "failed" && "text-destructive",
                            task.status === "running" && "text-foreground",
                          )}
                        >
                          {label(task)}
                        </span>
                        {task.owner && (
                          <span className="shrink-0 rounded bg-muted px-1 py-0.5 text-xs text-muted-foreground">
                            <PenLine aria-hidden className="mr-0.5 inline size-2.5" />
                            {task.owner}
                          </span>
                        )}
                        {elapsed !== null && elapsed > 0 && (
                          <span className="shrink-0 tabular-nums text-xs text-muted-foreground">
                            {formatElapsed(elapsed)}
                          </span>
                        )}
                      </li>
                    );
                  })}
                </ol>
              </section>
            ))}
          </div>
        </CollapsibleContent>
      </Collapsible>
    </div>
  );
}
