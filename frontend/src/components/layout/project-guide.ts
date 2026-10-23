import type { TFunction } from "i18next";

import { episodesViewPath } from "@/components/canvas/episodes/episodes-view-model";
import type { EpisodeMeta } from "@/types";
import type { WorkflowNextAction, WorkflowStatus } from "@/types/workflow";
import { episodeDisplayName, episodePosition, type EpisodeLedger } from "@/utils/episode-display";

/**
 * 顶栏状态条「下一步」的呈现形状：由项目层制作状态的 `next_action` 与 `next_alternatives` 投影而来，
 * 界面不自行推断下一步。
 *
 * 按钮分三种：`nav` 跳到工作台里能完成这一步的位置；`agent` 把请求预填进 Agent 输入框，由用户确认发送；
 * `ai` 直接提交这一步（目前只有 AI 规划分集）。还没有 Web 直接调用入口的动作只给 `agent`。
 */
export type GuideButton =
  | { kind: "nav"; label: string; to: string }
  | { kind: "agent"; label: string; prefill: string };

export interface ProjectNextGuide {
  /** 状态条右段「下一步」后面的短语。 */
  title: string;
  /** 弹层里的一句说明。 */
  detail: string;
  /** 是否带附加指令输入框；附加指令只写进交给 Agent 的消息。 */
  instruction: boolean;
  primary: GuideButton[];
  alternatives: GuideButton[];
  /** 下一步落在哪一集（集 ID）；项目层动作为 null。 */
  episodeId: number | null;
}

/** 下一步动作的祈使短语，与集页面板同一套译文。 */
export function actionPhrase(t: TFunction, action: WorkflowNextAction["type"]): string {
  return t(`workflow:action_${action}`, { defaultValue: t("workflow:action_unknown") });
}

function episodeIdArg(action: WorkflowNextAction): number | null {
  const value = action.args.episode_id;
  return typeof value === "number" ? value : null;
}

export function projectNextGuide(t: TFunction, status: WorkflowStatus, episodes: EpisodeLedger): ProjectNextGuide | null {
  const action = status.next_action;
  const allowed = new Set<WorkflowNextAction["type"]>([
    "collect_project_input", "create_episode", "prepare_episode_assets", "plan_short_drama_storyboards",
    "author_prompts", "generate_asset_sheets", "generate_shot_grids", "author_video_prompts", "generate_videos", "create_edit_timeline",
    "patch_episode_script", "wait_for_task", "retry", "fix_input", "generate_dependency", "configure_provider", "repair_artifact_state", "retry_artifact_download",
  ]);
  if (!allowed.has(action.type)) return null;
  const episodeId = episodeIdArg(action);
  if (episodeId !== null) {
    const position = episodePosition(episodes, episodeId);
    const name = episodeDisplayName(episodes, episodeId, t);
    return {
      title: position === null ? t("dashboard:guide_continue_named_episode", { name }) : t("dashboard:guide_continue_episode", { position }),
      detail: t("dashboard:guide_episode_detail", { name, step: actionPhrase(t, action.type) }), instruction: false,
      primary: [{ kind: "nav", label: position === null ? t("dashboard:guide_go_named_episode", { name }) : t("dashboard:guide_go_episode", { position }), to: "/episodes/" + episodeId }],
      alternatives: [], episodeId,
    };
  }
  return {
    title: actionPhrase(t, action.type), detail: t("workflow:next_step", { step: actionPhrase(t, action.type) }), instruction: false,
    primary: [{ kind: "nav", label: actionPhrase(t, action.type), to: action.type === "create_episode" ? episodesViewPath({ create: true }) : "/" }],
    alternatives: [], episodeId: null,
  };
}

/** 交给 Agent 的消息：附加指令非空时另起一行附上。 */
export function withInstruction(t: TFunction, prefill: string, instruction: string): string {
  const text = instruction.trim();
  return text ? `${prefill}\n${t("dashboard:guide_prefill_instruction", { instruction: text })}` : prefill;
}

/** 集的产物里有比当前内容旧的（分镜图或视频）。 */
export function episodeNeedsUpdate(episode: Pick<EpisodeMeta, "storyboards" | "videos">): boolean {
  return (episode.storyboards?.stale ?? 0) + (episode.videos?.stale ?? 0) > 0;
}
