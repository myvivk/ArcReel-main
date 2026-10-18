import { useCallback, useState } from "react";
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
  /** 页头动作用描边小按钮；空态里的主操作用实心按钮。 */
  prominent?: boolean;
}

const ASSET_ROUTE: Record<string, string> = {
  character: WORKSPACE_ROUTE_CHARACTERS,
  scene: WORKSPACE_ROUTE_SCENES,
  prop: WORKSPACE_ROUTE_PROPS,
};

/**
 * 分镜页的「AI 分镜 / AI 重新分镜」入口：读本集已保存的 Markdown 源稿重建分镜。
 * 只做内容切分与资产登记，不写提示词、不生成图片或视频。
 */
export function ShortDramaStoryboardButton({
  projectName,
  episode,
  storyboardCount,
  prominent = false,
}: Props) {
  const { t } = useTranslation("dashboard");
  const [open, setOpen] = useState(false);
  const [source, setSource] = useState<ShortDramaSource | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ scenes: number; missing: MissingAsset[] } | null>(null);

  const hasStoryboards = storyboardCount > 0;

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

  const openDialog = () => {
    setConfirmed(false);
    setResult(null);
    setError(null);
    setOpen(true);
    void load();
  };

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
      const res = await reimportShortDramaSource(projectName, episode, revision, hasStoryboards);
      // 先落结果再刷新：刷新带来的重渲染不该把「已生成」和缺描述黄条抹掉。
      setResult({ scenes: res.scenes, missing: res.assets_without_description ?? [] });
      if (res.assets_without_description?.length) {
        useAppStore.getState().pushToast(
          t("short_drama_ai_storyboard_missing", {
            count: res.assets_without_description.length,
            assets: res.assets_without_description.map((asset) => asset.name).join("、"),
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
    !loading && !submitting && Boolean(source?.revision) && (!hasStoryboards || confirmed);

  return (
    <>
      <Button
        variant={prominent ? "default" : "outline"}
        size={prominent ? "default" : "sm"}
        onClick={openDialog}
      >
        <Sparkles aria-hidden data-icon="inline-start" />
        {t(hasStoryboards ? "short_drama_ai_restoryboard" : "short_drama_ai_storyboard")}
      </Button>
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

              {hasStoryboards ? (
                <div className="rounded-md border border-warn/40 bg-warn/5 px-3 py-2 text-warn">
                  <p className="flex items-start gap-2">
                    <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
                    <span>{t("short_drama_ai_storyboard_overwrite", { count: storyboardCount })}</span>
                  </p>
                  <label className="mt-2 flex items-center gap-2 text-foreground">
                    <Checkbox
                      checked={confirmed}
                      onCheckedChange={(value) => setConfirmed(value === true)}
                      disabled={submitting}
                    />
                    <span>{t("short_drama_ai_storyboard_overwrite_confirm")}</span>
                  </label>
                </div>
              ) : null}

              {result ? (
                <div className="flex flex-col gap-2 border-t border-border pt-3">
                  <p className="flex items-center gap-2 text-good">
                    <Check aria-hidden className="size-4" />
                    {t("short_drama_ai_storyboard_result", { scenes: result.scenes })}
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
