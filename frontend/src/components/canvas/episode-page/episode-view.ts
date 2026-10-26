/** 工厂集页依次展示剧本、资产、分镜和剪辑；旧 URL 取值回到默认视图。 */
import { EPISODE_VIEW_EDIT, EPISODE_VIEW_PARAM, EPISODE_VIEW_SOURCE } from "@/app-routes";

export const EPISODE_VIEWS = [EPISODE_VIEW_SOURCE, "assets", "board", EPISODE_VIEW_EDIT] as const;
export type EpisodeView = (typeof EPISODE_VIEWS)[number];
export type CanvasView = "board";

export interface EpisodeViewFacts {
  hasScript: boolean;
  demo: boolean;
}

/**
 * 页签始终完整可点，不按流程进度置灰：步骤的前置关系由视图内的空状态说明下一步
 * （资产 → 先补剧本，剪辑 → 先生成视频），禁用的页签只说「不能点」，说不出下一步。
 */
export function episodeViewTabs(facts: EpisodeViewFacts): EpisodeView[] {
  return facts.demo ? ["board"] : [...EPISODE_VIEWS];
}

export function defaultEpisodeView(facts: EpisodeViewFacts): EpisodeView {
  return facts.demo || facts.hasScript ? "board" : EPISODE_VIEW_SOURCE;
}

export function resolveEpisodeView(requested: string | null, facts: EpisodeViewFacts): EpisodeView {
  const tab = episodeViewTabs(facts).find((entry) => entry === requested);
  return tab ?? defaultEpisodeView(facts);
}

export function withEpisodeView(params: URLSearchParams, next: EpisodeView, facts: EpisodeViewFacts): URLSearchParams {
  const result = new URLSearchParams(params);
  if (next === defaultEpisodeView(facts)) result.delete(EPISODE_VIEW_PARAM);
  else result.set(EPISODE_VIEW_PARAM, next);
  return result;
}

export function staysInEpisodeView(to: string, here: string, facts: EpisodeViewFacts): boolean {
  const current = new URL(here, "http://episode-view.invalid");
  const target = new URL(to, current);
  return target.pathname === current.pathname &&
    resolveEpisodeView(target.searchParams.get(EPISODE_VIEW_PARAM), facts) ===
    resolveEpisodeView(current.searchParams.get(EPISODE_VIEW_PARAM), facts);
}
