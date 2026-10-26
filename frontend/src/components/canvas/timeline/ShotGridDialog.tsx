import { useEffect, useId, useState } from "react";
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
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
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

/**
 * 「参考素材」弹窗：只定宫格规格（2×2 / 2×3 / 3×3）。逐格画面一律由分镜提示词现解析
 * （``parse_scene_shots`` + 关键帧选取），这里不再另写一份「参考图提示词」——宫格与分镜词
 * 只有一份真相，改画面就改分镜提示词。
 *
 * 已确认的宫格锁定规格；换规格先取消确认。「确定」= 保存规格 + 入队生成。
 */
export function ShotGridDialog({ request, onClose }: { request: ShotGridDialogRequest; onClose: () => void }) {
  const { t } = useTranslation("dashboard");
  const { projectName, scriptFile, episode, shotId, plan } = request;
  const layoutId = useId();
  const [layout, setLayout] = useState<ShotGridLayout>(() => layoutOf(plan));
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useTasksStore((state) => state.tasks);
  useTasksStore((state) => state.optimisticActive);
  const resourceBusy = isResourceBusy("shot_grid", projectName, plan?.id ?? shotId);

  const [rows, cols] = LAYOUTS[layout];
  const locked = plan?.status === "confirmed";
  const generating = plan?.status === "generating" || resourceBusy;
  const busy = submitting;

  const close = () => {
    if (!busy) onClose();
  };

  const submit = async () => {
    if (busy || generating) return;
    if (isResourceBusy("shot_grid", projectName, plan?.id ?? shotId)) {
      setError(t("shot_grid_generating"));
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      // prompt 传空：出图任务按 image_prompt 现解析逐格文本，不再存第二份可编辑文本。
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
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) close();
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
              <Label id={layoutId}>{t("shot_grid_layout_label")}</Label>
              <ToggleGroup
                aria-labelledby={layoutId}
                variant="outline"
                value={[layout]}
                onValueChange={(next: string[]) => {
                  // 单选：再次点击已选项不取消选择
                  if (next[0]) setLayout(next[0] as ShotGridLayout);
                }}
                disabled={locked || generating || busy}
              >
                <ToggleGroupItem value="2x2">2×2</ToggleGroupItem>
                <ToggleGroupItem value="2x3">2×3</ToggleGroupItem>
                <ToggleGroupItem value="3x3">3×3</ToggleGroupItem>
              </ToggleGroup>
              {locked ? <p className="text-xs text-muted-foreground">{t("shot_grid_layout_locked")}</p> : null}
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
          <Button disabled={busy || generating} onClick={() => void submit()}>
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
  useEffect(() => close, [close]);
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
