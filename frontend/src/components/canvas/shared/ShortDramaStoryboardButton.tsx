import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "wouter";
import { AlertTriangle, Check, Loader2, Sparkles } from "lucide-react";
import { reimportShortDramaSource } from "@/actions/shortDrama";
import { promptAuthoringResourceId } from "@/actions/generation";
import { useFactoryEpisodeSource } from "@/components/canvas/episode-page/useFactoryEpisodeSource";
import { isResourceBusy, useActiveResourceIds } from "@/stores/tasks-store";
import { useDemoWorkbench } from "@/onboarding/use-demo-workbench";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
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
  readOnly?: boolean;
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
export function ShortDramaStoryboardButton(props: Props) {
  return <ShortDramaStoryboardAction key={`${props.projectName}:${props.episode}`} {...props} />;
}

function ShortDramaStoryboardAction({
  projectName,
  episode,
  storyboardCount,
  sceneIds,
  prominent = false,
  readOnly = false,
}: Props) {
  const { t } = useTranslation("dashboard");
  const demo = useDemoWorkbench();
  const readonly = readOnly || demo;
  const sourceState = useFactoryEpisodeSource(projectName, episode, !readonly);
  const source = sourceState.value;
  const loading = sourceState.loading;
  const busy = useActiveResourceIds("text_episode_script", projectName).has(promptAuthoringResourceId(episode));
  const controllerRef = useRef<AbortController | null>(null);
  const [open, setOpen] = useState(false);
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
  const [pendingResult, setPendingResult] = useState<typeof result>(null);

  const hasStoryboards = storyboardCount > 0;
  const missing = source?.assets_missing_description ?? [];
  const blocked = missing.length > 0;

  useEffect(() => () => controllerRef.current?.abort(), []);

  const openDialog = () => {
    if (readonly || blocked || submitting || isResourceBusy("text_episode_script", projectName, promptAuthoringResourceId(episode))) return;
    setResult(null);
    setError(null);
    setSelected(sceneIds);
    setOpen(true);
    sourceState.reload();
  };

  const toggleScene = (sceneId: string, checked: boolean) => {
    setSelected((current) =>
      checked ? [...new Set([...current, sceneId])] : current.filter((id) => id !== sceneId),
    );
  };

  const selectedIds = selected.filter((id) => sceneIds.includes(id));
  const allSelected = hasStoryboards && sceneIds.length > 0 && selectedIds.length === sceneIds.length;
  /** 部分=勾选了既有分镜的真子集：按分镜重写，其余分镜原样保留。 */
  const partial = hasStoryboards && sceneIds.length > 0 && !allSelected;

  const submit = async () => {
    if (readonly || blocked || loading || submitting || pendingResult) return;
    if (isResourceBusy("text_episode_script", projectName, promptAuthoringResourceId(episode))) {
      setError(t("prompt_authoring_busy"));
      return;
    }
    if (hasStoryboards && sceneIds.length > 0 && selectedIds.length === 0) return;
    const revision = source?.revision;
    if (!revision) {
      setError(t("short_drama_ai_storyboard_source_missing"));
      return;
    }
    setSubmitting(true);
    setError(null);
    const controller = new AbortController();
    controllerRef.current = controller;
    try {
      const res = await reimportShortDramaSource(
        projectName,
        episode,
        revision,
        hasStoryboards,
        planningStrength,
        forceRegenerate,
        partial ? selectedIds : undefined,
        { signal: controller.signal },
      );
      controller.signal.throwIfAborted();
      const resMissing = res.assets_without_description ?? [];
      const completed = {
        scenes: partial ? res.regenerated_scenes ?? res.scenes : res.scenes,
        partial,
        missing: resMissing,
        cached: res.planning_cached,
      };
      setPendingResult(completed);
      const refreshed = await refreshAfterWrite(projectName, t);
      controller.signal.throwIfAborted();
      if (refreshed !== "success") return;
      setPendingResult(null);
      setResult(completed);
      if (resMissing.length > 0) {
        useAppStore.getState().pushToast(
          t("short_drama_ai_storyboard_missing", {
            count: resMissing.length,
            assets: resMissing.map((asset) => asset.name).join("、"),
          }),
          "warning",
        );
      }
    } catch (err) {
      if (!controller.signal.aborted) setError(errMsg(err));
    } finally {
      if (!controller.signal.aborted) setSubmitting(false);
    }
  };

  const retryRefresh = async () => {
    if (!pendingResult || submitting) return;
    setSubmitting(true);
    const controller = new AbortController();
    controllerRef.current = controller;
    try {
      const refreshed = await refreshAfterWrite(projectName, t);
      if (!controller.signal.aborted && refreshed === "success") {
        setResult(pendingResult);
        setPendingResult(null);
      }
    } finally {
      if (!controller.signal.aborted) setSubmitting(false);
    }
  };

  const missingLabel = (asset: MissingAsset) => {
    const type = t(`short_drama_asset_${asset.type}`);
    return type === `short_drama_asset_${asset.type}` ? asset.name : `${asset.name}（${type}）`;
  };

  const canSubmit =
    !readonly && !blocked && !busy && !loading && !submitting && !pendingResult
    && Boolean(source?.exists && source.revision) && (!hasStoryboards || selectedIds.length > 0);

  const trigger = (
    <Button
      variant={prominent ? "default" : "outline"}
      size={prominent ? "default" : "sm"}
      onClick={openDialog}
      disabled={readonly || blocked || busy || submitting}
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

              <label className="flex items-center gap-2">
                <Checkbox
                  checked={forceRegenerate}
                  onCheckedChange={(value) => setForceRegenerate(value === true)}
                  disabled={submitting || readonly}
                />
                <span>{t("short_drama_ai_storyboard_force_regenerate")}</span>
              </label>

              <div className="flex flex-col gap-1">
                <span className="text-muted-foreground">{t("short_drama_ai_storyboard_strength")}</span>
                <ToggleGroup
                  value={[String(planningStrength)]}
                  onValueChange={(values) => {
                    if (values[0]) setPlanningStrength(Number(values[0]));
                  }}
                  variant="outline"
                  size="sm"
                  disabled={submitting || readonly}
                  aria-label={t("short_drama_ai_storyboard_strength")}
                >
                  {[1, 2, 3].map((level) => (
                    <ToggleGroupItem
                      key={level}
                      value={String(level)}
                    >
                      {t(`short_drama_ai_storyboard_strength_${level}`)}
                    </ToggleGroupItem>
                  ))}
                </ToggleGroup>
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

              {error || sourceState.error ? <p role="alert" className="text-destructive">{error ?? sourceState.error}</p> : null}
              {sourceState.error ? <Button variant="outline" onClick={sourceState.reload}>{t("common:retry")}</Button> : null}
              {pendingResult ? (
                <Button variant="outline" onClick={() => void retryRefresh()} disabled={submitting}>
                  {t("common:retry")}
                </Button>
              ) : null}
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
