import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearch } from "wouter";
import { Button } from "@/components/ui/button";
import { useScrollTarget } from "@/hooks/useScrollTarget";
import { ONBOARDING_ANCHORS } from "@/onboarding/anchors";
import { useDemoWorkbench } from "@/onboarding/use-demo-workbench";
import type { AssetSheetType, WorkspaceFocusTarget } from "@/types";
import { useAssetBusyNames } from "./assetBusyGuard";
import { AssetBrowseCard } from "./AssetBrowseCard";
import { AssetEditorSheet } from "./AssetEditorSheet";
import { AssetImageViewer } from "./AssetImageViewer";
import { AssetSheetBatchControls } from "./AssetSheetBatchControls";
import { GalleryEmptyState } from "./GalleryEmptyState";
import { GalleryToolbar } from "./GalleryToolbar";
import {
  markerOf,
  matchesGalleryFilter,
  toGalleryAsset,
  type GalleryAssetSource,
  type GalleryFilter,
} from "./gallery-model";
import { useAssetSheetStatus, useSheetStatusByName } from "./useAssetSheetStatus";

export interface AssetGalleryProps<T extends GalleryAssetSource> {
  projectName: string;
  assetType: AssetSheetType;
  title: string;
  assets: Record<string, T>;
  /** 有资产图任务在跑的资产名。 */
  generatingNames?: Set<string>;
  /** 只读展示（引导演示项目）：不渲染生成、上传、删除等改写入口。 */
  readOnly: boolean;
  onGenerate: (name: string) => void;
  onRestoreVersion?: () => Promise<unknown> | void;
  onReload?: () => Promise<unknown> | void;
  /** 仅提供本集引用资产时限定批量生成范围；省略时维持类型全局范围。 */
  batchScope?: { episode_id: number };
  /** 已经按引用过滤后的状态行，避免画廊再载入全集状态用于集内批量计数。 */
  statusRows?: import("@/types").AssetSheetStatusRow[];
}

/**
 * 角色、场景、道具、商品共用的画廊：工具栏、按画布宽度加列的浏览卡网格、详情 Sheet 与大图查看。
 * 画廊只负责浏览与维护已有资产，点卡片打开详情，次要操作在卡片的「更多」里；
 * 资产由各集生成后汇总到这里，画廊不提供新增、导入、入库或合并入口。
 */
export function AssetGallery<T extends GalleryAssetSource>({
  projectName,
  assetType,
  title,
  assets,
  generatingNames,
  readOnly,
  onGenerate,
  onRestoreVersion,
  onReload,
  batchScope,
  statusRows,
}: AssetGalleryProps<T>) {
  const { t } = useTranslation("assets");
  const occupiedNames = useAssetBusyNames(assetType, projectName);
  // 演示项目不在服务端，没有资产图状态可取
  const demo = useDemoWorkbench();
  const loadedRows = useAssetSheetStatus(projectName, !demo && !statusRows);
  const rows = statusRows ?? loadedRows;
  const statusByName = useSheetStatusByName(rows, assetType);
  const [filter, setFilter] = useState<GalleryFilter>("all");
  const [editorName, setEditorName] = useState<string | null>(null);
  const [viewName, setViewName] = useState<string | null>(null);
  // 卡片上在途的上传、版本恢复与删除：查看器里的还原与它们互斥
  const [writingNames, setWritingNames] = useState<ReadonlySet<string>>(() => new Set());
  const handleWritingChange = useCallback((name: string, writing: boolean) => {
    setWritingNames((current) => {
      if (current.has(name) === writing) return current;
      const next = new Set(current);
      if (writing) next.add(name);
      else next.delete(name);
      return next;
    });
  }, []);

  const items = useMemo(
    () => Object.entries(assets).map(([name, source]) => ({ asset: toGalleryAsset(assetType, name, source), source })),
    [assets, assetType],
  );
  const shown = items.filter(({ asset }) => matchesGalleryFilter(asset, statusByName.get(asset.name), filter));
  const shownNames = shown.map(({ asset }) => asset.name);
  const filterCounts = {
    pending: items.filter(({ asset }) => matchesGalleryFilter(asset, statusByName.get(asset.name), "pending")).length,
    stale: items.filter(({ asset }) => matchesGalleryFilter(asset, statusByName.get(asset.name), "stale")).length,
  };

  // Agent 改动某个资产时定位到它的卡片；被筛选藏起来时先回到「全部」。
  const prepareTarget = useCallback(
    (target: WorkspaceFocusTarget) => {
      if (!(target.id in assets)) return false;
      setFilter("all");
      return true;
    },
    [assets],
  );
  useScrollTarget(assetType, { prepareTarget });

  const openAsset = useCallback((name: string) => setEditorName(name), []);

  // 「去补描述」等入口带 ?focus=名字 进来：直接打开那条资产的详情；关掉后同一 URL 不再弹。
  // 放到 effect 的宏任务里执行，避免渲染期同步 setState（React 会对成串更新告警）。
  const search = useSearch();
  const handledFocus = useRef<string | null>(null);
  useEffect(() => {
    const focus = new URLSearchParams(search).get("focus");
    if (!focus || handledFocus.current === focus || !(focus in assets)) return;
    handledFocus.current = focus;
    const timer = window.setTimeout(() => {
      setFilter("all");
      setEditorName(focus);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [search, assets]);
  // 单张生成只随只读收起：集内视图与全局页一样能生成 / 重生成单张资产图
  const generateAsset = readOnly ? () => undefined : onGenerate;
  const viewAsset = useCallback((name: string) => setViewName(name), []);
  // 查看器只在有资产图的资产之间切换，顺序与筛选后的网格一致；判据与卡片的「查看大图」相同
  const viewable = shown
    .map(({ asset }) => asset)
    .filter((asset) => asset.sheetPath !== null && statusByName.get(asset.name)?.status !== "missing");
  // 查看中的资产离开可查看列表时查看器随之关闭，同时结束查看：之后它重新可查看也不再自行弹出
  if (viewName !== null && !viewable.some((asset) => asset.name === viewName)) setViewName(null);
  const viewerBusy = new Set([...writingNames, ...occupiedNames]);
  generatingNames?.forEach((name) => viewerBusy.add(name));
  const editFromViewer = (name: string) => {
    setViewName(null);
    setEditorName(name);
  };

  return (
    <section aria-label={title} className="relative flex min-h-0 flex-1 flex-col overflow-y-auto [scrollbar-gutter:stable]">
      <GalleryToolbar
        title={title}
        count={items.length}
        filter={filter}
        onFilterChange={setFilter}
        filterCounts={filterCounts}
      >
        {(!readOnly || batchScope) && <AssetSheetBatchControls projectName={projectName} assetType={assetType} rows={rows} scope={batchScope} />}
      </GalleryToolbar>
      <div className="flex shrink-0 flex-col px-5 py-5" data-onboarding={ONBOARDING_ANCHORS.workbenchLorebook}>
        {items.length === 0 ? (
          <GalleryEmptyState assetType={assetType} />
        ) : shown.length === 0 ? (
          <div className="flex flex-col items-center gap-3 py-12 text-center text-sm text-muted-foreground">
            <p>{t("gallery_no_match")}</p>
            <Button variant="outline" size="sm" onClick={() => setFilter("all")}>
              {t("gallery_show_all")}
            </Button>
          </div>
        ) : (
          <ul aria-label={title} className="grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-4">
            {shown.map(({ asset }) => {
              const row = statusByName.get(asset.name);
              const generating = generatingNames?.has(asset.name) ?? false;
              return (
                <li key={asset.name} className="flex min-w-0">
                  <AssetBrowseCard
                    projectName={projectName}
                    asset={asset}
                    sheetStatus={row}
                    marker={markerOf(asset, row, generating)}
                    generating={generating}
                    busy={occupiedNames.has(asset.name)}
                    readOnly={readOnly}
                    onOpen={openAsset}
                    onView={viewAsset}
                    onGenerate={generateAsset}
                    onRestoreVersion={onRestoreVersion}
                    onReload={onReload}
                    onWritingChange={handleWritingChange}
                  />
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <AssetEditorSheet
        projectName={projectName}
        assetType={assetType}
        assets={assets}
        name={editorName}
        onNameChange={setEditorName}
        order={shownNames}
        statusByName={statusByName}
        generatingNames={generatingNames}
        writingNames={writingNames}
        readOnly={readOnly}
        onGenerate={generateAsset}
      />

      <AssetImageViewer
        projectName={projectName}
        assets={viewable}
        name={viewName}
        onNameChange={setViewName}
        onEdit={readOnly ? undefined : editFromViewer}
        busyNames={viewerBusy}
        readOnly={readOnly}
        onRestoreVersion={onRestoreVersion}
      />
    </section>
  );
}
