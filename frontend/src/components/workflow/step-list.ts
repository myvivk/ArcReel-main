import type { TFunction } from "i18next";
import type { EpisodeEditOverview } from "@/types/edit-timeline";
import type { StoryboardBatchKind } from "@/types/storyboard-batch";
import type { WorkflowArtifactCollection, WorkflowContent, WorkflowNextAction, WorkflowOperation, WorkflowPlan, WorkflowPlanStep } from "@/types/workflow";
import { ROUTE_APP_SETTINGS, WORKSPACE_ROUTE_EPISODES, EPISODE_VIEW_PARAM, episodeAssetsViewPath, episodeEditViewPath } from "@/app-routes";
import { formatNameList } from "@/utils/list-format";

export type StepRowKey = "source" | "script" | "prompts" | "assets" | "materials" | "video_prompt" | "voice" | "videos" | "edit";
export type StepRowTone = "done" | "todo" | "partial" | "running" | "warn" | "danger";
export type StepIntent =
  | { type: "agent"; text: string }
  | { type: "author_prompts" }
  | { type: "author_prompts_to_agent" }
  | { type: "open_author_prompts" }
  | { type: "open_video_prompts"; entryIds: string[] }
  | { type: "asset_batch"; episodeId: number }
  | { type: "storyboard_batch"; episodeId: number; kind: StoryboardBatchKind }
  | { type: "create_edit_timeline" }
  | { type: "view_unit"; unitId: string }
  | { type: "route"; path: string };
export interface StepAct {
  key: string; label: string; kind: "agent" | "ai" | "nav" | "danger";
  intent: StepIntent; disabledReason?: string | null;
}
export interface StepNote { key: string; text: string; tone: "warn" | "danger" | "info"; act?: StepAct; }
export interface StepRowView {
  key: StepRowKey; title: string; tone: StepRowTone; status: string;
  notes: StepNote[]; acts: StepAct[]; steps: WorkflowPlanStep[];
}
export interface StepInstruction { initial: string; persist: "prompt_authoring" | null; }
export interface NextStepView {
  rowKey: StepRowKey; actionType: WorkflowNextAction["type"]; title: string; detail: string | null;
  instruction: StepInstruction | null; primary: StepAct[]; alternatives: StepAct[]; hint: StepNote | null;
}
export interface StepListView { rows: StepRowView[]; next: NextStepView | null; summary: string; }
export interface StepListContext {
  t: TFunction; lang: string; episodeId: number; episodeRef: string; episodeDurationSeconds: number | null;
  assetRoute: (name: string) => string | null; savedPromptInstructions: string;
  canAuthorPrompts: boolean; canViewUnit: boolean; editOverview: EpisodeEditOverview | null;
}
interface Facts { plan: WorkflowPlan; content: WorkflowContent; steps: Map<string, WorkflowPlanStep>; }

function ids(collection: WorkflowArtifactCollection | undefined, key: "current_ids" | "stale_ids" | "missing_ids") {
  const value = collection?.[key];
  return Array.isArray(value) ? value : [];
}


export function refusalReason(t: TFunction, operation: WorkflowOperation | undefined): string | null {
  if (!operation || operation.state === "admitted") return null;
  const reason = operation.reason ?? "unknown";
  return t(`workflow:refusal_${reason}`, { defaultValue: t("workflow:refusal_unknown") });
}


function mediaStatus(t: TFunction, collection: WorkflowArtifactCollection | undefined): { status: string; tone: StepRowTone } {
  if (collection?.state === "blocked") return { status: t("workflow:status_unreadable"), tone: "danger" };
  const available = ids(collection, "current_ids").length + ids(collection, "stale_ids").length;
  const total = available + ids(collection, "missing_ids").length;
  if (total === 0) return { status: t("workflow:status_none"), tone: "todo" };
  if (available === total) return { status: t("workflow:status_media_all", { count: total }), tone: "done" };
  if (available === 0) return { status: t("workflow:status_media_none_of", { count: total }), tone: "todo" };
  return { status: t("workflow:status_media_some", { count: total, available }), tone: "partial" };
}


function stepsFor(facts: Facts, stepIds: string[]): WorkflowPlanStep[] {
  return stepIds.flatMap((id) => {
    const step = facts.steps.get(id);
    return step && step.state !== "skipped" ? [step] : [];
  });
}


function withRunning(row: StepRowView): StepRowView {
  return row.steps.some((step) => step.state === "active") ? { ...row, tone: "running" } : row;
}


function editActs(facts: Facts, ctx: StepListContext): StepAct[] {
  const { t } = ctx;
  const refusal = refusalReason(t, facts.plan.status.operations.create_edit_timeline);
  return [
    agentAct(t, t("workflow:agent_prefill_create_edit_timeline", { episodeRef: ctx.episodeRef }), t("workflow:act_agent_edit"), refusal),
    {
      key: "create-edit-timeline",
      label: t("workflow:act_create_edit_timeline"),
      kind: "ai",
      intent: { type: "create_edit_timeline" },
      disabledReason: refusal,
    },
  ];
}


function editTimelineCount(facts: Facts, ctx: StepListContext): number {
  const ids = facts.plan.status.artifacts.edit_timelines?.timeline_ids;
  return ctx.editOverview?.timeline_count ?? (Array.isArray(ids) ? ids.length : 0);
}


function openEditViewAct(ctx: StepListContext, key: string, label: string, timelineId?: string): StepAct {
  return { key, label, kind: "nav", intent: { type: "route", path: episodeEditViewPath(ctx.episodeId, timelineId) } };
}


function editRow(facts: Facts, ctx: StepListContext): StepRowView {
  const { t } = ctx;
  const { plan, content } = facts;
  const overview = ctx.editOverview;
  const count = editTimelineCount(facts, ctx);
  const issues = overview?.latest?.issue_count ?? 0;
  let status: string;
  if (count === 0) status = t("workflow:status_edit_none");
  else if (issues > 0) status = t("workflow:status_edit_timelines_with_issues", { count, issues });
  else status = t("workflow:status_edit_timelines", { count });
  const notes: StepNote[] = [];
  const stale = overview?.stale_final_cuts ?? [];
  if (stale.length > 0) {
    notes.push({
      key: "final-cut-stale",
      tone: "warn",
      text: t("workflow:note_final_cut_stale", {
        count: stale.length,
        names: formatNameList(stale.map((timeline) => timeline.name), ctx.lang),
      }),
      act: openEditViewAct(ctx, "final-cut-stale-render", t("workflow:act_go_render"), stale[0].id),
    });
  }
  // 剪辑是下一步时入口就地展开在下一步里；其余时候有正式脚本条目就常驻在本行，准入不满足时置灰。
  const hasItems = content.formal_script === "present" && (content.script_item_count ?? 0) > 0;
  const acts = hasItems && plan.next_action.type !== "create_edit_timeline" ? editActs(facts, ctx) : [];
  if (count > 0) acts.push(openEditViewAct(ctx, "open-edit-view", t("workflow:act_open_edit_view")));
  return {
    key: "edit",
    title: t("workflow:row_edit"),
    tone: count > 0 ? "done" : "todo",
    status,
    notes,
    acts,
    steps: stepsFor(facts, ["edit"]),
  };
}


function assetsRow(facts: Facts, ctx: StepListContext): StepRowView {
  const { t } = ctx;
  const { content } = facts;
  const formal = content.script_from_short_drama
    || (content.formal_script === "present" && (content.script_item_count ?? 0) > 0);
  const withoutDescription = new Set(content.referenced_assets_without_description);
  const toGenerate = content.referenced_assets_without_sheet.filter((name) => !withoutDescription.has(name));
  const stale = content.referenced_asset_sheets_stale;
  const notes: StepNote[] = [];
  const galleryAct = (key: string, label: string, name: string | undefined): StepAct | undefined => {
    const path = name ? ctx.assetRoute(name) : null;
    return path ? { key, label, kind: "nav", intent: { type: "route", path } } : undefined;
  };
  if (stale.length > 0) {
    notes.push({
      key: "stale",
      tone: "warn",
      text: t("workflow:note_assets_stale", { count: stale.length, names: formatNameList(stale, ctx.lang) }),
      act: galleryAct("stale-gallery", t("workflow:act_view_gallery"), stale[0]),
    });
  }
  if (withoutDescription.size > 0) {
    const names = content.referenced_assets_without_description;
    notes.push({
      key: "no-description",
      tone: "info",
      text: t("workflow:note_assets_without_description", { count: names.length, names: formatNameList(names, ctx.lang) }),
      act: galleryAct("describe", t("workflow:act_fill_description"), names[0]),
    });
  }
  if (content.unregistered_references.length > 0) {
    const names = content.unregistered_references;
    notes.push({
      key: "unregistered",
      tone: "warn",
      text: t("workflow:note_unregistered_references", { count: names.length, names: formatNameList(names, ctx.lang) }),
    });
  }
  let status: string;
  let tone: StepRowTone;
  if (!formal) {
    status = t("workflow:status_none");
    tone = "todo";
  } else if (toGenerate.length > 0) {
    status = t("workflow:status_assets_missing", { count: toGenerate.length });
    tone = "partial";
  } else if (content.referenced_assets_without_sheet.length > 0) {
    // 余下没有资产图的是现在生成不了的（缺描述，或衍生的本体缺图），原因见本行的提醒。
    status = t("workflow:status_assets_unsheeted", { count: content.referenced_assets_without_sheet.length });
    tone = "warn";
  } else {
    status = t("workflow:status_assets_done");
    tone = stale.length > 0 ? "warn" : "done";
  }
  // 有正式脚本时给一个进资产步骤的入口：进度提醒与资产步骤就此打通（D-32）。
  const acts: StepAct[] = formal
    ? [
        {
          key: "open-assets-view",
          label: t("workflow:act_open_assets_view"),
          kind: "nav",
          intent: { type: "route", path: episodeAssetsViewPath(ctx.episodeId) },
        },
      ]
    : [];
  return {
    key: "assets",
    title: t("workflow:row_assets"),
    tone,
    status,
    notes,
    acts,
    steps: stepsFor(facts, ["asset_sheets"]),
  };
}


function agentAct(t: TFunction, text: string, label?: string, disabledReason?: string | null): StepAct {
  return {
    key: "agent",
    label: label ?? t("workflow:act_agent"),
    kind: "agent",
    intent: { type: "agent", text },
    disabledReason,
  };
}


/** 工厂内容及视觉派生物按制作顺序呈现。 */
function buildRows(facts: Facts, ctx: StepListContext): StepRowView[] {
  const { t } = ctx;
  const { content, plan } = facts;
  const status = plan.status;
  const source = content.script_from_short_drama === true;
  const formal = content.formal_script === "present";
  const row = (key: StepRowKey, title: string, collection: WorkflowArtifactCollection | undefined, stepIds: string[]): StepRowView => {
    const media = mediaStatus(t, collection);
    return { key, title, tone: formal ? media.tone : "todo", status: formal ? media.status : t("workflow:status_none"), notes: [], acts: [], steps: stepsFor(facts, stepIds) };
  };
  const rows: StepRowView[] = [{
    key: "source", title: t("dashboard:episode_view_source"), tone: source ? "done" : "todo",
    status: source ? t("workflow:status_source_present") : t("workflow:status_source_absent"), notes: [],
    acts: [{ key: "factory-source", label: t("dashboard:episode_view_source"), kind: "nav", intent: { type: "route", path: "/" + WORKSPACE_ROUTE_EPISODES + "/" + ctx.episodeId + "?" + EPISODE_VIEW_PARAM + "=source" } }],
    steps: stepsFor(facts, ["short_drama_source"]),
  }, assetsRow(facts, ctx), {
    key: "script", title: t("dashboard:short_drama_ai_storyboard"),
    tone: content.formal_script === "invalid" ? "danger" : formal && (content.script_item_count ?? 0) > 0 ? "done" : "todo",
    status: formal ? t("workflow:status_items_storyboard", { count: content.script_item_count ?? 0 }) : t("workflow:status_none"),
    notes: [], acts: [], steps: stepsFor(facts, ["script_structure"]),
  }];
  const pending = content.pending_authoring_ids.length;
  rows.push({
    key: "prompts", title: t("workflow:row_prompts"), tone: !formal ? "todo" : pending ? "partial" : "done",
    status: !formal ? t("workflow:status_none") : pending ? t("workflow:status_prompts_pending", { count: pending, total: content.script_item_count ?? 0 }) : t("workflow:status_prompts_done"),
    notes: [], acts: pending && ctx.canAuthorPrompts && plan.next_action.type !== "author_prompts" ? [{ key: "author-prompts", label: t("dashboard:prompt_authoring_open"), kind: "ai", intent: { type: "open_author_prompts" }, disabledReason: refusalReason(t, status.operations.author_prompts) }] : [],
    steps: stepsFor(facts, ["final_script"]),
  });
  rows.push(row("materials", t("workflow:row_materials"), status.artifacts.shot_grids, ["shot_grid"]));
  const stale = ids(status.artifacts.video_prompts, "stale_ids");
  const videoPrompts = row("video_prompt", t("dashboard:detail_video_prompt_title"), {
    ...status.artifacts.video_prompts, missing_ids: [...ids(status.artifacts.video_prompts, "missing_ids"), ...stale], stale_ids: [],
  }, ["video_prompt"]);
  if (stale.length) videoPrompts.notes.push({ key: "video-prompt-stale", tone: "warn", text: t("dashboard:video_prompt_stale_hint", { count: stale.length }) });
  rows.push(videoPrompts);
  if (status.artifacts.audio && status.artifacts.audio.state !== "not_applicable") rows.push(row("voice", t("workflow:row_voice"), status.artifacts.audio, []));
  rows.push(row("videos", t("workflow:row_videos"), status.artifacts.videos, ["video"]), editRow(facts, ctx));
  return rows.map(withRunning);
}

function buildNext(facts: Facts, rows: StepRowView[], ctx: StepListContext): NextStepView | null {
  const action = facts.plan.next_action;
  if (action.type === "none" || facts.plan.blockers.length > 0) return null;
  const owners: Partial<Record<WorkflowNextAction["type"], StepRowKey>> = {
    collect_project_input: "source", prepare_episode_assets: "assets", generate_asset_sheets: "assets",
    plan_short_drama_storyboards: "script", patch_episode_script: "script", author_prompts: "prompts",
    generate_shot_grids: "materials", author_video_prompts: "video_prompt", generate_videos: "videos",
    create_edit_timeline: "edit", configure_provider: "videos", retry: "videos", fix_input: "videos",
    generate_dependency: "videos", repair_artifact_state: "videos", retry_artifact_download: "videos", confirm_request_duration: "videos",
  };
  const rowKey = action.type === "wait_for_task" ? rows.find(row => row.steps.some(step => step.state === "active"))?.key : owners[action.type];
  if (!rowKey) return null;
  const { t } = ctx;
  const base: NextStepView = {
    rowKey, actionType: action.type, title: t("workflow:action_" + action.type),
    detail: null, instruction: null, primary: [], alternatives: [], hint: null,
  };
  const boardPath = "/" + WORKSPACE_ROUTE_EPISODES + "/" + ctx.episodeId + "?" + EPISODE_VIEW_PARAM + "=board";
  switch (action.type) {
    case "collect_project_input":
      return { ...base, primary: [{ key: "factory", label: t("dashboard:short_drama_factory_generate"), kind: "nav", intent: { type: "route", path: "/" } }] };
    case "prepare_episode_assets":
      return { ...base, detail: t("workflow:next_detail_prepare_episode_assets"), primary: [{ key: "assets", label: t("workflow:act_open_assets_view"), kind: "nav", intent: { type: "route", path: episodeAssetsViewPath(ctx.episodeId) } }] };
    case "plan_short_drama_storyboards":
      return { ...base, detail: t("workflow:next_detail_plan_short_drama_storyboards"), primary: [{ key: "board", label: t("dashboard:short_drama_ai_storyboard"), kind: "nav", intent: { type: "route", path: boardPath } }] };
    case "author_prompts":
      return { ...base, instruction: { initial: ctx.savedPromptInstructions, persist: "prompt_authoring" }, primary: [
        { key: "agent", label: t("workflow:act_agent"), kind: "agent", intent: { type: "author_prompts_to_agent" } },
        ...(ctx.canAuthorPrompts ? [{ key: "author", label: t("workflow:act_author_prompts"), kind: "ai", intent: { type: "author_prompts" } } satisfies StepAct] : []),
      ] };
    case "author_video_prompts":
      return { ...base, primary: [{ key: "video-prompts", label: t("workflow:act_author_video_prompts", { count: action.requested_ids.length }), kind: "ai", intent: { type: "open_video_prompts", entryIds: action.requested_ids } }] };
    case "generate_asset_sheets":
      return { ...base, primary: [{ key: "asset-batch", label: t("workflow:act_asset_batch", { count: action.requested_ids.length }), kind: "ai", intent: { type: "asset_batch", episodeId: ctx.episodeId } }] };
    case "generate_shot_grids":
    case "generate_videos": {
      const kind = action.type === "generate_videos" ? "videos" : "shot-grids";
      return { ...base, primary: [{ key: "batch", label: t("dashboard:batch_generate_" + kind), kind: "ai", intent: { type: "storyboard_batch", episodeId: ctx.episodeId, kind } }] };
    }
    case "create_edit_timeline":
      return { ...base, instruction: { initial: "", persist: null }, primary: editActs(facts, ctx) };
    case "configure_provider":
      return { ...base, primary: [{ key: "settings", label: t("workflow:act_open_settings"), kind: "nav", intent: { type: "route", path: "~" + ROUTE_APP_SETTINGS } }] };
    case "wait_for_task":
    case "confirm_request_duration": return base;
    default: return { ...base, primary: [agentAct(t, t("workflow:agent_prefill_admission", { episodeRef: ctx.episodeRef }))] };
  }
}

export function buildStepList(plan: WorkflowPlan, ctx: StepListContext): StepListView | null {
  const content = plan.status.content;
  if (!content) return null;
  const facts: Facts = { plan, content, steps: new Map(plan.steps.map(step => [step.id, step])) };
  const rows = buildRows(facts, ctx), next = buildNext(facts, rows, ctx);
  const summaryRow = next ? rows.find(row => row.key === next.rowKey) : rows.find(row => row.tone === "danger" || row.tone === "warn");
  const summary = summaryRow ? ctx.t("workflow:summary_row", { title: summaryRow.title, status: summaryRow.status }) : content.episode_complete ? ctx.t("workflow:summary_complete") : "";
  return { rows, next, summary };
}
