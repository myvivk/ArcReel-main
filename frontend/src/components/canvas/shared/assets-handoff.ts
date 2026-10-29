import type { TFunction } from "i18next";

/**
 * 资产步骤「交给 Agent」的预填指令：制作进度与资产页共用同一份文案，避免两个入口给出不同要求。
 * 导入原文项目没有工厂人物圣经，换成「读本集剧本源稿 + 已登记资产」的版本。
 */
export function prepareEpisodeAssetsPrompt(
  t: TFunction,
  episodeRef: string,
  sourceImport: boolean,
): string {
  const base = t(
    sourceImport
      ? "workflow:agent_prefill_prepare_episode_assets_import"
      : "workflow:agent_prefill_prepare_episode_assets",
    { episodeRef },
  );
  return `${base}\n${t("workflow:agent_prefill_prepare_episode_assets_media_warning")}`;
}
