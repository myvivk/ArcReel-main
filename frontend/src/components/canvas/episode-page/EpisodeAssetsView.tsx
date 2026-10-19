import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation } from "wouter";
import { Loader2 } from "lucide-react";
import { API } from "@/api";
import { Button } from "@/components/ui/button";
import { AssetGallery } from "@/components/canvas/lorebook/AssetGallery";
import { useAppStore } from "@/stores/app-store";
import { useAssetSheetBatchStore } from "@/stores/asset-sheet-batch-store";
import { useProjectsStore } from "@/stores/projects-store";
import type { AssetSheetStatusRow, AssetSheetType, ProjectData } from "@/types";
import { errMsg } from "@/utils/async";

type ReferencedAssetResponse = {
  episode: number;
  has_script: boolean;
  assets: { asset_type: AssetSheetType; asset_name: string; asset: Record<string, unknown> }[];
};

const TYPES: AssetSheetType[] = ["character", "scene", "prop"];

export function EpisodeAssetsView({ projectName, episode, projectData }: { projectName: string; episode: number; projectData: ProjectData | null }) {
  const { t } = useTranslation(["assets", "dashboard"]);
  const [, setLocation] = useLocation();
  const snapshotRevision = useProjectsStore((state) => state.projectSnapshotRevisions[projectName] ?? 0);
  const [result, setResult] = useState<ReferencedAssetResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const [activeType, setActiveType] = useState<AssetSheetType>("character");
  const [statuses, setStatuses] = useState<AssetSheetStatusRow[]>([]);
  const [generating, setGenerating] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    API.getEpisodeReferencedAssets(projectName, episode, { signal: controller.signal })
      .then((data) => { if (!controller.signal.aborted) { setResult(data); setError(null); } })
      .catch((err: unknown) => { if (!controller.signal.aborted) setError(errMsg(err)); });
    return () => controller.abort();
  }, [projectName, episode, snapshotRevision, retry]);

  useEffect(() => {
    const controller = new AbortController();
    API.getAssetSheetStatus(projectName, { signal: controller.signal })
      .then((data) => { if (!controller.signal.aborted) setStatuses(data.assets ?? []); })
      .catch(() => { if (!controller.signal.aborted) setStatuses([]); });
    return () => controller.abort();
  }, [projectName, snapshotRevision]);

  const byType = useMemo(() => {
    const grouped: Record<AssetSheetType, Record<string, Record<string, unknown>>> = { character: {}, scene: {}, prop: {}, product: {} };
    for (const item of result?.assets ?? []) grouped[item.asset_type][item.asset_name] = item.asset;
    return grouped;
  }, [result]);

  const goToAll = () => setLocation(`/${activeType === "character" ? "characters" : activeType === "scene" ? "scenes" : "props"}`);
  const scriptPath = `/episodes/${episode}`;

  /** 一键：抽取本集剧本引用 → 补登记未登记的资产 → 按集提交资产图批量生成。 */
  const handleGenerate = async () => {
    setGenerating(true);
    try {
      const result = await API.generateEpisodeAssets(projectName, episode);
      const members = result.members.filter((member) => member.status !== "blocked");
      if (members.length > 0) {
        useAssetSheetBatchStore.getState().track({ batchId: result.batch_id, projectName, members });
      }
      const queuedCount = members.filter((member) => member.task_id).length;
      const registeredCount = Object.values(result.registered).reduce(
        (sum, names) => sum + (names?.length ?? 0),
        0,
      );
      if (queuedCount > 0) {
        useAppStore.getState().pushToast(t("sheet_batch_submitted", { ns: "assets", count: queuedCount }), "success");
      }
      if (registeredCount > 0) {
        useAppStore
          .getState()
          .pushToast(t("episode_assets_registered", { ns: "dashboard", count: registeredCount }), "success");
      }
      if (result.without_description.length > 0) {
        useAppStore
          .getState()
          .pushToast(
            t("episode_assets_missing_description", {
              ns: "dashboard",
              count: result.without_description.length,
            }),
            "warning",
          );
      }
      setRetry((value) => value + 1);
    } catch (err) {
      useAppStore.getState().pushToast(errMsg(err), "error");
    } finally {
      setGenerating(false);
    }
  };

  const generateButton = (
    <Button variant="default" size="sm" disabled={generating} onClick={() => void handleGenerate()}>
      {generating && <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />}
      {generating ? t("episode_assets_generating", { ns: "dashboard" }) : t("episode_assets_generate", { ns: "dashboard" })}
    </Button>
  );
  if (error) return <div className="flex flex-col items-center gap-3 p-8" role="alert"><p>{t("episode_assets_load_failed", { ns: "dashboard", message: error })}</p><Button variant="outline" onClick={() => { setError(null); setResult(null); setRetry((value) => value + 1); }}>{t("retry", { ns: "dashboard" })}</Button></div>;
  if (!result) return <div className="p-8 text-sm text-muted-foreground" role="status">{t("episode_assets_loading", { ns: "dashboard" })}</div>;

  const title = t(`type.${activeType}`);
  const assets = byType[activeType];
  const all = projectData?.[activeType === "character" ? "characters" : activeType === "scene" ? "scenes" : "props"] ?? {};
  const total = Object.keys(all).length;
  return (
    <section className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <nav aria-label={t("episode_assets_types", { ns: "dashboard" })} className="flex shrink-0 items-center gap-1 border-b px-4 py-2">
        {TYPES.map((type) => {
          const count = Object.keys(byType[type]).length;
          return <Button key={type} variant={activeType === type ? "secondary" : "ghost"} size="sm" onClick={() => setActiveType(type)} aria-pressed={activeType === type}>
            {t(`type.${type}`)} <span className="ml-1 text-xs text-muted-foreground">{count} / {Object.keys(projectData?.[type === "character" ? "characters" : type === "scene" ? "scenes" : "props"] ?? {}).length}</span>
          </Button>;
        })}
        <div className="ml-auto flex items-center gap-2">
          {generateButton}
          <Button variant="outline" size="sm" onClick={goToAll}>{t("episode_assets_all", { ns: "dashboard", type: title, count: total })}</Button>
        </div>
      </nav>
      {Object.keys(assets).length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-center">
          {result.has_script ? (
            <>
              <p className="text-sm font-medium">{t("episode_assets_empty", { ns: "dashboard", type: title })}</p>
              <p className="text-sm text-muted-foreground">{t("episode_assets_empty_hint", { ns: "dashboard" })}</p>
              <div className="flex items-center gap-2">
                {generateButton}
                <Button variant="outline" size="sm" onClick={goToAll}>{t("episode_assets_all", { ns: "dashboard", type: title, count: total })}</Button>
              </div>
            </>
          ) : (
            <>
              <p className="text-sm font-medium">{t("episode_assets_no_script", { ns: "dashboard" })}</p>
              <p className="text-sm text-muted-foreground">{t("episode_assets_no_script_hint", { ns: "dashboard" })}</p>
              <Button variant="default" size="sm" onClick={() => setLocation(scriptPath)}>{t("episode_assets_go_script", { ns: "dashboard" })}</Button>
            </>
          )}
        </div>
      ) : (
        <AssetGallery
          projectName={projectName}
          assetType={activeType}
          title={title}
          assets={assets as never}
          readOnly
          onGenerate={() => undefined}
          statusRows={statuses.filter((row) => row.asset_type === activeType && Object.hasOwn(assets, row.name))}
          batchScope={{ episode_id: episode }}
          hideAddActions
        />
      )}
  </section>);
}
