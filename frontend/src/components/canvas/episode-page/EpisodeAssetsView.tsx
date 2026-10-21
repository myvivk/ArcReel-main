import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation } from "wouter";
import { Button } from "@/components/ui/button";
import { AssetGallery } from "@/components/canvas/lorebook/AssetGallery";
import { EpisodeAssetGenerateDialog } from "@/components/canvas/episode-page/EpisodeAssetGenerateDialog";
import { useProjectsStore } from "@/stores/projects-store";
import { useAssetSheetStatus } from "@/components/canvas/lorebook/useAssetSheetStatus";
import { useActiveResourceIds } from "@/stores/tasks-store";
import { enqueueCharacter, enqueueProp, enqueueScene } from "@/actions/generation";
import { refreshAfterWrite } from "@/components/canvas/shared/refreshAfterWrite";
import { useAppStore } from "@/stores/app-store";
import { API } from "@/api";
import type { AssetSheetType, EpisodeReferencedAssets, ProjectData } from "@/types";
import { errMsg } from "@/utils/async";

const TYPES: AssetSheetType[] = ["character", "scene", "prop"];

function bucketKeyOf(assetType: AssetSheetType): "characters" | "scenes" | "props" {
  if (assetType === "character") return "characters";
  return assetType === "scene" ? "scenes" : "props";
}

export function EpisodeAssetsView({
  projectName,
  episode,
  projectData,
}: {
  projectName: string;
  episode: number;
  projectData: ProjectData | null;
}) {
  const { t } = useTranslation(["assets", "dashboard"]);
  const [, setLocation] = useLocation();
  const snapshotRevision = useProjectsStore((state) => state.projectSnapshotRevisions[projectName] ?? 0);
  const [result, setResult] = useState<EpisodeReferencedAssets | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const [activeType, setActiveType] = useState<AssetSheetType>("character");
  const [dialogOpen, setDialogOpen] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    API.getEpisodeReferencedAssets(projectName, episode, { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) {
          setResult(data);
          setError(null);
        }
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setError(errMsg(err));
      });
    return () => controller.abort();
  }, [projectName, episode, snapshotRevision, retry]);

  const statuses = useAssetSheetStatus(projectName);
  // 与全局资产页同一接线：按当前 tab 取该类资产在途生成名，卡片与详情据此显示占用态
  const generatingNames = useActiveResourceIds(activeType, projectName);

  // 单张生成 / 重生成：本集引用到的资产与全局页同一入口（一键之外的补救路径——缺描述补描述、
  // 缺图或过期补图，都在卡片「更多」与详情页里，和侧栏全局资产页完全一致）
  const handleGenerateAsset = (name: string) => {
    const enqueue = activeType === "character" ? enqueueCharacter : activeType === "scene" ? enqueueScene : enqueueProp;
    void enqueue(projectName, name).catch((err: unknown) => {
      useAppStore.getState().pushToast(errMsg(err), "error");
    });
  };
  // 大图查看器里还原历史版本后刷新项目数据（查看器自己的版本列表由它自己重载）
  const handleRestoreVersion = async () => {
    await refreshAfterWrite(projectName, t);
  };

  const byType = useMemo(() => {
    const grouped: Record<AssetSheetType, Record<string, Record<string, unknown>>> = {
      character: {},
      scene: {},
      prop: {},
      product: {},
    };
    for (const item of result?.assets ?? []) grouped[item.asset_type][item.asset_name] = item.asset;
    return grouped;
  }, [result]);

  const goToAll = () => setLocation(`/${activeType === "character" ? "characters" : activeType === "scene" ? "scenes" : "props"}`);
  const scriptPath = `/episodes/${episode}`;
  const episodeTitle =
    projectData?.episodes?.find((item) => item.episode === episode)?.title ?? `第 ${episode} 集`;

  if (error) {
    return (
      <div className="flex flex-col items-center gap-3 p-8" role="alert">
        <p>{t("episode_assets_load_failed", { ns: "dashboard", message: error })}</p>
        <Button
          variant="outline"
          onClick={() => {
            setError(null);
            setResult(null);
            setRetry((value) => value + 1);
          }}
        >
          {t("retry", { ns: "dashboard" })}
        </Button>
      </div>
    );
  }
  if (!result) {
    return (
      <div className="p-8 text-sm text-muted-foreground" role="status">
        {t("episode_assets_loading", { ns: "dashboard" })}
      </div>
    );
  }

  const title = t(`type.${activeType}`);
  const assets = byType[activeType];
  const all = projectData?.[bucketKeyOf(activeType)] ?? {};
  const total = Object.keys(all).length;

  /** 页内唯一的「生成本集资产」入口：点开确认框，确认后才真正生成。 */
  const generateButton = (
    <Button variant="default" size="sm" onClick={() => setDialogOpen(true)}>
      {t("episode_assets_generate", { ns: "dashboard" })}
    </Button>
  );

  return (
    <section className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <nav aria-label={t("episode_assets_types", { ns: "dashboard" })} className="flex shrink-0 items-center gap-1 border-b px-4 py-2">
        {TYPES.map((type) => {
          const count = Object.keys(byType[type]).length;
          return (
            <Button key={type} variant={activeType === type ? "secondary" : "ghost"} size="sm" onClick={() => setActiveType(type)} aria-pressed={activeType === type}>
              {t(`type.${type}`)}{" "}
              <span className="ml-1 text-xs text-muted-foreground">
                {t("episode_assets_scope_badge", {
                  ns: "dashboard",
                  count,
                  total: Object.keys(projectData?.[bucketKeyOf(type)] ?? {}).length,
                })}
              </span>
            </Button>
          );
        })}
        <div className="ml-auto flex items-center gap-2">
          {generateButton}
          <Button variant="outline" size="sm" onClick={goToAll}>
            {t("episode_assets_all", { ns: "dashboard", type: title, count: total })}
          </Button>
        </div>
      </nav>
      {Object.keys(assets).length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-center">
          {result.has_script ? (
            <>
              <p className="text-sm font-medium">{t("episode_assets_empty", { ns: "dashboard", type: title })}</p>
              <p className="text-sm text-muted-foreground">{t("episode_assets_empty_hint", { ns: "dashboard" })}</p>
              <Button variant="outline" size="sm" onClick={goToAll}>
                {t("episode_assets_all", { ns: "dashboard", type: title, count: total })}
              </Button>
            </>
          ) : (
            <>
              <p className="text-sm font-medium">{t("episode_assets_no_script", { ns: "dashboard" })}</p>
              <p className="text-sm text-muted-foreground">{t("episode_assets_no_script_hint", { ns: "dashboard" })}</p>
              <Button variant="default" size="sm" onClick={() => setLocation(scriptPath)}>
                {t("episode_assets_go_script", { ns: "dashboard" })}
              </Button>
            </>
          )}
        </div>
      ) : (
        <AssetGallery
          projectName={projectName}
          assetType={activeType}
          title={title}
          assets={assets as never}
          generatingNames={generatingNames}
          readOnly={false}
          onGenerate={handleGenerateAsset}
          onRestoreVersion={handleRestoreVersion}
          statusRows={statuses.filter((row) => row.asset_type === activeType && Object.hasOwn(assets, row.name))}
          batchScope={{ episode_id: episode }}
          hideAddActions
        />
      )}
      {dialogOpen && (
        <EpisodeAssetGenerateDialog
          projectName={projectName}
          episode={episode}
          episodeTitle={episodeTitle}
          plan={result.plan}
          onClose={() => setDialogOpen(false)}
          onSubmitted={() => setRetry((value) => value + 1)}
        />
      )}
    </section>
  );
}
