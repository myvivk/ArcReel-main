import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { AlertTriangle, Loader2, Sparkles } from "lucide-react";

import { API, ApiRequestError, type SourceImportOriginal, type SourceImportResult } from "@/api";
import { useLeaveGuard } from "@/components/shared/edit-unit/LeaveGuard";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { errMsg } from "@/utils/async";
import { SOURCE_IMPORT_MAX_CHARS } from "@/utils/creation-mode";
import { EpisodeHeaderActions } from "./EpisodeHeaderActions";

interface SourceImportPanelProps {
  projectName: string;
  episode: number;
  /** 已有源稿：原文视图，提供「保存原文」与「重新导入」。 */
  hasScript: boolean;
  /** 当前源稿 revision（重新导入的并发基准）；还没有源稿时 null。 */
  sourceRevision: string | null;
  /** 本集已有分镜图或视频：重新生成前追加下游过期提示，并用危险样式确认。 */
  hasMediaArtifacts: boolean;
  /** 生成成功后父级递增它，让面板重新读取原文 revision 与 stale 状态。 */
  reloadToken: number;
  /** 生成成功：父级切到剧本视图、展示机检告警并刷新项目与源稿。 */
  onImported: (result: SourceImportResult) => void;
  /** 切到「原文」视图：制作进度里的「重新导入」要让用户看到风险提示。 */
  onShowOriginal: () => void;
  /** 原文与剧本是否已不同步（原文已改、剧本未重新生成）；父级据此在视图顶部提示。 */
  onStaleChange: (stale: boolean) => void;
}

/**
 * 导入原文的内联面板（不是弹窗）：无源稿时粘贴本集原文生成剧本；
 * 有源稿时是「原文」视图，可编辑并保存原文，或用当前原文重新导入。
 * 风险提示（重新导入会覆盖剧本、让下游媒体过期）也在面板内展开，不再另开弹窗。
 */
export function SourceImportPanel({
  projectName,
  episode,
  hasScript,
  sourceRevision,
  hasMediaArtifacts,
  reloadToken,
  onImported,
  onShowOriginal,
  onStaleChange,
}: SourceImportPanelProps) {
  const { t, i18n } = useTranslation(["dashboard", "common"]);
  const textId = useId();
  const countId = useId();

  const [original, setOriginal] = useState<SourceImportOriginal | null>(null);
  const [loadedKey, setLoadedKey] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [draft, setDraft] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedNotice, setSavedNotice] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const controllerRef = useRef<AbortController | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  const requestKey = `${projectName}:${episode}:${reloadToken}:${attempt}`;
  // 原文与剧本分开落盘：还没有生成剧本时也可能已经保存过原文（生成前先存一份）。
  const loading = loadedKey !== requestKey;
  const text = draft;
  const tooLong = text.length > SOURCE_IMPORT_MAX_CHARS;
  const dirty = !loading && text !== (original?.text ?? "");
  const stale = Boolean(original?.stale);

  // 原文只在进入原文视图时读取一次；重试按钮换 requestKey 重新拉取。
  useEffect(() => {
    const controller = new AbortController();
    API.getSourceImportOriginal(projectName, episode, { signal: controller.signal })
      .then((value) => {
        if (controller.signal.aborted) return;
        setOriginal(value);
        setDraft(value.text);
        setLoadedKey(requestKey);
        setLoadError(null);
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        // 还没有原文时后端回 404：按「没有原文」处理，面板照常可编辑。
        if (cause instanceof ApiRequestError && cause.status === 404) {
          setOriginal(null);
          setLoadedKey(requestKey);
          setLoadError(null);
          return;
        }
        setOriginal(null);
        setLoadedKey(requestKey);
        setLoadError(errMsg(cause));
      });
    return () => controller.abort();
  }, [projectName, episode, requestKey]);

  useEffect(() => {
    onStaleChange(stale);
  }, [stale, onStaleChange]);

  // 面板是这一页的主输入：加载完成后把焦点交给原文输入框（不用 autoFocus，避免打断读屏）。
  useEffect(() => {
    if (!loading) textareaRef.current?.focus();
  }, [loading]);

  // 离开页面即取消在途生成；服务端随断开取消文本任务。
  useEffect(() => () => controllerRef.current?.abort(), []);

  const saveOriginal = useCallback(async (): Promise<boolean> => {
    if (saving || generating) return false;
    if (tooLong) {
      setError(t("source_import_too_long", { limit: SOURCE_IMPORT_MAX_CHARS.toLocaleString(i18n.language) }));
      return false;
    }
    setSaving(true);
    setError(null);
    setSavedNotice(false);
    try {
      const result = await API.putSourceImportOriginal(projectName, episode, {
        text,
        expected_revision: original?.revision ?? null,
      });
      setOriginal((current) => ({
        text,
        revision: result.revision,
        generated_from_revision: current?.generated_from_revision ?? null,
        stale: result.stale,
        exists: true,
      }));
      setSavedNotice(true);
      return true;
    } catch (cause) {
      setError(errMsg(cause));
      return false;
    } finally {
      setSaving(false);
    }
  }, [episode, generating, i18n.language, original, projectName, saving, t, text, tooLong]);

  const discardOriginal = useCallback(() => setDraft(original?.text ?? ""), [original]);

  // 原文编辑未保存时走现有离开拦截；首次导入时粘贴的原文同样受保护。
  useLeaveGuard({
    dirty,
    saving,
    save: saveOriginal,
    discard: discardOriginal,
    title: t("source_import_leave_title"),
  });

  const cancelGenerate = () => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    setGenerating(false);
  };

  const runImport = async (confirmArtifacts: boolean) => {
    if (generating || saving) return;
    if (!text.trim()) {
      setError(t("source_import_text_required"));
      return;
    }
    if (tooLong) {
      setError(t("source_import_too_long", { limit: SOURCE_IMPORT_MAX_CHARS.toLocaleString(i18n.language) }));
      return;
    }
    const controller = new AbortController();
    controllerRef.current = controller;
    setGenerating(true);
    setError(null);
    setConfirming(false);
    try {
      const result = await API.importSourceScript(
        projectName,
        episode,
        {
          text,
          // 并发基准是源稿 revision（不是原文 revision）；首次导入为 null。
          expected_revision: hasScript ? sourceRevision : null,
          confirm_artifacts: confirmArtifacts,
        },
        { signal: controller.signal },
      );
      if (controller.signal.aborted) return;
      setSavedNotice(false);
      onImported(result);
    } catch (cause) {
      if (controller.signal.aborted) return;
      setError(errMsg(cause));
    } finally {
      if (controllerRef.current === controller) {
        controllerRef.current = null;
        setGenerating(false);
      }
    }
  };

  const limit = SOURCE_IMPORT_MAX_CHARS.toLocaleString(i18n.language);
  const canGenerate = !loading && !generating && !saving && !tooLong && text.trim().length > 0;
  const busy = generating || saving;

  return (
    <section className="flex min-h-0 w-full flex-1 flex-col gap-4">
      <header className="flex shrink-0 flex-col gap-1">
        <h2 className="text-base font-semibold text-foreground">
          {t(hasScript ? "source_import_original_title" : "source_import_first_title")}
        </h2>
        <p className="text-sm leading-relaxed text-muted-foreground">
          {t(hasScript ? "source_import_original_hint" : "source_import_first_desc")}
        </p>
      </header>
      {/* 动作与其它页面一致：登记给「制作进度」的剧本行，由面板统一调度。 */}
      <EpisodeHeaderActions target="source">
        {hasScript ? (
          <>
            <Button variant="outline" onClick={() => void saveOriginal()} disabled={!dirty || busy || !text.trim()}>
              {saving && <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />}
              {t(saving ? "source_import_saving_original" : "source_import_save_original")}
            </Button>
            <Button
              onClick={() => {
                // 重新导入的风险提示在原文面板内：先切到原文视图，用户才看得到。
                onShowOriginal();
                setConfirming(true);
              }}
              disabled={!canGenerate}
            >
              <Sparkles aria-hidden data-icon="inline-start" />
              {t("source_import_reimport")}
            </Button>
          </>
        ) : (
          <Button onClick={() => void runImport(false)} disabled={!canGenerate}>
            {generating ? (
              <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
            ) : (
              <Sparkles aria-hidden data-icon="inline-start" />
            )}
            {t(generating ? "source_import_generating" : "source_import_generate")}
          </Button>
        )}
        {generating && (
          <Button variant="outline" onClick={cancelGenerate}>{t("common:cancel")}</Button>
        )}
      </EpisodeHeaderActions>

      {/* 重新导入的风险提示放在顶部：从制作进度触发时视图会切到这里，提示必须首屏可见。 */}
      {confirming && !generating && (
        <Alert className="shrink-0">
          <AlertTriangle aria-hidden />
          <AlertTitle>{t("source_import_reimport_title")}</AlertTitle>
          <AlertDescription>
            <div className="flex flex-col gap-3">
              <p>{t("source_import_reimport_risk")}</p>
              {hasMediaArtifacts && <p>{t("source_import_reimport_risk_media")}</p>}
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="outline" onClick={() => setConfirming(false)}>
                  {t("common:cancel")}
                </Button>
                <Button
                  size="sm"
                  variant={hasMediaArtifacts ? "destructive" : "default"}
                  onClick={() => void runImport(true)}
                >
                  {t(hasMediaArtifacts ? "source_import_reimport_confirm_media" : "source_import_reimport_confirm")}
                </Button>
              </div>
            </div>
          </AlertDescription>
        </Alert>
      )}

      {loading ? (
        <div className="flex min-h-48 items-center justify-center text-muted-foreground" aria-busy="true">
          <Loader2 aria-hidden className="size-5 animate-spin" />
        </div>
      ) : (
        <>
          {loadError ? (
            <Alert variant="destructive">
              <AlertTriangle aria-hidden />
              <AlertTitle>{t("source_import_original_load_failed")}</AlertTitle>
              <AlertDescription>
                <div className="flex flex-col items-start gap-2">
                  <p>{loadError}</p>
                  <Button variant="outline" size="sm" onClick={() => setAttempt((value) => value + 1)}>
                    {t("common:retry")}
                  </Button>
                </div>
              </AlertDescription>
            </Alert>
          ) : (
            <div className="flex min-h-0 flex-1 flex-col gap-1.5">
              <label htmlFor={textId} className="text-sm font-medium text-foreground">
                {t("source_import_label")}
              </label>
              <Textarea
                ref={textareaRef}
                id={textId}
                value={text}
                onChange={(event) => {
                  setDraft(event.target.value);
                  setSavedNotice(false);
                }}
                placeholder={t("source_import_placeholder")}
                aria-describedby={countId}
                aria-invalid={tooLong}
                disabled={busy}
                fill
                className="min-h-40 max-h-none flex-1 resize-none"
              />
              <div id={countId} className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                <span className="num">{t("source_import_char_count", { current: text.length.toLocaleString(i18n.language), limit })}</span>
                {tooLong && (
                  <span className="text-destructive">{t("source_import_too_long", { limit })}</span>
                )}
              </div>
              {hasScript && dirty && (
                <p className="text-xs text-subtle-foreground">{t("source_import_original_dirty")}</p>
              )}
              {savedNotice && (
                <p role="status" className="text-xs text-subtle-foreground">{t("source_import_saved_original")}</p>
              )}
            </div>
          )}

          {error && (
            <Alert variant="destructive" className="shrink-0">
              <AlertTriangle aria-hidden />
              <AlertTitle>{t("source_import_failed")}</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
        </>
      )}
    </section>
  );
}
