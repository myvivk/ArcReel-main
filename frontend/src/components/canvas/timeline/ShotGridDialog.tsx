import { useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2 } from "lucide-react";
import { API } from "@/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { enqueueShotGrid } from "@/actions/generation";
import { useShotGridDialogStore, type ShotGridDialogRequest } from "@/stores/shot-grid-dialog-store";
import type { ShotGridLayout, ShotGridPlan } from "@/types/shot-grid";
import { errMsg } from "@/utils/async";
import { isResourceBusy, useTasksStore } from "@/stores/tasks-store";
import { refreshAfterWrite } from "@/components/canvas/shared/refreshAfterWrite";

/** 规格 → 行列；与后端 SUPPORTED_LAYOUTS 同一闭集（ADR 0099 D-1）。 */
const LAYOUTS: Record<ShotGridLayout, [rows: number, cols: number]> = {
  "2x2": [2, 2],
  "2x3": [2, 3],
  "3x3": [3, 3],
};

const LAYOUT_BY_SIZE: Record<string, ShotGridLayout> = { "2x2": "2x2", "2x3": "2x3", "3x3": "3x3" };

function layoutOf(plan: ShotGridPlan | null): ShotGridLayout {
  return LAYOUT_BY_SIZE[`${plan?.rows ?? 2}x${plan?.cols ?? 2}`] ?? "2x2";
}

/** 参考素材弹窗只设置规格；生成直接使用已保存的分镜提示词。 */
export function ShotGridDialog({ request, onClose }: { request: ShotGridDialogRequest; onClose: () => void }) {
  const { t } = useTranslation("dashboard");
  const { projectName, scriptFile, episode, shotId, plan, allowConfirmedRegenerate = false } = request;
  const [layout, setLayout] = useState<ShotGridLayout>(() => request.layout ?? layoutOf(plan));
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useTasksStore((state) => state.tasks);
  useTasksStore((state) => state.optimisticActive);
  const resourceBusy = isResourceBusy("shot_grid", projectName, plan?.id ?? shotId);

  const [rows, cols] = LAYOUTS[layout];
  const layoutLabelId = useId();
  const layoutItems: { value: ShotGridLayout; label: string }[] = [
    { value: "2x2", label: `2×2 · ${t("shot_grid_cells_count", { count: 4 })}` },
    { value: "2x3", label: `2×3 · ${t("shot_grid_cells_count", { count: 6 })}` },
    { value: "3x3", label: `3×3 · ${t("shot_grid_cells_count", { count: 9 })}` },
  ];
  const confirmed = plan?.status === "confirmed";
  const promptLocked = confirmed && !allowConfirmedRegenerate;
  const generating = plan?.status === "generating" || resourceBusy;
  const busy = submitting;
  const canSubmit = !busy && !generating && !promptLocked;

  const close = () => {
    if (!busy) onClose();
  };

  const submit = async () => {
    if (!canSubmit || isResourceBusy("shot_grid", projectName, plan?.id ?? shotId)) return;
    setSubmitting(true);
    setError(null);
    try {
      const saved = await API.saveShotGrid(projectName, {
        script_file: scriptFile,
        episode,
        shot_id: shotId,
        rows,
        cols,
        prompt: "",
      });
      await enqueueShotGrid(projectName, scriptFile, shotId, saved.id);
      if ((await refreshAfterWrite(projectName, t)) === "success") onClose();
    } catch (cause) {
      setError(errMsg(cause));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) void close();
      }}
    >
      <DialogContent size="lg" showCloseButton={!busy}>
        <DialogHeader>
          <DialogTitle>{t("shot_grid_dialog_title")}</DialogTitle>
          <DialogDescription>{t("shot_grid_dialog_desc")}</DialogDescription>
        </DialogHeader>
        <DialogBody>
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label id={layoutLabelId}>{t("shot_grid_layout_label")}</Label>
              <Select
                items={layoutItems}
                value={layout}
                onValueChange={(next) => {
                  if (next) setLayout(next);
                }}
                disabled={confirmed || generating || busy}
              >
                <SelectTrigger aria-labelledby={layoutLabelId} className="w-48">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {layoutItems.map((item) => (
                    <SelectItem key={item.value} value={item.value}>
                      {item.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {confirmed ? <p className="text-xs text-muted-foreground">{t("shot_grid_layout_locked")}</p> : null}
              {generating ? <p className="text-xs text-muted-foreground">{t("shot_grid_generating")}</p> : null}
            </div>

            <p className="text-xs text-muted-foreground">{t("shot_grid_from_storyboard_note")}</p>

            {error ? (
              <p role="alert" className="text-xs text-destructive">
                {error}
              </p>
            ) : null}
          </div>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" disabled={busy} onClick={close}>
            {t("common:cancel")}
          </Button>
          <Button disabled={!canSubmit} onClick={() => void submit()}>
            {submitting ? <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" /> : null}
            {submitting ? t("shot_grid_submitting") : t("shot_grid_confirm")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** 分镜详情上唯一的宿主：按 {@link useShotGridDialogStore} 的请求打开。 */
export function ShotGridDialogHost() {
  const request = useShotGridDialogStore((s) => s.request);
  const close = useShotGridDialogStore((s) => s.close);
  if (!request) return null;
  return (
    <ShotGridDialog
      // 记录每更新一次就重新初始化：不沿用上一次对话框里未提交的输入。
      key={`${request.projectName}:${request.scriptFile}:${request.shotId}:${request.plan?.updated_at ?? "new"}`}
      request={request}
      onClose={close}
    />
  );
}
