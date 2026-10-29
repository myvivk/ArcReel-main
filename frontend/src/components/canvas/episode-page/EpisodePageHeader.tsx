import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

import { EpisodeHeaderActionSlot } from "./EpisodeHeaderActions";
import type { EpisodeView } from "./episode-view";

/** 视图 tab 的元素 id，视图区以它作为无障碍名。 */
export function episodeViewTabId(view: EpisodeView): string {
  return `episode-view-tab-${view}`;
}

/**
 * 集页页头，两行：第一行是视图 tab 与行尾的制作进度入口，第二行是集头。
 * 当前视图的动作（批量补齐、编写提示词、出片等）不再占页头一行，放进制作进度面板里，
 * 由各视图经 `EpisodeHeaderActions` 放进插槽，页头不认识具体动作。
 *
 * 页签不按流程进度置灰：还没到那一步时，视图内的空状态说明缺什么。
 */
export function EpisodePageHeader({
  head,
  progress,
  tabs,
  view,
  onViewChange,
  boardLabel,
  sourceLabel,
}: {
  head: ReactNode;
  /** 制作进度入口；演示项目没有。 */
  progress: ReactNode;
  tabs: EpisodeView[];
  view: EpisodeView;
  onViewChange: (view: EpisodeView) => void;
  /** 「分镜」tab 的名称：参考生视频项目称「视频单元」。 */
  boardLabel: string;
  /**
   * 「原文」tab 的名称。短剧工厂导入的集没有集原文，这一页是剧本内容，故给「剧本」，
   * 沿用集页「页签名按项目与这一集的事实取」的做法（同 boardLabel）。
   */
  sourceLabel?: string;
}) {
  const { t } = useTranslation("dashboard");
  const label: Record<EpisodeView, string> = {
    assets: t("episode_view_assets"),
    board: boardLabel,
    source: sourceLabel ?? t("episode_view_source"),
    edit: t("episode_view_edit"),
  };
  return (
    <header className="shrink-0 border-b border-border">
      {/* 视图 tab 在上、集头在下；制作进度入口留在右上角不动。 */}
      <div className="flex h-10 items-center gap-3 px-4">
        <Tabs value={view} onValueChange={(next: EpisodeView) => onViewChange(next)} className="shrink-0">
          <TabsList aria-label={t("episode_view_aria")}>
            {tabs.map((tab) => (
              <TabsTrigger key={tab} value={tab} id={episodeViewTabId(tab)}>
                {label[tab]}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
        <span className="flex-1" />
        {progress}
      </div>
      <div className="flex h-11 items-center gap-3 px-4">
        {head}
        <span className="flex-1" />
        <EpisodeHeaderActionSlot className="flex min-w-0 items-center justify-end gap-1.5" />
      </div>
    </header>
  );
}
