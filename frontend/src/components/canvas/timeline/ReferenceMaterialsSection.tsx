import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { ArrowRight, Check, Loader2, Sparkles } from "lucide-react";
import { API } from "@/api";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import type { ShotGridPlan } from "@/types/shot-grid";
import { useAppStore } from "@/stores/app-store";
import { useProjectsStore } from "@/stores/projects-store";
import { useShotGridDialogStore } from "@/stores/shot-grid-dialog-store";
import { usePromptAuthoringStore } from "@/stores/prompt-authoring-store";
import { ShotGridDialogHost } from "./ShotGridDialog";
import { refreshAfterWrite } from "@/components/canvas/shared/refreshAfterWrite";

interface ReferenceMaterialsSectionProps {
  projectName: string;
  scriptFile?: string;
  episode?: number;
  shotId: string;
  aspectRatio?: "9:16" | "16:9";
  readOnly?: boolean;
  disabled?: boolean;
  /** 该分镜已有视频提示词：收尾条的入口写「重写」而不是「生成」。 */
  videoPromptFilled?: boolean;
  saveFirst?: boolean;
  beforeGenerate: () => Promise<boolean>;
  onPlanChange: (plan: ShotGridPlan | null) => void;
  onBusyChange: (busy: boolean) => void;
}

/** 格数 → 静态栅格类：Tailwind 只能识别完整的类名，动态拼接的那一个不会被编译进去。 */
const GRID_COLUMNS: Record<number, string> = { 2: "grid-cols-2", 3: "grid-cols-3" };

export function ReferenceMaterialsSection({
  projectName,
  scriptFile,
  episode,
  shotId,
  aspectRatio = "9:16",
  readOnly,
  disabled,
  videoPromptFilled,
  saveFirst,
  beforeGenerate,
  onPlanChange,
  onBusyChange,
}: ReferenceMaterialsSectionProps) {
  const { t } = useTranslation("dashboard");
  const fingerprints = useProjectsStore((state) => state.assetFingerprints);
  const titleId = useId();
  const openDialog = useShotGridDialogStore((s) => s.open);
  const dialogOpen = useShotGridDialogStore((s) => s.request) !== null;
  const [plan, setPlan] = useState<ShotGridPlan | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // 确认（切分落格 + 登记分镜图）是独立一步：生成完成后必须显式确认，前端才会展示
  // 切分后的逐格与描述，分镜列表也才拿得到 storyboard_image。
  const [confirming, setConfirming] = useState(false);
  // 宫格图的放大预览：缩略图按可用宽度等比缩放，点击进查看器看原图。
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  // 弹窗关掉就重读：确定入队了这一条要接着轮询它的任务，取消则回到原样。
  const [reloadKey, setReloadKey] = useState(0);
  const wasOpen = useRef(dialogOpen);
  useEffect(() => {
    if (wasOpen.current && !dialogOpen) setReloadKey((key) => key + 1);
    wasOpen.current = dialogOpen;
  }, [dialogOpen]);
  const busy = submitting || confirming || plan?.status === "generating";

  const updatePlan = useCallback(
    (next: ShotGridPlan | null) => {
      setPlan(next);
      onPlanChange(next);
    },
    [onPlanChange],
  );

  useEffect(() => {
    if (!scriptFile) return;
    const controller = new AbortController();
    API.getShotGrid(projectName, scriptFile, shotId, { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) updatePlan(value);
      })
      .catch(() => {});
    return () => controller.abort();
  }, [projectName, scriptFile, shotId, updatePlan, reloadKey]);

  useEffect(() => {
    if (!plan?.task_id || plan.status !== "generating") return;
    let stopped = false;
    const poll = async () => {
      try {
        const task = await API.getShotGridTask(projectName, plan.task_id!);
        if (stopped) return;
        if (["completed", "succeeded", "failed"].includes(task.status)) {
          const refreshed = await API.getShotGrid(projectName, plan.script_file, plan.shot_id);
          if (!stopped) updatePlan(refreshed);
          return;
        }
      } catch {
        // Keep tracking through transient request failures.
      }
      if (!stopped) window.setTimeout(() => void poll(), 1500);
    };
    const timer = window.setTimeout(() => void poll(), 1000);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [plan, projectName, updatePlan]);

  useEffect(() => {
    onBusyChange(busy);
    return () => onBusyChange(false);
  }, [busy, onBusyChange]);

  /** 打开配置弹窗：先保存分镜上的未提交修改，scene 是逐格文本的来源。 */
  const configure = async () => {
    if (!scriptFile || episode === undefined || disabled || busy || submitting) return;
    if (saveFirst && !(await beforeGenerate())) return;
    openDialog({ projectName, scriptFile, episode, shotId, plan });
  };

  /** 确认参考素材：切分落格 + 登记 storyboard_image；之后才展示逐格与描述、列表才有缩略图。 */
  const confirm = async () => {
    if (!plan?.id || confirming) return;
    setConfirming(true);
    try {
      const result = await API.confirmShotGrid(projectName, plan.id);
      updatePlan(result.grid);
      // 确认把整图登记为 generated_assets.storyboard_image：刷新项目让分镜列表拿到缩略图。
      await refreshAfterWrite(projectName, t);
    } catch {
      // 失败保留现状，用户可重试
    } finally {
      setConfirming(false);
    }
  };

  /** 宫格完成后进入视频提示词：单分镜、字段锁定为 video_prompt。 */
  const openVideoPrompt = () => {
    if (episode === undefined) return;
    usePromptAuthoringStore.getState().open({
      projectName,
      episode,
      scope: "current",
      currentEntryId: shotId,
      defaultFields: ["video_prompt"],
      lockFields: true,
    });
  };

  const compositeUrl = plan?.composite_path
    ? API.getFileUrl(projectName, plan.composite_path, fingerprints[plan.composite_path] ?? null, { width: 1280 })
    : null;
  const columns = GRID_COLUMNS[plan?.cols ?? 2] ?? "grid-cols-2";
  const cellWidth = 200;
  const cellHeight = Math.round(cellWidth * (9 / 16));
  const gridWidth = (plan?.cols ?? 2) * cellWidth;
  const gridHeight = (plan?.rows ?? 2) * cellHeight;
  const describeCell = (cell: ShotGridPlan["cells"][number]) => cell.prompt.trim() || cell.continuity_notes.trim();
  const shotLabel = (index: number) => {
    if (index === 0) return "开场";
    if (index === (plan?.cells.length ?? 1) - 1) return "收尾";
    return "过渡";
  };

  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-2">
      <div className="flex min-h-7 flex-wrap items-center gap-2">
        <h3 id={titleId} className="min-w-0 flex-1 text-xs font-medium text-muted-foreground">{t("shot_reference_materials")}</h3>
        {!readOnly && (
          <Button
            type="button"
            variant="outline"
            size="xs"
            disabled={!scriptFile || episode === undefined || disabled || busy || submitting}
            onClick={() => void configure()}
          >
            {(busy || submitting) ? <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" /> : <Sparkles aria-hidden data-icon="inline-start" />}
            {t("shot_configure_materials")}
          </Button>
        )}
        {(plan?.status === "completed" || plan?.status === "confirmed") && (
          <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-muted/40 px-2 py-1.5">
            <Check aria-hidden className="size-3.5 shrink-0 text-primary" />
            <span className="min-w-0 flex-1 text-xs text-muted-foreground">{t("shot_materials_ready")}</span>
            <span className="text-xs text-muted-foreground">
              {videoPromptFilled ? t("video_prompt_done") : t("video_prompt_missing")}
            </span>
            {plan?.status === "completed" ? (
              <Button
                type="button"
                size="xs"
                disabled={disabled || confirming}
                onClick={() => void confirm()}
              >
                {confirming ? (
                  <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
                ) : (
                  <Check aria-hidden data-icon="inline-start" />
                )}
                {t("shot_confirm_materials")}
              </Button>
            ) : null}
            <Button
              type="button"
              size="xs"
              variant="secondary"
              disabled={disabled || episode === undefined}
              onClick={openVideoPrompt}
            >
              <Sparkles aria-hidden data-icon="inline-start" />
              {videoPromptFilled ? t("shot_video_prompt_rewrite") : t("shot_video_prompt_generate")}
            </Button>
          </div>
        )}
      </div>
      <p className="text-xs text-muted-foreground">{t("shot_materials_hint")}</p>
      {plan?.error_message ? <p role="alert" className="text-xs text-destructive">{plan.error_message}</p> : null}
      {plan?.cells.some((cell) => cell.image_path) ? (
        aspectRatio === "9:16" ? (
          <div className="flex max-w-full items-stretch gap-2 overflow-x-auto pb-1">
            {plan.cells.map((cell, index) => (
              <div key={cell.index} className="flex shrink-0 items-center gap-2">
                <button
                  type="button"
                  aria-label={t("shot_keyframe", { index: cell.index })}
                  onClick={() => {
                    if (cell.image_path) {
                      setPreviewUrl(API.getFileUrl(projectName, cell.image_path, fingerprints[cell.image_path] ?? null, { width: 1280 }));
                    }
                  }}
                  title={describeCell(cell)}
                  className="focus-ring relative w-[140px] overflow-hidden rounded-lg border border-border bg-muted/40 p-1.5 text-left shadow-sm"
                >
                  <div className="overflow-hidden rounded-md bg-background" style={{ aspectRatio: "9 / 16" }}>
                    {cell.image_path ? (
                      <img
                        src={API.getFileUrl(projectName, cell.image_path, fingerprints[cell.image_path] ?? null, { width: 640 })}
                        alt={t("shot_keyframe", { index: cell.index })}
                        className="size-full object-contain"
                      />
                    ) : null}
                  </div>
                  <div className="px-1 pb-1 pt-1.5">
                    <div className="flex items-center justify-between gap-1">
                      <span className="text-xs font-semibold text-foreground">K{cell.index}</span>
                      <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">{shotLabel(index)}</span>
                    </div>
                    <p className="mt-1 line-clamp-2 text-[11px] leading-4 text-muted-foreground">{describeCell(cell)}</p>
                  </div>
                </button>
                {index < plan.cells.length - 1 ? <ArrowRight aria-hidden className="size-4 shrink-0 text-muted-foreground" /> : null}
              </div>
            ))}
          </div>
        ) : (
          <div
            className={`grid ${columns} gap-2 overflow-hidden rounded-md border border-border bg-muted/20 p-2`}
            style={{ width: gridWidth, maxWidth: "100%" }}
          >
            {plan.cells.map((cell) => (
              <button
                key={cell.index}
                type="button"
                aria-label={t("shot_keyframe", { index: cell.index })}
                onClick={() => {
                  if (cell.image_path) {
                    setPreviewUrl(API.getFileUrl(projectName, cell.image_path, fingerprints[cell.image_path] ?? null, { width: 1280 }));
                  }
                }}
                className="focus-ring min-w-0 overflow-hidden rounded-md border border-border bg-muted/40 text-left"
              >
                <div className="relative overflow-hidden bg-background" style={{ aspectRatio: `${cellWidth} / ${cellHeight}` }}>
                  {cell.image_path ? (
                    <img
                      src={API.getFileUrl(projectName, cell.image_path, fingerprints[cell.image_path] ?? null, { width: 640 })}
                      alt={t("shot_keyframe", { index: cell.index })}
                      className="size-full object-contain"
                    />
                  ) : null}
                  <span className="absolute left-1.5 top-1.5 rounded bg-black/65 px-1.5 py-0.5 text-[10px] font-medium text-white">K{cell.index}</span>
                </div>
                <div className="px-2 py-1.5">
                  <p className="line-clamp-2 text-[11px] leading-4 text-muted-foreground">{describeCell(cell)}</p>
                </div>
              </button>
            ))}
          </div>
        )
      ) : compositeUrl ? (
        <button
          type="button"
          aria-label={t("shot_materials_preview_open")}
          onClick={() => setPreviewUrl(compositeUrl)}
          className="focus-ring relative block overflow-hidden rounded-md border border-border bg-muted/20"
          style={{ width: gridWidth, height: gridHeight, maxWidth: "100%", maxHeight: "100%" }}
        >
          <img src={compositeUrl} alt={t("shot_materials_preview")} className="size-full object-contain" />
        </button>
      ) : null}
      <Dialog open={previewUrl !== null} onOpenChange={(open) => !open && setPreviewUrl(null)}>
        <DialogContent size="viewer">
          <DialogHeader>
            <DialogTitle>{t("shot_materials_preview")}</DialogTitle>
          </DialogHeader>
          <DialogBody className="flex min-h-0 items-center justify-center">
            {previewUrl ? <img src={previewUrl} alt={t("shot_materials_preview")} className="size-full object-contain" /> : null}
          </DialogBody>
        </DialogContent>
      </Dialog>
      <ShotGridDialogHost />
    </section>
  );
}
