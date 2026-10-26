import { useCallback, useEffect, useId, useRef, useState, type CSSProperties } from "react";
import { useTranslation } from "react-i18next";
import { ArrowRight, Check, Loader2, Settings2, Sparkles } from "lucide-react";
import { API, type FileUrlWidth } from "@/api";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import type { ShotGridLayout, ShotGridPlan } from "@/types/shot-grid";
import { useProjectsStore } from "@/stores/projects-store";
import { isResourceBusy, useTasksStore } from "@/stores/tasks-store";
import { useShotGridDialogStore } from "@/stores/shot-grid-dialog-store";
import { usePromptAuthoringStore } from "@/stores/prompt-authoring-store";
import { ShotGridDialogHost } from "./ShotGridDialog";
import { enqueueShotGrid } from "@/actions/generation";
import { refreshAfterWrite } from "@/components/canvas/shared/refreshAfterWrite";
import { errMsg } from "@/utils/async";
import type { ReferenceNameSets } from "@/utils/reference-mentions";

interface ReferenceMaterialsSectionProps {
  projectName: string;
  scriptFile?: string;
  episode?: number;
  shotId: string;
  aspectRatio?: "9:16" | "16:9";
  readOnly?: boolean;
  disabled?: boolean;
  videoPromptFilled?: boolean;
  saveFirst?: boolean;
  /** 本分镜的出场资产清单：参考素材逐格提示词的 @ 候选从这里取。 */
  referenceNames?: ReferenceNameSets;
  beforeGenerate: () => Promise<boolean>;
  onPlanChange: (plan: ShotGridPlan | null) => void;
  onBusyChange: (busy: boolean) => void;
}

const GRID_COLUMNS: Record<number, string> = { 2: "grid-cols-2", 3: "grid-cols-3" };
const LAYOUTS: Record<ShotGridLayout, [rows: number, cols: number]> = {
  "2x2": [2, 2],
  "2x3": [2, 3],
  "3x3": [3, 3],
};
const TERMINAL_TASK_STATUSES = new Set(["completed", "succeeded", "failed", "cancelled"]);

export function ReferenceMaterialsSection({
  projectName, scriptFile, episode, shotId, aspectRatio = "9:16", readOnly, disabled,
  videoPromptFilled, saveFirst, beforeGenerate, onPlanChange, onBusyChange,
}: ReferenceMaterialsSectionProps) {
  const { t } = useTranslation("dashboard");
  const titleId = useId();
  const fingerprints = useProjectsStore((state) => state.assetFingerprints);
  const revision = useProjectsStore((state) => state.projectSnapshotRevisions[projectName] ?? 0);
  useTasksStore((state) => state.tasks);
  useTasksStore((state) => state.optimisticActive);
  const scopeKey = `${projectName}:${scriptFile}:${shotId}`;
  const [reloadKey, setReloadKey] = useState(0);
  const [result, setResult] = useState<{ key: string; plan: ShotGridPlan | null; error: string | null }>({
    key: "", plan: null, error: null,
  });
  const [submitting, setSubmitting] = useState(false);
  const [needsRefresh, setNeedsRefresh] = useState(false);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const operation = useRef<AbortController | null>(null);
  const loading = useRef<AbortController | null>(null);
  const plan = result.key === scopeKey ? result.plan : null;
  const error = result.key === scopeKey ? result.error : null;
  const resourceBusy = isResourceBusy("shot_grid", projectName, plan?.id ?? shotId);
  const busy = submitting || resourceBusy || plan?.status === "generating";

  useEffect(() => () => operation.current?.abort(), [scopeKey]);

  const updatePlan = useCallback((next: ShotGridPlan | null) => {
    setResult({ key: scopeKey, plan: next, error: null });
    onPlanChange(next);
  }, [scopeKey, onPlanChange]);

  useEffect(() => useShotGridDialogStore.subscribe((state, previous) => {
    if (previous.request && !state.request) setReloadKey((value) => value + 1);
  }), []);

  useEffect(() => {
    if (!scriptFile) return;
    const controller = new AbortController();
    loading.current?.abort();
    loading.current = controller;
    let timer: number | undefined;
    const load = async () => {
      try {
        let next = await API.getShotGrid(projectName, scriptFile, shotId, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (next?.status === "generating" && next.task_id) {
          const task = await API.getShotGridTask(projectName, next.task_id, { signal: controller.signal });
          if (controller.signal.aborted) return;
          if (TERMINAL_TASK_STATUSES.has(task.status)) {
            next = await API.getShotGrid(projectName, scriptFile, shotId, { signal: controller.signal });
            if (controller.signal.aborted) return;
            if (next?.status === "generating" && ["failed", "cancelled"].includes(task.status)) {
              next = { ...next, status: "failed", error_message: task.error_message || t("shot_materials_failed") };
            }
          }
        }
        updatePlan(next);
        if (next?.status === "generating") timer = window.setTimeout(() => void load(), 1500);
      } catch (cause) {
        if (controller.signal.aborted) return;
        setResult((previous) => ({
          key: scopeKey, plan: previous.key === scopeKey ? previous.plan : null, error: errMsg(cause),
        }));
        timer = window.setTimeout(() => void load(), 3000);
      }
    };
    void load();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [projectName, scriptFile, shotId, scopeKey, revision, reloadKey, updatePlan, t]);

  useEffect(() => {
    onBusyChange(busy);
    return () => onBusyChange(false);
  }, [busy, onBusyChange]);

  const generate = async (layout?: ShotGridLayout) => {
    if (!scriptFile || episode === undefined || readOnly || disabled || busy) return;
    const controller = new AbortController();
    operation.current?.abort();
    operation.current = controller;
    setSubmitting(true);
    try {
      if (saveFirst && !(await beforeGenerate())) return;
      if (controller.signal.aborted) return;
      if (isResourceBusy("shot_grid", projectName, plan?.id ?? shotId)) return;
      if (
        plan?.status === "confirmed"
        && !window.confirm(
          t("shot_materials_regenerate_guard", {
            defaultValue: "重新生成会作废已确认的 K 帧，视频提示词也要重写。继续？",
          }),
        )
      ) {
        return;
      }
      // 直接出图：不弹配置窗。prompt 传空串，服务端按 image_prompt 现解析逐格文本，
      // 宫格与分镜词从此只有一份真相，不再另写一份“参考图提示词”。
      const [rows, cols] = layout ? LAYOUTS[layout] : [plan?.rows ?? 2, plan?.cols ?? 2];
      const saved = await API.saveShotGrid(projectName, {
        script_file: scriptFile,
        episode,
        shot_id: shotId,
        rows,
        cols,
        prompt: "",
      });
      if (controller.signal.aborted) return;
      updatePlan(saved);
      await enqueueShotGrid(projectName, scriptFile, shotId, saved.id);
      if (controller.signal.aborted) return;
      setReloadKey((value) => value + 1);
    } catch (cause) {
      if (!controller.signal.aborted) setResult({ key: scopeKey, plan, error: errMsg(cause) });
    } finally {
      if (!controller.signal.aborted) setSubmitting(false);
    }
  };

  const configure = () => generate();

  const changeConfirmation = async (confirmed: boolean) => {
    if (!plan || readOnly || disabled || busy || isResourceBusy("shot_grid", projectName, plan.id)) return;
    const controller = new AbortController();
    operation.current?.abort();
    operation.current = controller;
    setSubmitting(true);
    try {
      const response = confirmed
        ? await API.confirmShotGrid(projectName, plan.id)
        : await API.unconfirmShotGrid(projectName, plan.id);
      if (controller.signal.aborted) return;
      loading.current?.abort();
      updatePlan(response.grid);
      setReloadKey((value) => value + 1);
      setNeedsRefresh(true);
      if (await refreshAfterWrite(projectName, t) === "success" && !controller.signal.aborted) {
        setNeedsRefresh(false);
      }
    } catch (cause) {
      if (!controller.signal.aborted) setResult({ key: scopeKey, plan, error: errMsg(cause) });
    } finally {
      if (!controller.signal.aborted) setSubmitting(false);
    }
  };

  const openVideoPrompt = () => {
    // stale 只说明输入相对出图时变了；已确认的 K 帧仍是可用素材，允许继续写视频提示词。
    if (episode === undefined || readOnly || disabled || busy || needsRefresh || plan?.status !== "confirmed") return;
    usePromptAuthoringStore.getState().open({
      projectName, episode, scope: "current", currentEntryId: shotId, defaultFields: ["video_prompt"], lockFields: true,
    });
  };
  const retryRefresh = async () => {
    if (submitting) return;
    setSubmitting(true);
    try {
      if (await refreshAfterWrite(projectName, t) === "success") {
        setNeedsRefresh(false);
        setReloadKey((value) => value + 1);
      }
    } finally {
      setSubmitting(false);
    }
  };
  const imageUrl = (path: string, width: FileUrlWidth = 1280) =>
    API.getFileUrl(projectName, path, fingerprints[path] ?? null, { width });
  const cellRatio = (plan?.video_aspect_ratio ?? aspectRatio).replace(":", " / ");
  const columns = GRID_COLUMNS[plan?.cols ?? 2] ?? "grid-cols-2";
  const cellWidth = 200;
  const cellHeight = Math.round(cellWidth * (9 / 16));
  const gridWidth = (plan?.cols ?? 2) * cellWidth;
  const gridHeight = (plan?.rows ?? 2) * cellHeight;
  const describeCell = (cell: ShotGridPlan["cells"][number]) =>
    cell.prompt.trim() || cell.continuity_notes.trim();
  const showCells = plan?.status === "confirmed" && plan.cells.some((cell) => cell.image_path);
  const statusLabel = plan?.stale
    ? plan?.status === "confirmed" ? t("shot_materials_stale_confirmed") : t("shot_materials_stale")
    : plan?.status === "confirmed" ? t("shot_materials_confirmed")
      : plan?.status === "completed" ? t("shot_materials_ready")
        : plan?.status === "generating" ? t("shot_grid_generating")
          : plan?.status === "failed" ? t("shot_materials_failed") : t("shot_materials_not_generated");

  return (
    <section aria-labelledby={titleId} className="flex min-w-0 flex-col gap-2">
      <div className="flex min-h-7 flex-wrap items-center gap-2">
        <h3 id={titleId} className="min-w-0 flex-1 text-xs font-medium text-muted-foreground">{t("shot_reference_materials")}</h3>
        {!readOnly && (
          <>
            <Button type="button" variant="outline" size="xs"
              disabled={!scriptFile || episode === undefined || disabled || busy}
              onClick={() => void configure()}>
              {busy ? <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" /> : <Sparkles aria-hidden data-icon="inline-start" />}
              {t("shot_configure_materials")}
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger
                render={
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-xs"
                    aria-label={t("shot_configure_advanced")}
                    title={t("shot_configure_advanced")}
                    disabled={!scriptFile || episode === undefined || disabled || busy}
                  />
                }
              >
                <Settings2 aria-hidden />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {(Object.keys(LAYOUTS) as ShotGridLayout[]).map((item) => (
                  <DropdownMenuItem key={item} disabled={busy} onClick={() => void generate(item)}>
                    {`${plan?.rows ?? 2}x${plan?.cols ?? 2}` === item ? (
                      <Check aria-hidden />
                    ) : (
                      <span className="size-3.5" aria-hidden />
                    )}
                    {item.replace("x", "×")}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          </>
        )}
      </div>
      <p role="status" className={plan?.stale ? "text-xs text-warn" : "text-xs text-muted-foreground"}>{statusLabel}</p>
      <p className="text-xs text-muted-foreground">{t("shot_materials_hint")}</p>
      {error || plan?.error_message ? <p role="alert" className="text-xs text-destructive">{error || plan?.error_message}</p> : null}
      {needsRefresh && (
        <Button type="button" variant="outline" size="xs" disabled={submitting}
          onClick={() => void retryRefresh()}>{t("common:refresh")}</Button>
      )}
      {!readOnly && plan?.status === "completed" && (
        <Button type="button" size="xs" disabled={disabled || busy || plan.stale}
          onClick={() => void changeConfirmation(true)}>
          {submitting ? <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" /> : <Check aria-hidden data-icon="inline-start" />}
          {t("shot_confirm_materials")}
        </Button>
      )}
      {!readOnly && plan?.status === "confirmed" && (
        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" variant="outline" size="xs" disabled={disabled || busy}
            onClick={() => void changeConfirmation(false)}>{t("shot_unconfirm_materials")}</Button>
          <Button type="button" size="xs" disabled={disabled || busy || needsRefresh || episode === undefined}
            onClick={openVideoPrompt}>
            <Sparkles aria-hidden data-icon="inline-start" />
            {videoPromptFilled ? t("shot_video_prompt_rewrite") : t("shot_video_prompt_generate")}
          </Button>
        </div>
      )}
      {showCells && plan ? (
        aspectRatio === "9:16" ? (
          <div className="flex max-w-full items-stretch gap-2 overflow-x-auto pb-1">
            {plan.cells.map((cell, index) => (
              <div key={cell.index} className="flex shrink-0 items-center gap-2">
                <button
                  type="button"
                  aria-label={t("shot_keyframe", { index: cell.index })}
                  onClick={() => { if (cell.image_path) setPreviewUrl(imageUrl(cell.image_path)); }}
                  className="focus-ring relative w-[140px] overflow-hidden rounded-lg border border-border bg-muted/40 p-1.5 text-left shadow-sm"
                >
                  <div className="overflow-hidden rounded-md bg-background" style={{ aspectRatio: cellRatio }}>
                    {cell.image_path ? (
                      <img src={imageUrl(cell.image_path, 640)} alt={t("shot_keyframe", { index: cell.index })}
                        className="size-full object-contain" />
                    ) : null}
                    <span className="absolute left-1.5 top-1.5 rounded bg-black/65 px-1.5 py-0.5 text-[10px] font-medium text-white">K{cell.index}</span>
                  </div>
                  <div className="px-2 py-1.5">
                    <p className="line-clamp-2 text-[11px] leading-4 text-muted-foreground">{describeCell(cell)}</p>
                  </div>
                </button>
                {index < plan.cells.length - 1 ? <ArrowRight aria-hidden className="shrink-0 text-muted-foreground" /> : null}
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
                type="button"
                aria-label={t("shot_keyframe", { index: cell.index })}
                onClick={() => { if (cell.image_path) setPreviewUrl(imageUrl(cell.image_path)); }}
                className="focus-ring min-w-0 overflow-hidden rounded-md border border-border bg-muted/40 text-left"
              >
                <div className="relative overflow-hidden bg-background" style={{ aspectRatio: cellRatio }}>
                  {cell.image_path ? (
                    <img src={imageUrl(cell.image_path, 640)} alt={t("shot_keyframe", { index: cell.index })}
                      className="size-full object-contain" />
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
      ) : plan?.composite_path ? (
        <button type="button" aria-label={t("shot_materials_preview_open")}
          onClick={() => { if (plan.composite_path) setPreviewUrl(imageUrl(plan.composite_path)); }}
          className="focus-ring relative block overflow-hidden rounded-md border border-border bg-muted/20"
          style={{ width: gridWidth, height: gridHeight, maxWidth: "100%", maxHeight: "100%" }}>
          <img src={imageUrl(plan.composite_path)} alt={t("shot_materials_preview")} className="size-full object-contain" />
        </button>
      ) : null}
      <Dialog open={previewUrl !== null} onOpenChange={(open) => { if (!open) setPreviewUrl(null); }}>
        <DialogContent size="viewer">
          <DialogHeader><DialogTitle>{t("shot_materials_preview")}</DialogTitle></DialogHeader>
          <DialogBody className="flex min-h-0 items-center justify-center">
            {previewUrl ? <img src={previewUrl} alt={t("shot_materials_preview")} className="size-full object-contain" /> : null}
          </DialogBody>
        </DialogContent>
      </Dialog>
      <ShotGridDialogHost />
    </section>
  );
}
