import { useId, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { AlertTriangle, Check, Loader2, Sparkles } from "lucide-react";
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
import { Textarea } from "@/components/ui/textarea";
import { enqueueShotGrid } from "@/actions/generation";
import { useShotGridDialogStore, type ShotGridDialogRequest } from "@/stores/shot-grid-dialog-store";
import type { ShotGridLayout, ShotGridPlan } from "@/types/shot-grid";
import { errMsg } from "@/utils/async";
import { isResourceBusy, useTasksStore } from "@/stores/tasks-store";
import { refreshAfterWrite } from "@/components/canvas/shared/refreshAfterWrite";
import type { ReferenceNameSets } from "@/utils/reference-mentions";

/** 规格 → 行列；与后端 SUPPORTED_LAYOUTS 同一闭集（ADR 0099 D-1）。 */
const LAYOUTS: Record<ShotGridLayout, [rows: number, cols: number]> = {
  "2x2": [2, 2],
  "2x3": [2, 3],
  "3x3": [3, 3],
};

const LAYOUT_BY_SIZE: Record<string, ShotGridLayout> = { "2x2": "2x2", "2x3": "2x3", "3x3": "3x3" };
const SHOT_SIZES = ["远景", "全景", "中景", "近景", "特写"];
const CAMERA_RE = /镜头(?:推|拉|摇|移|跟|升|降|转|切|固定|缓|慢|向前|向后|靠近|远离)|机位|景别|运镜|推近|拉远|摇镜|移镜|跟拍|环绕|变焦/;
const SEQUENCE_RE = /然后|随后|最后|继而|先[^。；\n]{0,20}再/;
const GRADUAL_RE = /逐渐|渐渐|慢慢|缓缓|越来越|不断|一点一点/;
const DIALOGUE_RE = /(台词|对白|画外音|旁白)\s*[：:]/;
const CELL_RE = /^第(\d+)格(?:[（(]([^）)]*)[）)])?[：:]\s*(.+)$/;

function layoutOf(plan: ShotGridPlan | null): ShotGridLayout {
  return LAYOUT_BY_SIZE[`${plan?.rows ?? 2}x${plan?.cols ?? 2}`] ?? "2x2";
}

interface CellEntry {
  index: number;
  shotSize: string;
  text: string;
}

interface PromptChecks {
  count: number;
  written: number;
  invalid: number;
  cellsOk: boolean;
  sizeIssue: { index: number; size: string } | null;
  hasAssets: boolean;
  assetCount: number;
  missing: string[];
  keyframeIssue: { index: number } | null;
  dialogueIssue: { index: number } | null;
  pass: boolean;
}

function parseCells(value: string): { entries: CellEntry[]; invalid: number } {
  // 与后端 parse_grid_prompt_cells 同口径：没有标记的行并入上一格；重复序号留首；
  // 标记之前的非空行才算格式错误。
  const marked: { index: number; shotSize: string; parts: string[] }[] = [];
  let leadingInvalid = 0;
  for (const raw of value.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    const match = CELL_RE.exec(line);
    if (match) {
      marked.push({ index: Number(match[1]), shotSize: (match[2] ?? "").trim(), parts: [match[3]] });
    } else if (marked.length > 0) {
      marked[marked.length - 1].parts.push(line);
    } else {
      leadingInvalid += 1;
    }
  }

  const entries: CellEntry[] = [];
  const seen = new Set<number>();
  for (const cell of marked) {
    if (seen.has(cell.index)) continue;
    const text = cell.parts.join("\n").trim();
    if (!text) continue;
    seen.add(cell.index);
    entries.push({ index: cell.index, shotSize: cell.shotSize, text });
  }
  return { entries, invalid: leadingInvalid };
}

function assetAliases(name: string): string[] {
  const aliases = new Set([name]);
  for (const part of name.split(/[・/]/)) {
    const trimmed = part.trim();
    if (trimmed.length >= 2) aliases.add(trimmed);
  }
  return [...aliases];
}

function missingAssets(names: readonly string[], text: string): string[] {
  return names.filter((name) => !assetAliases(name).some((alias) => text.includes(alias)));
}

function checkPrompt(value: string, count: number, referenceNames?: ReferenceNameSets): PromptChecks {
  const { entries, invalid } = parseCells(value);
  const indices = entries.map((entry) => entry.index);
  const unique = new Set(indices);
  const cellsOk =
    invalid === 0 &&
    entries.length === count &&
    unique.size === count &&
    indices.every((index) => index >= 1 && index <= count);

  const sizeIssue = entries.find((entry) => !SHOT_SIZES.includes(entry.shotSize)) ?? null;
  const assetNames = referenceNames
    ? [...referenceNames.characters, ...referenceNames.scenes, ...referenceNames.props]
        .map((name) => name.trim())
        .filter(Boolean)
    : [];
  const hasAssets = referenceNames !== undefined;
  const missing = missingAssets(assetNames, value);
  const keyframeEntry = entries.find(
    (entry) => CAMERA_RE.test(entry.text) || SEQUENCE_RE.test(entry.text) || GRADUAL_RE.test(entry.text),
  ) ?? null;
  const dialogueEntry = entries.find((entry) => DIALOGUE_RE.test(entry.text)) ?? null;

  const pass =
    cellsOk &&
    sizeIssue === null &&
    hasAssets &&
    missing.length === 0 &&
    keyframeEntry === null &&
    dialogueEntry === null;

  return {
    count,
    written: entries.length,
    invalid,
    cellsOk,
    sizeIssue: sizeIssue ? { index: sizeIssue.index, size: sizeIssue.shotSize } : null,
    hasAssets,
    assetCount: assetNames.length,
    missing,
    keyframeIssue: keyframeEntry ? { index: keyframeEntry.index } : null,
    dialogueIssue: dialogueEntry ? { index: dialogueEntry.index } : null,
    pass,
  };
}

function CheckLine({ ok, children }: { ok: boolean; children: ReactNode }) {
  return (
    <li className="flex items-start gap-1.5 text-xs leading-5">
      {ok ? (
        <Check aria-hidden className="mt-0.5 size-3.5 shrink-0 text-media-text" />
      ) : (
        <AlertTriangle aria-hidden className="mt-0.5 size-3.5 shrink-0 text-media-audio" />
      )}
      <span className={ok ? "text-muted-foreground" : "text-foreground"}>{children}</span>
    </li>
  );
}

/**
 * 「参考素材」弹窗：定宫格规格 + 一个文本框写所有格的画面描述。
 *
 * 输入框沿用后端已有的 ``第N格（景别）：`` 逐格文本格式；AI 生成走
 * ``/shot-grids/author-prompt``，只回填文本，不落盘。硬性检查不通过时不能提交；
 * 「确定并生成」才保存 prompt 并入队。
 */
export function ShotGridDialog({ request, onClose }: { request: ShotGridDialogRequest; onClose: () => void }) {
  const { t } = useTranslation("dashboard");
  const { projectName, scriptFile, episode, shotId, plan, referenceNames, allowConfirmedRegenerate = false } = request;
  const promptId = useId();
  const [layout, setLayout] = useState<ShotGridLayout>(() => layoutOf(plan));
  const [prompt, setPrompt] = useState(() => plan?.prompt ?? "");
  const [submitting, setSubmitting] = useState(false);
  const [authoring, setAuthoring] = useState(false);
  const [savingDraft, setSavingDraft] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useTasksStore((state) => state.tasks);
  useTasksStore((state) => state.optimisticActive);
  const resourceBusy = isResourceBusy("shot_grid", projectName, plan?.id ?? shotId);

  const [rows, cols] = LAYOUTS[layout];
  const count = rows * cols;
  const layoutItems: { value: ShotGridLayout; label: string }[] = [
    { value: "2x2", label: `2×2 · ${t("shot_grid_cells_count", { count: 4 })}` },
    { value: "2x3", label: `2×3 · ${t("shot_grid_cells_count", { count: 6 })}` },
    { value: "3x3", label: `3×3 · ${t("shot_grid_cells_count", { count: 9 })}` },
  ];
  const confirmed = plan?.status === "confirmed";
  const promptLocked = confirmed && !allowConfirmedRegenerate;
  const generating = plan?.status === "generating" || resourceBusy;
  const busy = submitting || authoring || savingDraft;
  const checks = useMemo(() => checkPrompt(prompt, count, referenceNames), [prompt, count, referenceNames]);
  const canSubmit = checks.pass && !busy && !generating && !promptLocked;

  const close = async () => {
    if (busy) return;
    // 已确认的宫格关窗不写草稿：确认态的文本只能经「确定并生成」重新落盘。
    if (confirmed) {
      onClose();
      return;
    }
    // 关窗即存：只要已有记录或文本框里有内容，就把当前文本写成草稿；
    // 首次打开且完全没写时不建空记录。
    const shouldSaveDraft = prompt.trim().length > 0 || plan !== null;
    if (!shouldSaveDraft) {
      onClose();
      return;
    }
    setSavingDraft(true);
    setError(null);
    try {
      await API.saveShotGridDraft(projectName, {
        script_file: scriptFile,
        episode,
        shot_id: shotId,
        rows,
        cols,
        prompt,
      });
      onClose();
    } catch (cause) {
      setError(errMsg(cause));
    } finally {
      setSavingDraft(false);
    }
  };

  const author = async () => {
    if (busy || generating || promptLocked) return;
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
      setPrompt(result.prompt);
    } catch (cause) {
      setError(errMsg(cause));
    } finally {
      setAuthoring(false);
    }
  };

  const submit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const saved = await API.saveShotGrid(projectName, {
        script_file: scriptFile,
        episode,
        shot_id: shotId,
        rows,
        cols,
        prompt,
      });
      await enqueueShotGrid(projectName, scriptFile, shotId, saved.id);
      if ((await refreshAfterWrite(projectName, t)) === "success") onClose();
    } catch (cause) {
      setError(errMsg(cause));
    } finally {
      setSubmitting(false);
    }
  };

  const countsText = checks.invalid > 0
    ? t("shot_grid_prompt_invalid", { invalid: checks.invalid })
    : checks.written === 0
      ? t("shot_grid_prompt_required")
      : checks.written === count
        ? t("shot_grid_prompt_ready", { count })
        : checks.written < count
          ? t("shot_grid_prompt_continuation", { written: checks.written, count })
          : t("shot_grid_prompt_overflow", { written: checks.written, count });

  const cellsOk = checks.cellsOk && checks.sizeIssue === null;
  const assetsOk = checks.hasAssets && checks.missing.length === 0;
  const keyframeOk = checks.keyframeIssue === null;
  const dialogueOk = checks.dialogueIssue === null;

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
          <DialogDescription>{t("shot_grid_dialog_desc2")}</DialogDescription>
        </DialogHeader>
        <DialogBody>
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label id={`${promptId}-layout`}>{t("shot_grid_layout_label")}</Label>
              <Select
                items={layoutItems}
                value={layout}
                onValueChange={(next) => {
                  if (next) setLayout(next);
                }}
                disabled={confirmed || generating || busy}
              >
                <SelectTrigger aria-labelledby={`${promptId}-layout`} className="w-48">
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

            <div className="flex flex-col gap-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <Label htmlFor={promptId}>{t("shot_grid_prompt_label", { count })}</Label>
                <Button
                  type="button"
                  size="sm"
                  disabled={busy || generating || promptLocked}
                  onClick={() => void author()}
                >
                  {authoring ? (
                    <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
                  ) : (
                    <Sparkles aria-hidden data-icon="inline-start" />
                  )}
                  {authoring ? t("shot_grid_ai_generating") : t("shot_grid_prompt_author")}
                </Button>
              </div>
              <Textarea
                id={promptId}
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                placeholder={t("shot_grid_prompt_placeholder")}
                disabled={generating || submitting || promptLocked}
                className="min-h-48 max-h-72"
              />
              <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                <span className={checks.invalid > 0 ? "text-destructive" : "text-muted-foreground"}>{countsText}</span>
                <span className="text-muted-foreground">{t("shot_grid_prompt_note")}</span>
              </div>
            </div>

            <div className="rounded-lg border border-border/70 bg-muted/20 p-3">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-medium">{t("shot_grid_check_title")}</span>
                <span className={checks.pass ? "text-xs font-medium text-media-text" : "text-xs font-medium text-destructive"}>
                  {checks.pass ? t("shot_grid_check_pass") : t("shot_grid_check_fail")}
                </span>
              </div>
              <ul className="mt-2 flex flex-col gap-1">
                <CheckLine ok={cellsOk}>
                  {checks.cellsOk
                    ? t("shot_grid_check_cells_ok", { count })
                    : checks.invalid > 0
                      ? t("shot_grid_check_cells_invalid", { invalid: checks.invalid })
                      : t("shot_grid_check_cells_bad", { count, written: checks.written })}
                  {checks.sizeIssue
                    ? ` ${t("shot_grid_check_shot_size_bad", {
                        cell: `第${checks.sizeIssue.index}格`,
                        size: checks.sizeIssue.size || "—",
                      })}`
                    : null}
                </CheckLine>
                <CheckLine ok={assetsOk}>
                  {!checks.hasAssets
                    ? t("shot_grid_check_assets_unknown")
                    : checks.missing.length > 0
                      ? t("shot_grid_check_assets_missing", { names: checks.missing.join("、") })
                      : t("shot_grid_check_assets_ok", { count: checks.assetCount })}
                </CheckLine>
                {checks.written > 0 ? (
                  <>
                    <CheckLine ok={keyframeOk}>
                      {checks.keyframeIssue
                        ? t("shot_grid_check_keyframe_bad", { cell: `第${checks.keyframeIssue.index}格` })
                        : t("shot_grid_check_keyframe_ok")}
                    </CheckLine>
                    <CheckLine ok={dialogueOk}>
                      {checks.dialogueIssue
                        ? t("shot_grid_check_dialogue_bad", { cell: `第${checks.dialogueIssue.index}格` })
                        : t("shot_grid_check_dialogue_ok")}
                    </CheckLine>
                  </>
                ) : null}
              </ul>
            </div>

            {error ? (
              <p role="alert" className="text-xs text-destructive">
                {error}
              </p>
            ) : null}
          </div>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" disabled={busy} onClick={() => void close()}>
            {savingDraft ? <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" /> : null}
            {savingDraft ? t("shot_grid_draft_saving") : t("common:cancel")}
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
