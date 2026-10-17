import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Check, Loader2, Sparkles } from "lucide-react";
import { API } from "@/api";
import { Button } from "@/components/ui/button";
import type { ShotGridPlan } from "@/types/shot-grid";
import { useAppStore } from "@/stores/app-store";
import { useProjectsStore } from "@/stores/projects-store";
import { useShotGridDialogStore } from "@/stores/shot-grid-dialog-store";
import { ShotGridDialogHost } from "./ShotGridDialog";

interface ReferenceMaterialsSectionProps {
  projectName: string;
  scriptFile?: string;
  episode?: number;
  shotId: string;
  readOnly?: boolean;
  disabled?: boolean;
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
  readOnly,
  disabled,
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
  // 弹窗关掉就重读：确定入队了这一条要接着轮询它的任务，取消则回到原样。
  const [reloadKey, setReloadKey] = useState(0);
  const wasOpen = useRef(dialogOpen);
  useEffect(() => {
    if (wasOpen.current && !dialogOpen) setReloadKey((key) => key + 1);
    wasOpen.current = dialogOpen;
  }, [dialogOpen]);
  const busy = submitting || plan?.status === "generating";

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

  const confirm = async () => {
    if (!plan || disabled) return;
    setSubmitting(true);
    try {
      const result = await API.confirmShotGrid(projectName, plan.id);
      updatePlan(result.grid);
    } catch {
      useAppStore.getState().pushToast(t("shot_materials_failed"), "error");
    } finally {
      setSubmitting(false);
    }
  };

  const compositeUrl = plan?.composite_path
    ? API.getFileUrl(projectName, plan.composite_path, fingerprints[plan.composite_path] ?? null, { width: 1280 })
    : null;
  const columns = GRID_COLUMNS[plan?.cols ?? 2] ?? "grid-cols-2";

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
        {plan?.status === "completed" && !readOnly && (
          <Button type="button" size="xs" disabled={disabled || submitting} onClick={() => void confirm()}>
            {submitting ? <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" /> : <Check aria-hidden data-icon="inline-start" />}
            {t("shot_confirm_materials")}
          </Button>
        )}
        {plan?.status === "confirmed" && <span className="text-xs text-muted-foreground">{t("shot_materials_confirmed")}</span>}
      </div>
      <p className="text-xs text-muted-foreground">{t("shot_materials_hint")}</p>
      {plan?.error_message ? <p role="alert" className="text-xs text-destructive">{plan.error_message}</p> : null}
      {compositeUrl ? (
        <img src={compositeUrl} alt={t("shot_materials_preview")} className="max-h-80 w-full rounded-md border border-border object-contain" />
      ) : plan?.cells.length ? (
        <div className={`grid ${columns} gap-2`}>
          {plan.cells.map((cell) => (
            <div key={cell.index} className="aspect-video overflow-hidden rounded-md border border-border bg-muted">
              {cell.image_path ? <img src={API.getFileUrl(projectName, cell.image_path, fingerprints[cell.image_path] ?? null, { width: 320 })} alt={t("shot_keyframe", { index: cell.index })} className="size-full object-cover" /> : null}
            </div>
          ))}
        </div>
      ) : null}
      <ShotGridDialogHost />
    </section>
  );
}
