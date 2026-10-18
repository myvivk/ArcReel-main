import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2, Sparkles } from "lucide-react";
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
import { Textarea } from "@/components/ui/textarea";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { enqueueShotGrid } from "@/actions/generation";
import { useShotGridDialogStore, type ShotGridDialogRequest } from "@/stores/shot-grid-dialog-store";
import type { ShotGridLayout, ShotGridPlan } from "@/types/shot-grid";
import { errMsg } from "@/utils/async";

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

/** 输入框里已写成的格数：按服务端同一口径数非空行。 */
function writtenCells(prompt: string): number {
  return prompt.split("\n").filter((line) => line.trim()).length;
}

/**
 * 「参考素材」弹窗：定宫格规格、编辑（或让 AI 写）逐格镜头提示词，「确定」落盘并入队。
 *
 * 规格 2×2 / 2×3 / 3×3，默认 2×2；打开时读这一分镜已有的宫格记录，逐格文本回填输入框。
 * 已确认的宫格锁死规格——重做只能改提示词重新生成（R7），换规格被服务端 409 拒绝。
 * 「确定」= 保存配置 + 入队；AI 生成返回与关窗（取消 / Esc / ×）都把输入框里的文本存成草稿，
 * 下次打开自动回填——草稿只写逐格文本，不动生成状态，也不会清掉已出的宫格图。
 */
export function ShotGridDialog({ request, onClose }: { request: ShotGridDialogRequest; onClose: () => void }) {
  const { t } = useTranslation("dashboard");
  const { projectName, scriptFile, episode, shotId, plan } = request;
  const layoutId = useId();
  const promptId = useId();
  const [layout, setLayout] = useState<ShotGridLayout>(() => layoutOf(plan));
  const [prompt, setPrompt] = useState(plan?.prompt ?? "");
  const [authoring, setAuthoring] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [closing, setClosing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  //: 已落盘的逐格文本：只有它与输入框不一致才需要存草稿。
  const baseline = useRef(plan?.prompt ?? "");

  const [rows, cols] = LAYOUTS[layout];
  const count = rows * cols;
  const locked = plan?.status === "confirmed";
  const generating = plan?.status === "generating";
  const written = writtenCells(prompt);
  const busy = submitting || closing;

  const persistDraft = async (text: string) => {
    await API.saveShotGridDraft(projectName, {
      script_file: scriptFile,
      episode,
      shot_id: shotId,
      rows,
      cols,
      prompt: text,
    });
    baseline.current = text;
  };

  const author = async () => {
    setAuthoring(true);
    setError(null);
    try {
      const result = await API.authorShotGridPrompt(projectName, {
        script_file: scriptFile,
        episode,
        shot_id: shotId,
        rows,
        cols,
      });
      // AI 产出的是整套逐格文本：回填后由用户在手改里定稿；先落成草稿，关窗也不会丢。
      setPrompt(result.prompt);
      await persistDraft(result.prompt).catch(() => {
        // 草稿没存上不影响回填：文本还在输入框里，关窗时会再存一次。
      });
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setAuthoring(false);
    }
  };

  const closeWithDraft = async () => {
    if (busy) return;
    if (prompt === baseline.current) {
      onClose();
      return;
    }
    setClosing(true);
    try {
      await persistDraft(prompt);
    } catch {
      // 草稿写失败不挡关闭：下次打开至多回到上一次落盘的文本。
    } finally {
      setClosing(false);
    }
    onClose();
  };

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      await API.saveShotGrid(projectName, {
        script_file: scriptFile,
        episode,
        shot_id: shotId,
        rows,
        cols,
        prompt,
      });
      await enqueueShotGrid(projectName, scriptFile, shotId);
      onClose();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setSubmitting(false);
    }
  };

  const hint = useMemo(() => {
    if (written === 0) return t("shot_grid_prompt_empty_hint");
    if (written > count) return t("shot_grid_prompt_overflow", { written, count });
    if (written < count) return t("shot_grid_prompt_continuation", { written, count });
    return t("shot_grid_prompt_ready", { count });
  }, [written, count, t]);

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) void closeWithDraft();
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
                disabled={locked || generating}
              >
                <ToggleGroupItem value="2x2">2×2</ToggleGroupItem>
                <ToggleGroupItem value="2x3">2×3</ToggleGroupItem>
                <ToggleGroupItem value="3x3">3×3</ToggleGroupItem>
              </ToggleGroup>
              {locked ? <p className="text-xs text-muted-foreground">{t("shot_grid_layout_locked")}</p> : null}
              {generating ? <p className="text-xs text-muted-foreground">{t("shot_grid_generating")}</p> : null}
            </div>
            <div className="flex flex-col gap-1.5">
              <div className="flex flex-wrap items-center gap-2">
                <Label htmlFor={promptId} className="flex-1">
                  {t("shot_grid_prompt_label", { count })}
                </Label>
                <Button
                  type="button"
                  variant="outline"
                  size="xs"
                  disabled={authoring || busy}
                  onClick={() => void author()}
                >
                  {authoring ? (
                    <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
                  ) : (
                    <Sparkles aria-hidden data-icon="inline-start" />
                  )}
                  {t("shot_grid_prompt_author")}
                </Button>
              </div>
              <Textarea
                id={promptId}
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                maxLength={12000}
                rows={10}
                className="max-h-none min-h-40"
                mono
                placeholder={t("shot_grid_prompt_placeholder", { count })}
                disabled={submitting}
              />
              <p className="text-xs text-muted-foreground">{hint}</p>
              <p className="text-xs text-muted-foreground">{t("shot_grid_prompt_note")}</p>
              <p className="text-xs text-muted-foreground">{t("shot_grid_prompt_draft_note")}</p>
            </div>
            {error ? (
              <p role="alert" className="text-xs text-destructive">
                {error}
              </p>
            ) : null}
          </div>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" disabled={busy} onClick={() => void closeWithDraft()}>
            {t("common:cancel")}
          </Button>
          <Button disabled={busy || authoring || generating || written === 0} onClick={() => void submit()}>
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
