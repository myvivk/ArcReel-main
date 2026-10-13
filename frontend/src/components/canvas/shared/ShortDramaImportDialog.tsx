import { useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { FileText, Loader2 } from "lucide-react";
import {
  importShortDramaScript,
  importShortDramaSeries,
  createBlankEpisodes,
  type ShortDramaImportDetail,
} from "@/actions/shortDrama";
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
import { refreshAfterWrite } from "@/components/canvas/shared/refreshAfterWrite";
import { useEpisodeLedger } from "@/hooks/useEpisodeLedger";
import { useShortDramaImportStore, type ShortDramaImportOpenRequest } from "@/stores/short-drama-import-store";
import { episodeDisplayName } from "@/utils/episode-display";

const HEADER_RE = /^(?:#\s*)?第\s*[【[]?\s*(\d+)\s*[】\]]?\s*集(?:[：:\s]|$)/;

/** 把多集拼接的 Markdown 按「第 N 集」标题拆成 [{episode_id, markdown}]；标题行保留在正文里（转写器据此取集数/集名）。 */
export function parseShortDramaBatch(text: string): Array<{ episode_id: number; markdown: string }> {
  const blocks: Array<{ episode_id: number; markdown: string }> = [];
  let current: { episode_id: number; lines: string[] } | null = null;
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(HEADER_RE);
    if (match) {
      if (current) blocks.push({ episode_id: current.episode_id, markdown: current.lines.join("\n").trim() });
      current = { episode_id: Number(match[1]), lines: [line] };
    } else if (current) {
      current.lines.push(line);
    }
  }
  if (current) blocks.push({ episode_id: current.episode_id, markdown: current.lines.join("\n").trim() });
  return blocks.filter((block) => block.markdown.length > 0);
}

/** 集页上唯一的短剧工厂「导入剧本」弹窗宿主：按 {@link useShortDramaImportStore} 的请求打开。 */
export function ShortDramaImportHost({ projectName, episode }: { projectName: string; episode: number }) {
  const request = useShortDramaImportStore((s) => s.request);
  const close = useShortDramaImportStore((s) => s.close);
  useEffect(() => close, [close, projectName, episode]);
  if (!request || request.projectName !== projectName) return null;
  return <ShortDramaImportDialog request={request} onClose={close} />;
}

interface DialogProps {
  request: ShortDramaImportOpenRequest;
  onClose: () => void;
}

type Mode = "single" | "batch";

/**
 * 短剧工厂「导入剧本」：把 short-drama-factory 的 Markdown 转写为正式剧情演绎剧本并落盘。
 * 转写规则（与 Agent 入口一致，固化在此处）：动作/神态进 scene_description、台词进 utterances、
 * 单集按项目默认分镜时长切分多幕（未设置时使用 8 秒；导入阶段不读取视频供应商能力）、
 * 视觉层留空待 generate_episode_script 补出、集尾断章对齐 hook。
 * 集须已存在；批量模式可先「批量建空白集」再粘贴多集 Markdown（以「第 N 集」标题分隔，N 即集 ID）。
 */
function ShortDramaImportDialog({ request, onClose }: DialogProps) {
  const { t } = useTranslation("dashboard");
  const ledger = useEpisodeLedger();
  const projectName = request.projectName;
  const fieldId = useId();
  const [mode, setMode] = useState<Mode>("single");
  const [episodeId, setEpisodeId] = useState<number | "">(request.episode ?? ledger[0]?.episode ?? "");
  const [markdown, setMarkdown] = useState("");
  const [batchText, setBatchText] = useState("");
  const [blankCount, setBlankCount] = useState(10);
  const [creating, setCreating] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<{ imported: number; failed: number; details: ShortDramaImportDetail[] } | null>(null);

  const canSubmit =
    mode === "single"
      ? episodeId !== "" && markdown.trim().length > 0
      : parseShortDramaBatch(batchText).length > 0;

  const createBlanks = async () => {
    if (creating || blankCount <= 0) return;
    setCreating(true);
    try {
      await createBlankEpisodes(projectName, blankCount);
      await refreshAfterWrite(projectName, t);
    } finally {
      setCreating(false);
    }
  };

  const submit = async () => {
    if (submitting || !canSubmit) return;
    setSubmitting(true);
    setResult(null);
    try {
      if (mode === "single") {
        await importShortDramaScript(projectName, Number(episodeId), markdown);
        await refreshAfterWrite(projectName, t);
        onClose();
      } else {
        const episodes = parseShortDramaBatch(batchText);
        const res = await importShortDramaSeries(projectName, episodes, true);
        await refreshAfterWrite(projectName, t);
        setResult(res);
      }
    } finally {
      setSubmitting(false);
    }
  };

  const submitLabel = mode === "single" ? t("short_drama_import_submit") : t("short_drama_import_submit_batch");

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next && !submitting) onClose();
      }}
    >
      <DialogContent showCloseButton={!submitting}>
        <DialogHeader>
          <DialogTitle>{t("short_drama_import_title")}</DialogTitle>
          <DialogDescription>{t("short_drama_import_desc")}</DialogDescription>
        </DialogHeader>
        <DialogBody>
          <div className="flex flex-col gap-4">
            <div className="flex gap-2">
              <Button
                type="button"
                variant={mode === "single" ? "default" : "outline"}
                size="sm"
                onClick={() => setMode("single")}
              >
                {t("short_drama_import_mode_single")}
              </Button>
              <Button
                type="button"
                variant={mode === "batch" ? "default" : "outline"}
                size="sm"
                onClick={() => setMode("batch")}
              >
                {t("short_drama_import_mode_batch")}
              </Button>
            </div>

            {mode === "single" ? (
              <div className="flex flex-col gap-1.5">
                <Label htmlFor={`${fieldId}-episode`}>{t("short_drama_import_episode_label")}</Label>
                <select
                  id={`${fieldId}-episode`}
                  className="h-9 rounded-md border border-input bg-background px-3 text-sm"
                  value={episodeId}
                  onChange={(event) => setEpisodeId(event.target.value === "" ? "" : Number(event.target.value))}
                  disabled={submitting}
                >
                  <option value="">{t("short_drama_import_episode_placeholder")}</option>
                  {ledger.map((ep) => (
                    <option key={ep.episode} value={ep.episode}>
                      {episodeDisplayName(ledger, ep.episode, t)}
                    </option>
                  ))}
                </select>
                <Label htmlFor={`${fieldId}-md`} className="mt-2">
                  {t("short_drama_import_markdown_label")}
                </Label>
                <Textarea
                  id={`${fieldId}-md`}
                  value={markdown}
                  onChange={(event) => setMarkdown(event.target.value)}
                  placeholder={t("short_drama_import_markdown_placeholder_single")}
                  disabled={submitting}
                  className="min-h-48"
                />
              </div>
            ) : (
              <div className="flex flex-col gap-3">
                <div className="flex flex-wrap items-end gap-2 rounded-md border border-dashed border-input p-3">
                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor={`${fieldId}-blank`}>{t("short_drama_import_create_blank_label")}</Label>
                    <input
                      id={`${fieldId}-blank`}
                      type="number"
                      min={1}
                      max={200}
                      value={blankCount}
                      onChange={(event) => setBlankCount(Number(event.target.value))}
                      disabled={creating || submitting}
                      className="h-9 w-24 rounded-md border border-input bg-background px-3 text-sm"
                    />
                  </div>
                  <Button type="button" variant="outline" size="sm" onClick={() => void createBlanks()} disabled={creating || submitting}>
                    {creating ? <Loader2 className="size-4 animate-spin" /> : null}
                    {t("short_drama_import_create_blank_button")}
                  </Button>
                  <span className="text-xs text-muted-foreground">{t("short_drama_import_create_blank_hint")}</span>
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor={`${fieldId}-batch`}>{t("short_drama_import_markdown_label")}</Label>
                  <Textarea
                    id={`${fieldId}-batch`}
                    value={batchText}
                    onChange={(event) => setBatchText(event.target.value)}
                    placeholder={t("short_drama_import_markdown_placeholder_batch")}
                    disabled={submitting}
                    className="min-h-56"
                  />
                </div>
                {result && (
                  <div className="flex flex-col gap-1.5 rounded-md border border-input p-3 text-sm">
                    <p>
                      {t("short_drama_import_result_summary", {
                        imported: result.imported,
                        failed: result.failed,
                      })}
                    </p>
                    {result.details.map((detail) => (
                      <div key={detail.episode}>
                        <p className={detail.ok ? "text-primary" : "text-destructive"}>
                          {t("short_drama_import_result_detail", {
                            episode: detail.episode,
                            status: detail.ok
                              ? t("short_drama_import_result_ok")
                              : t("short_drama_import_result_failed"),
                            info: detail.ok ? String(detail.scenes ?? 0) : detail.error ?? "",
                          })}
                        </p>
                        {detail.ok && detail.duration_shortfall ? (
                          <p className="text-warn">
                            {t("short_drama_import_result_shortfall", {
                              declared: detail.declared_seconds ?? 0,
                              actual: detail.actual_seconds ?? 0,
                            })}
                          </p>
                        ) : null}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={() => void submit()} disabled={submitting || !canSubmit}>
            {submitting ? <Loader2 className="size-4 animate-spin" /> : <FileText aria-hidden data-icon="inline-start" />}
            {submitLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

interface ButtonProps {
  projectName: string;
  episode: number;
  prominent?: boolean;
}

/** 打开短剧工厂「导入剧本」弹窗的入口；置于剧情演绎项目的集页页头动作区。 */
export function ShortDramaImportButton({ projectName, episode, prominent = false }: ButtonProps) {
  const { t } = useTranslation("dashboard");
  const open = useShortDramaImportStore((s) => s.open);
  return (
    <Button
      variant={prominent ? "default" : "outline"}
      size={prominent ? "default" : "sm"}
      onClick={() => open({ projectName, episode })}
    >
      <FileText aria-hidden data-icon="inline-start" />
      {t("short_drama_import_open")}
    </Button>
  );
}
