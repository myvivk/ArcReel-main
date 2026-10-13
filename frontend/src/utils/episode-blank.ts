import type { EpisodeMeta } from "@/types";

/** 本集还是空的：既没有脚本规划中间稿，也没有正式剧本，也没有自带原文。 */
function isBlankEpisode(meta: EpisodeMeta): boolean {
  if (meta.script_status === "segmented" || meta.script_status === "generated") return false;
  // 自带原文的集已经有内容要规划，不当作空白集覆盖掉。
  const hasSource = meta.source_range != null || (meta.source_origin != null && meta.source_origin !== "none");
  return !hasSource;
}

/**
 * 播出顺序末尾连续的一段空白集：连写时优先填它们，填不满才补建新集。
 * 只取末尾连续的一段——填中段的空白集会把新故事插进已有内容之间，反而打断连续性。
 * 全部为空时就是整个账本。
 */
export function trailingBlankEpisodes(metas: readonly EpisodeMeta[] | undefined): number[] {
  const list = metas ?? [];
  const blank: number[] = [];
  for (let i = list.length - 1; i >= 0; i--) {
    if (!isBlankEpisode(list[i])) break;
    blank.unshift(list[i].episode);
  }
  return blank;
}
