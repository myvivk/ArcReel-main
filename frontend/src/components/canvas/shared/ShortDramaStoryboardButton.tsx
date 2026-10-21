import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "wouter";
import { AlertTriangle, Check, Loader2, Sparkles } from "lucide-react";
import { API } from "@/api";
import { reimportShortDramaSource, type ShortDramaSource } from "@/actions/shortDrama";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { refreshAfterWrite } from "@/components/canvas/shared/refreshAfterWrite";
import { useAppStore } from "@/stores/app-store";
import { errMsg } from "@/utils/async";
import {
  WORKSPACE_ROUTE_CHARACTERS,
  WORKSPACE_ROUTE_PROPS,
  WORKSPACE_ROUTE_SCENES,
} from "@/app-routes";

type MissingAsset = { type: string; name: string };

interface Props {
  projectName: string;
  episode: number;
  /** 本集现有分镜数：0 时叫「AI 分镜」，≥1 时叫「AI 重新分镜」。 */
  storyboardCount: number;
  /** 本集现有分镜 ID（scene_id）：重新分镜时可只勾选部分分镜重写，其余原样保留。 */
  sceneIds: string[];
  /** 页头动作用描边小按钮；空态里的主操作用实心按钮。 */
  prominent?: boolean;
}

const ASSET_ROUTE: Record<string, string> = {
  character: WORKSPACE_ROUTE_CHARACTERS,
  scene: WORKSPACE_ROUTE_SCENES,
  prop: WORKSPACE_ROUTE_PROPS,
};

/** ``E1S3`` → ``S3``：给分镜选择列表一个短标签。 */
function sceneLabel(sceneId: string): string {
  const match = /S(\d+)$/i.exec(sceneId);
  return match ? `S${match[1]}` : sceneId;
}

/**
 * 分镜页的「AI 分镜 / AI 重新分镜」入口：读本集已保存的 Markdown 源稿重建分镜。
 * AI 分镜是唯一生成方式（没有快速切分/机械回落），校验不过就带原因返修。
 * 本集引用的资产缺外观描述时入口置灰，提示先去资产页补齐；不新增资产、不生成图片或视频，
 * 视频提示词留待宫格确认后编写。
 */
export function ShortDramaStoryboardButton({
  projectName,
  episode,
  storyboardCount,
  sceneIds,
  prominent = false,
}: Props) {
  const { t } = useTranslation("dashboard");
  const [open, setOpen] = useState(false);
  const [source, setSource] = useState<ShortDramaSource | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [planningStrength, setPlanningStrength] = useState(2);
  const [forceRegenerate, setForceRegenerate] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{
    scenes: number;
    partial: boolean;
    missing: MissingAsset[];
    cached: boolean;
  } | null>(null);

  const hasStoryboards = storyboardCount > 0;
  const missing = source?.assets_missing_description ?? [];
  const blocked = missing.length > 0;

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setSource(await API.getShortDramaSource(projectName, episode));
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setLoading(false);
    }
  }, [projectName, episode]);

  // 缺描述资产的置灰判定要在入口渲染时就可用，因此挂载即拉取源稿（弹窗复用同一份）。
  useEffect(() => {
    let cancelled = false;
    // setState 不能同步发生在 effect 体里（react-hooks/set-state-in-effect），放进微任务。
    queueMicrotask(() => {
      if (!cancelled) void load();
    });
    return () => {
      cancelled = true;
    };
  }, [load]);

  const openDialog = () => {
    setResult(null);
    setError(null);
    setSelected(sceneIds);
    setOpen(true);
    void load();
  };

  const toggleScene = (sceneId: string, checked: boolean) => {
    setSelected((current) =>
      checked ? [...new Set([...current, sceneId])] : current.filter((id) => id !== sceneId),
    );
  };

  const allSelected = hasStoryboards && sceneIds.length > 0 && selected.length === sceneIds.length;
  /** 部分=勾选了既有分镜的真子集：按分镜重写，其余分镜原样保留。 */
  const partial = hasStoryboards && sceneIds.length > 0 && !allSelected;

  const submit = async () => {
    if (submitting) return;
    const revision = source?.revision;
    if (!revision) {
      setError(t("short_drama_ai_storyboard_source_missing"));
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const res = await reimportShortDramaSource(
        projectName,
        episode,
        revision,
        hasStoryboards,
        planningStrength,
        forceRegenerate,
        partial ? selected : undefined,
      );
      const resMissing = res.assets_without_description ?? [];
      setResult({
        scenes: partial ? res.regenerated_scenes ?? res.scenes : res.scenes,
        partial,
        missing: resMissing,
        cached: res.planning_cached,
      });
      if (resMissing.length > 0) {
        useAppStore.getState().pushToast(
          t("short_drama_ai_storyboard_missing", {
            count: resMissing.length,
            assets: resMissing.map((asset) => asset.name).join("、"),
          }),
          "warning",
        );
      }
      await refreshAfterWrite(projectName, t);
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setSubmitting(false);
    }
  };

  const missingLabel = (asset: MissingAsset) => {
    const type = t(`short_drama_asset_${asset.type}`);
    return type === `short_drama_asset_${asset.type}` ? asset.name : `${asset.name}（${type}）`;
  };

  const canSubmit =
    !loading && !submitting && Boolean(source?.revision) && (!hasStoryboards || selected.length > 0);

  const trigger = (
    <Button
      variant={prominent ? "default" : "outline"}
      size={prominent ? "default" : "sm"}
      onClick={openDialog}
      disabled={blocked}
    >
      <Sparkles aria-hidden data-icon="inline-start" />
      {t(hasStoryboards ? "short_drama_ai_restoryboard" : "short_drama_ai_storyboard")}
    </Button>
  );

  return (
    <>
      {blocked ? (
        <span
          title={t("short_drama_storyboard_blocked_missing", {
            count: missing.length,
            assets: missing.map(missingLabel).join("、"),
          })}
        >
          {trigger}
        </span>
      ) : (
        trigger
      )}
      <Dialog
        open={open}
        onOpenChange={(next) => {
          if (!next && !submitting) setOpen(false);
        }}
      >
        <DialogContent showCloseButton={!submitting}>
          <DialogHeader>
            <DialogTitle>
              {t(
                hasStoryboards
                  ? "short_drama_ai_restoryboard_title"
                  : "short_drama_ai_storyboard_title",
                { episode },
              )}
            </DialogTitle>
            <DialogDescription>{t("short_drama_ai_storyboard_desc")}</DialogDescription>
          </DialogHeader>
          <DialogBody>
            <div className="flex flex-col gap-3 text-sm">
              {loading ? (
                <p className="text-muted-foreground">{t("short_drama_source_loading")}</p>
              ) : source?.exists ? (
                <p className="text-muted-foreground">
                  {t("short_drama_ai_storyboard_source", {
                    path: source.path,
                    lines: source.markdown.split("\n").length,
                    chars: source.markdown.length,
                  })}
                </p>
              ) : (
                <p className="rounded-md border border-warn/40 bg-warn/5 px-3 py-2 text-warn">
                  {t("short_drama_ai_storyboard_source_missing")}
                </p>
              )}

              {blocked ? (
                <div className="flex flex-wrap items-center gap-2 rounded-md border border-warn/40 bg-warn/5 px-3 py-2 text-warn">
                  <AlertTriangle aria-hidden className="size-4 shrink-0" />
                  <span>
                    {t("short_drama_storyboard_blocked_missing", {
                      count: missing.length,
                      assets: missing.map(missingLabel).join("、"),
                    })}
                  </span>
                  <Link
                    href={`/${ASSET_ROUTE[missing[0].type] ?? WORKSPACE_ROUTE_CHARACTERS}?focus=${encodeURIComponent(missing[0].name)}`}
                    className="focus-ring rounded-sm underline underline-offset-2 hover:text-foreground"
                  >
                    {t("short_drama_ai_storyboard_fix_missing")}
                  </Link>
                </div>
              ) : null}

              {hasStoryboards && sceneIds.length > 0 ? (
                <div className="flex flex-col gap-2">
                  <div className="flex items-center justify-between">
                    <span>{t("short_drama_ai_storyboard_scenes_label")}</span>
                    <label className="flex items-center gap-2 text-muted-foreground">
                      <Checkbox
                        checked={allSelected}
                        onCheckedChange={(value) => setSelected(value === true ? sceneIds : [])}
                        disabled={submitting}
                      />
                      <span>{t("short_drama_ai_storyboard_select_all")}</span>
                    </label>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {sceneIds.map((sceneId) => (
                      <label
                        key={sceneId}
                        className="flex items-center gap-1.5 rounded-sm border border-border px-2 py-1"
                      >
                        <Checkbox
                          checked={selected.includes(sceneId)}
                          onCheckedChange={(value) => toggleScene(sceneId, value === true)}
                          disabled={submitting}
                        />
                        <span>{sceneLabel(sceneId)}</span>
                      </label>
                    ))}
                  </div>
                  <p className="text-xs text-muted-foreground">
                    {allSelected
                      ? t("short_drama_ai_storyboard_overwrite", { count: storyboardCount })
                      : t("short_drama_ai_storyboard_scenes_hint")}
                  </p>
                </div>
              ) : hasStoryboards ? (
                <p className="text-warn">{t("short_drama_ai_storyboard_overwrite", { count: storyboardCount })}</p>
              ) : null}

              {!allSelected && (
                <label className="flex items-center gap-2">
                  <Checkbox
                    checked={forceRegenerate}
                    onCheckedChange={(value) => setForceRegenerate(value === true)}
                    disabled={submitting}
                  />
                  <span>{t("short_drama_ai_storyboard_force_regenerate")}</span>
                </label>
              )}

              <div className="flex flex-col gap-1">
                <span className="text-muted-foreground">{t("short_drama_ai_storyboard_strength")}</span>
                <div
                  className="flex flex-wrap gap-2"
                  role="group"
                  aria-label={t("short_drama_ai_storyboard_strength")}
                >
                  {[1, 2, 3].map((level) => (
                    <Button
                      key={level}
                      type="button"
                      size="sm"
                      variant={planningStrength === level ? "default" : "outline"}
                      disabled={submitting}
                      aria-pressed={planningStrength === level}
                      onClick={() => setPlanningStrength(level)}
                    >
                      {t(`short_drama_ai_storyboard_strength_${level}`)}
                    </Button>
                  ))}
                </div>
                <p className="text-xs text-muted-foreground">
                  {t("short_drama_ai_storyboard_strength_hint")}
                </p>
              </div>

              {result ? (
                <div className="flex flex-col gap-2 border-t border-border pt-3">
                  <p className="flex items-center gap-2 text-good">
                    <Check aria-hidden className="size-4" />
                    {result.partial
                      ? t("short_drama_ai_storyboard_partial_result", { scenes: result.scenes })
                      : t("short_drama_ai_storyboard_result", { scenes: result.scenes })}
                    {result.cached ? ` · ${t("short_drama_ai_storyboard_cache_hit")}` : ""}
                  </p>
                  {result.missing.length > 0 ? (
                    <div className="flex flex-wrap items-center gap-2 rounded-md border border-warn/40 bg-warn/5 px-3 py-2 text-warn">
                      <AlertTriangle aria-hidden className="size-4 shrink-0" />
                      <span>
                        {t("short_drama_ai_storyboard_missing", {
                          count: result.missing.length,
                          assets: result.missing.map(missingLabel).join("、"),
                        })}
                      </span>
                      <Link
                        href={`/${ASSET_ROUTE[result.missing[0].type] ?? WORKSPACE_ROUTE_CHARACTERS}?focus=${encodeURIComponent(result.missing[0].name)}`}
                        className="focus-ring rounded-sm underline underline-offset-2 hover:text-foreground"
                      >
                        {t("short_drama_ai_storyboard_fix_missing")}
                      </Link>
                    </div>
                  ) : null}
                </div>
              ) : null}

              {error ? <p className="text-destructive">{error}</p> : null}
            </div>
          </DialogBody>
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)} disabled={submitting}>
              {result ? t("short_drama_ai_storyboard_close") : t("common:cancel")}
            </Button>
            <Button onClick={() => void submit()} disabled={!canSubmit}>
              {submitting ? (
                <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
              ) : (
                <Sparkles aria-hidden data-icon="inline-start" />
              )}
              {t(
                hasStoryboards
                  ? "short_drama_ai_restoryboard_submit"
                  : "short_drama_ai_storyboard_submit",
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
