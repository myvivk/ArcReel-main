import { useCallback, useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { Check, Loader2, Sparkles } from "lucide-react";
import { API } from "@/api";
import { enqueueShotGrid } from "@/actions/generation";
import { Button } from "@/components/ui/button";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import type { ShotGridLayout, ShotGridPlan } from "@/types/shot-grid";
import { useAppStore } from "@/stores/app-store";
import { useProjectsStore } from "@/stores/projects-store";
import { usePromptAuthoringStore } from "@/stores/prompt-authoring-store";

interface ReferenceMaterialsSectionProps {
  projectName: string;
  scriptFile?: string;
  episode?: number;
  shotId: string;
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

/** 规格 → 行列；与后端 SUPPORTED_LAYOUTS 同一闭集（D-1）。 */
const LAYOUTS: Record<ShotGridLayout, readonly [rows: number, cols: number]> = {
  "2x2": [2, 2],
  "2x3": [2, 3],
  "3x3": [3, 3],
};

const LAYOUT_ITEMS = Object.keys(LAYOUTS) as ShotGridLayout[];

function layoutOf(plan: ShotGridPlan | null): ShotGridLayout {
  const key = `${plan?.rows ?? 2}x${plan?.cols ?? 2}` as ShotGridLayout;
  return key in LAYOUTS ? key : "2x2";
}

/**
 * 「参考素材」区：规格选择 + 直接入队，没有弹窗、没有提示词输入框（D-1）。
 *
 * 逐格镜头文本一律由服务端在出图时从 `image_prompt.scene` 现解析，这一区只负责「定规格、
 * 触发、看结果、确认」。已确认的宫格锁规格（D-4），换规格要先取消确认。
 */
export function ReferenceMaterialsSection({
  projectName,
  scriptFile,
  episode,
  shotId,
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
  const [plan, setPlan] = useState<ShotGridPlan | null>(null);
  const [layout, setLayout] = useState<ShotGridLayout>("2x2");
  const [submitting, setSubmitting] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const busy = submitting || plan?.status === "generating";
  const confirmed = plan?.status === "confirmed";

  const updatePlan = useCallback(
    (next: ShotGridPlan | null) => {
      setPlan(next);
      onPlanChange(next);
    },
    [onPlanChange],
  );

  // 记录变化（首次读取、重出图后刷新）时把规格对齐到记录；用户临时改的选择不会被读取打断。
  // 在渲染期对齐而不是 effect 里 setState：后者会多跑一轮渲染（react-hooks/set-state-in-effect）。
  const planKey = `${plan?.id ?? ""}:${plan?.updated_at ?? ""}`;
  const [syncedPlanKey, setSyncedPlanKey] = useState(planKey);
  if (syncedPlanKey !== planKey) {
    setSyncedPlanKey(planKey);
    setLayout(layoutOf(plan));
  }
  const [rows, cols] = LAYOUTS[layout];

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

  /** 直接入队：规格变了先 PUT 落规格（服务端据此重置），再按新规格出图；没有预存草稿。 */
  const generate = async () => {
    if (!scriptFile || episode === undefined || disabled || busy) return;
    if (saveFirst && !(await beforeGenerate())) return;
    setSubmitting(true);
    try {
      if (plan && (plan.rows !== rows || plan.cols !== cols)) {
        const saved = await API.saveShotGrid(projectName, {
          script_file: scriptFile,
          episode,
          shot_id: shotId,
          rows,
          cols,
        });
        updatePlan(saved);
      }
      await enqueueShotGrid(projectName, { script_file: scriptFile, episode, shot_id: shotId, rows, cols });
      setReloadKey((key) => key + 1);
    } catch {
      useAppStore.getState().pushToast(t("shot_materials_failed"), "error");
    } finally {
      setSubmitting(false);
    }
  };

  /** 宫格确认后进入视频提示词：单分镜、字段锁定为 video_prompt（D-20 的收尾条）。 */
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
  const columns = GRID_COLUMNS[plan?.cols ?? cols] ?? "grid-cols-2";

  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-2">
      <div className="flex min-h-7 flex-wrap items-center gap-2">
        <h3 id={titleId} className="min-w-0 flex-1 text-xs font-medium text-muted-foreground">
          {t("shot_reference_materials")}
        </h3>
        <ToggleGroup
          aria-label={t("shot_grid_spec_label")}
          variant="outline"
          size="sm"
          value={[layout]}
          onValueChange={(next: string[]) => {
            if (next[0]) setLayout(next[0] as ShotGridLayout);
          }}
          disabled={readOnly || disabled || busy || confirmed}
        >
          {LAYOUT_ITEMS.map((item) => (
            <ToggleGroupItem key={item} value={item}>
              {item.replace("x", "×")}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
        {!readOnly && (
          <Button
            type="button"
            variant="outline"
            size="xs"
            disabled={!scriptFile || episode === undefined || disabled || busy}
            onClick={() => void generate()}
          >
            {busy ? (
              <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
            ) : (
              <Sparkles aria-hidden data-icon="inline-start" />
            )}
            {plan ? t("shot_grid_regenerate") : t("shot_configure_materials")}
          </Button>
        )}
        {plan?.status === "completed" && !readOnly && (
          <Button type="button" size="xs" disabled={disabled || submitting} onClick={() => void confirm()}>
            {submitting ? (
              <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
            ) : (
              <Check aria-hidden data-icon="inline-start" />
            )}
            {t("shot_confirm_materials")}
          </Button>
        )}
      </div>
      {confirmed && <p className="text-xs text-muted-foreground">{t("shot_grid_spec_locked")}</p>}
      <p className="text-xs text-muted-foreground">{t("shot_materials_hint")}</p>
      {confirmed && (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-muted/40 px-2 py-1.5">
          <Check aria-hidden className="size-3.5 shrink-0 text-primary" />
          <span className="min-w-0 flex-1 text-xs text-muted-foreground">{t("shot_materials_confirmed")}</span>
          <span className="text-xs text-muted-foreground">
            {videoPromptFilled ? t("video_prompt_done") : t("video_prompt_missing")}
          </span>
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
      {plan?.error_message ? (
        <p role="alert" className="text-xs text-destructive">
          {plan.error_message}
        </p>
      ) : null}
      {compositeUrl ? (
        <img
          src={compositeUrl}
          alt={t("shot_materials_preview")}
          className="max-h-80 w-full rounded-md border border-border object-contain"
        />
      ) : plan?.cells.length ? (
        <div className={`grid ${columns} gap-2`}>
          {plan.cells.map((cell) => (
            <div key={cell.index} className="aspect-video overflow-hidden rounded-md border border-border bg-muted">
              {cell.image_path ? (
                <img
                  src={API.getFileUrl(projectName, cell.image_path, fingerprints[cell.image_path] ?? null, { width: 320 })}
                  alt={t("shot_keyframe", { index: cell.index })}
                  className="size-full object-cover"
                />
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}
