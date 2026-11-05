import { useEffect, useId, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Bot, Loader2, Sparkles } from "lucide-react";
import { API, ApiRequestError } from "@/api";
import { enqueuePromptAuthoring, promptAuthoringResourceId } from "@/actions/generation";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogBody,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
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
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Textarea } from "@/components/ui/textarea";
import { ProviderModelSelect } from "@/components/shared/ProviderModelSelect";
import { refreshAfterWrite } from "@/components/canvas/shared/refreshAfterWrite";
import type { ModelCandidatesResponse } from "@/types/system";
import { useAppStore } from "@/stores/app-store";
import { useAssistantStore } from "@/stores/assistant-store";
import { useProjectsStore } from "@/stores/projects-store";
import {
  usePromptAuthoringStore,
  type PromptAuthoringOpenRequest,
  type PromptAuthoringScope,
} from "@/stores/prompt-authoring-store";
import { isResourceBusy } from "@/stores/tasks-store";
import type { PromptOverwrite } from "@/types";
import { errMsg } from "@/utils/async";
import { useEpisodeLedger } from "@/hooks/useEpisodeLedger";
import { episodeAgentRef, itemIdsInEpisodeText } from "@/utils/episode-display";
import { promptAuthoringEntries, type PromptAuthoringEntry } from "./prompt-authoring-entries";
import { promptAuthoringHandoffText } from "./prompt-authoring-handoff";

function readPromptOverwrite(err: unknown): PromptOverwrite | null {
  if (!(err instanceof ApiRequestError) || err.status !== 409) return null;
  const diagnostic = err.diagnostic as { prompt_overwrite?: PromptOverwrite } | undefined;
  return diagnostic?.prompt_overwrite ?? null;
}

interface HostProps {
  projectName: string;
  episode: number;
  /** 本集正式脚本；尚无时不渲染。 */
  script: unknown;
  /** 本集上次保存的附加指令。 */
  savedInstructions?: string;
}

/** 集页上唯一的「编写提示词」弹窗宿主：按 {@link usePromptAuthoringStore} 的请求打开。 */
export function PromptAuthoringHost({ projectName, episode, script, savedInstructions }: HostProps) {
  const request = usePromptAuthoringStore((s) => s.request);
  const close = usePromptAuthoringStore((s) => s.close);
  useEffect(() => close, [close, projectName, episode]);
  const entries = useMemo(() => promptAuthoringEntries(script), [script]);
  if (!request || request.projectName !== projectName || request.episode !== episode || !script) return null;
  return (
    <PromptAuthoringDialog
      // 每次打开都从请求重新初始化范围与指令，不沿用上一次弹窗里未提交的状态。
      key={`${projectName}:${episode}:${request.scope}:${request.currentEntryId ?? ""}:${request.defaultRewrite ? "rewrite" : "fill"}:${(request.defaultEntryIds ?? []).join(",")}`}
      projectName={projectName}
      episode={episode}
      entries={entries}
      request={request}
      savedInstructions={savedInstructions ?? ""}
      onClose={close}
    />
  );
}

interface DialogProps {
  projectName: string;
  episode: number;
  entries: PromptAuthoringEntry[];
  request: PromptAuthoringOpenRequest;
  savedInstructions: string;
  onClose: () => void;
}

export function PromptAuthoringDialog({
  projectName,
  episode,
  entries,
  request,
  savedInstructions,
  onClose,
}: DialogProps) {
  const { t } = useTranslation("dashboard");
  const episodeLedger = useEpisodeLedger();
  const scopeLabelId = useId();
  const rewriteId = useId();
  const fieldId = useId();
  const pendingCount = entries.filter((entry) => entry.pending).length;
  const currentEntryId =
    request.currentEntryId && entries.some((entry) => entry.id === request.currentEntryId)
      ? request.currentEntryId
      : null;
  const initialScope: PromptAuthoringScope =
    request.scope === "current" && !currentEntryId ? "pending" : request.scope;

  const [scope, setScope] = useState<PromptAuthoringScope>(initialScope);
  const [selected, setSelected] = useState<string[]>(() => {
    if (currentEntryId) return [currentEntryId];
    if (request.defaultEntryIds?.length) return request.defaultEntryIds.filter((id) => entries.some((entry) => entry.id === id));
    return entries.filter((entry) => entry.pending).map((entry) => entry.id);
  });
  const [rewrite, setRewrite] = useState(request.defaultRewrite === true);
  const currentOnly = request.scope === "current" && currentEntryId !== null;
  const [instructions, setInstructions] = useState(savedInstructions);
  const [submitting, setSubmitting] = useState(false);
  const [overwrite, setOverwrite] = useState<PromptOverwrite | null>(null);
  const project = useProjectsStore((state) => state.currentProjectData);
  const projectRecord = (project ?? {}) as Record<string, unknown>;
  const [videoModel, setVideoModel] = useState(
    typeof projectRecord.video_provider_r2v === "string" ? projectRecord.video_provider_r2v : "",
  );
  // 项目没选模型时的生效模型：项目默认 → 全局默认。提示词要按模型方言写，弹窗必须知道
  // 会跑哪个模型，所以「跟随默认」要解析成一个具体模型并写进项目。
  const [globalVideoModel, setGlobalVideoModel] = useState("");
  const effectiveVideoModel =
    videoModel ||
    (typeof projectRecord.video_backend === "string" ? projectRecord.video_backend : "") ||
    globalVideoModel;
  // 候选来自供应商能力目录（include_unconfigured）：允许先按目标模型写提示词、再配密钥。
  // 视频固定走参考图模式，故只列 r2v 桶。
  const [videoCandidates, setVideoCandidates] = useState<ModelCandidatesResponse | null>(null);
  useEffect(() => {
    let alive = true;
    void Promise.all([API.getModelCandidates({ includeUnconfigured: true }), API.getSystemConfig()])
      .then(([result, config]) => {
        if (!alive) return;
        setVideoCandidates(result);
        setGlobalVideoModel(config.settings.default_video_backend_r2v || config.settings.default_video_backend || "");
      })
      .catch(() => {
        if (alive) setVideoCandidates(null);
      });
    return () => {
      alive = false;
    };
  }, []);
  const videoModelOptions = useMemo(
    () => videoCandidates?.video.buckets.r2v ?? videoCandidates?.video.default ?? [],
    [videoCandidates],
  );

  const changeVideoModel = async (next: string) => {
    setVideoModel(next);
    try {
      // 「跟随默认」不是一个模型：写作侧按项目配置取提示词方言，必须落成具体模型。
      await API.updateProject(projectName, { video_provider_r2v: next || globalVideoModel || null });
      await refreshAfterWrite(projectName, t);
    } catch (err) {
      useAppStore.getState().pushToast(errMsg(err), "error");
    }
  };

  /** 提交前把「跟随默认」落成具体模型：没落盘的话，写作任务取不到方言。 */
  const persistEffectiveVideoModel = async () => {
    if (!effectiveVideoModel) return;
    if (projectRecord.video_provider_r2v === effectiveVideoModel) return;
    await API.updateProject(projectName, { video_provider_r2v: effectiveVideoModel });
    await refreshAfterWrite(projectName, t);
  };

  // 脚本在弹窗打开期间变化（条目被删）时，自选范围只算仍在脚本里的条目，按剧本顺序。
  const selectedIds = useMemo(
    () => entries.filter((entry) => selected.includes(entry.id)).map((entry) => entry.id),
    [entries, selected],
  );

  const entryIds: string[] | null =
    scope === "current" ? (currentEntryId ? [currentEntryId] : []) : scope === "custom" ? selectedIds : null;
  const scopeEmpty = entryIds === null ? pendingCount === 0 : entryIds.length === 0;
  const scopeLabel =
    scope === "pending"
      ? t("prompt_authoring_scope_pending_prefill", { count: pendingCount })
      : (entryIds ?? []).join(t("prompt_authoring_id_separator"));

  const guardBusy = (): boolean => {
    if (isResourceBusy("text_episode_script", projectName, promptAuthoringResourceId(episode))) {
      useAppStore.getState().pushToast(t("prompt_authoring_busy"), "error");
      return true;
    }
    return false;
  };

  const submit = async (overwriteRevision: string | null) => {
    if (submitting || scopeEmpty || guardBusy()) return;
    setSubmitting(true);
    try {
      await persistEffectiveVideoModel();
      await enqueuePromptAuthoring(projectName, episode, {
        entry_ids: entryIds,
        rewrite,
        fields: ["image_prompt"],
        instructions: instructions.trim() || null,
        overwrite_revision: overwriteRevision,
      });
      setOverwrite(null);
      onClose();
    } catch (err) {
      const loss = readPromptOverwrite(err);
      if (loss) {
        setOverwrite(loss);
      } else {
        setOverwrite(null);
        useAppStore.getState().pushToast(errMsg(err), "error");
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handOff = async () => {
    if (submitting || scopeEmpty) return;
    setSubmitting(true);
    try {
      await persistEffectiveVideoModel();
      await API.savePromptAuthoringInstructions(projectName, episode, instructions.trim());
    } catch (err) {
      useAppStore.getState().pushToast(errMsg(err), "error");
      setSubmitting(false);
      return;
    }
    useAssistantStore.getState().setInput(
      promptAuthoringHandoffText(t, {
        episodeRef: episodeAgentRef(episodeLedger, episode, t),
        scopeLabel,
        rewrite,
        instructions,
      }),
    );
    useAppStore.getState().setAssistantPanelOpen(true);
    setSubmitting(false);
    onClose();
  };

  const toggle = (id: string) =>
    setSelected((current) =>
      current.includes(id) ? current.filter((value) => value !== id) : [...current, id],
    );

  const scopes: { value: PromptAuthoringScope; label: string; disabled: boolean }[] = [
    { value: "current", label: t("prompt_authoring_scope_current"), disabled: !currentEntryId },
    {
      value: "pending",
      label: t("prompt_authoring_scope_pending", { count: pendingCount }),
      disabled: pendingCount === 0,
    },
    { value: "custom", label: t("prompt_authoring_scope_custom"), disabled: entries.length === 0 },
  ];

  return (
    <>
      <Dialog
        open={overwrite === null}
        onOpenChange={(next) => {
          if (!next && !submitting) onClose();
        }}
      >
        <DialogContent showCloseButton={!submitting}>
          <DialogHeader>
            <DialogTitle>{t("prompt_authoring_title")}</DialogTitle>
            <DialogDescription>{t("prompt_authoring_desc")}</DialogDescription>
          </DialogHeader>
          <DialogBody>
            <div className="flex flex-col gap-4">
              <div className="flex flex-col gap-2">
                <span id={scopeLabelId} className="text-sm font-medium">
                  {t("prompt_authoring_scope_label")}
                </span>
                {currentOnly ? (
                  <p className="text-sm text-muted-foreground">{scopeLabel}</p>
                ) : (
                  <RadioGroup
                    aria-labelledby={scopeLabelId}
                    value={scope}
                    onValueChange={(next) => setScope(next as PromptAuthoringScope)}
                    disabled={submitting}
                  >
                    {scopes.map((option) => (
                      <Label key={option.value}>
                        <RadioGroupItem value={option.value} disabled={option.disabled} />
                        {option.label}
                      </Label>
                    ))}
                  </RadioGroup>
                )}
                {scope === "custom" && (
                  <ul
                    className="relative flex max-h-48 flex-col gap-0.5 overflow-y-auto rounded-lg border border-border p-1.5"
                    aria-label={t("prompt_authoring_scope_custom")}
                  >
                    {entries.map((entry) => (
                      <li key={entry.id} className="rounded-sm px-1.5 py-1 hover:bg-muted/50">
                        <Label className="w-full">
                          <Checkbox
                            checked={selected.includes(entry.id)}
                            disabled={submitting}
                            onCheckedChange={() => toggle(entry.id)}
                          />
                          <span className="font-mono text-foreground">{entry.id}</span>
                          <span className="ml-auto text-xs text-muted-foreground">
                            {entry.pending
                              ? t("prompt_authoring_entry_pending")
                              : entry.hasContent
                                ? t("prompt_authoring_entry_has_prompt")
                                : t("prompt_authoring_entry_empty")}
                          </span>
                        </Label>
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              <div className="flex items-start gap-2">
                <Checkbox
                  id={rewriteId}
                  className="mt-0.5"
                  checked={rewrite}
                  disabled={submitting}
                  onCheckedChange={(checked) => setRewrite(checked)}
                  aria-describedby={`${rewriteId}-hint`}
                />
                <div className="flex flex-col gap-0.5">
                  <Label htmlFor={rewriteId}>{t("prompt_authoring_rewrite_toggle")}</Label>
                  <p id={`${rewriteId}-hint`} className="text-xs text-muted-foreground">
                    {t(rewrite ? "prompt_authoring_rewrite_hint" : "prompt_authoring_fill_hint")}
                  </p>
                </div>
              </div>

              <div className="flex flex-col gap-1.5">
                <Label>{t("prompt_authoring_video_model")}</Label>
                <ProviderModelSelect
                  value={videoModel}
                  options={videoModelOptions}
                  providerNames={videoCandidates?.provider_names ?? {}}
                  modelNames={videoCandidates?.model_names}
                  onChange={(next) => void changeVideoModel(next)}
                  allowDefault
                  defaultLabel={t("prompt_authoring_video_model_default")}
                  fallbackValue={effectiveVideoModel || undefined}
                  fallbackLabel={t("prompt_authoring_video_model_default")}
                  placeholder={t("prompt_authoring_video_model_placeholder")}
                  aria-label={t("prompt_authoring_video_model")}
                />
                <p className="text-xs text-muted-foreground">
                  {t("prompt_authoring_video_model_hint")}
                </p>
                {videoCandidates && videoModelOptions.length === 0 ? (
                  <p className="text-xs text-warn">{t("no_video_providers_hint")}</p>
                ) : null}
              </div>

              <div className="flex flex-col gap-1.5">
                <Label htmlFor={fieldId}>{t("prompt_authoring_instructions_label")}</Label>
                <Textarea
                  id={fieldId}
                  value={instructions}
                  onChange={(event) => setInstructions(event.target.value)}
                  maxLength={4000}
                  placeholder={t("prompt_authoring_instructions_placeholder")}
                  disabled={submitting}
                />
              </div>
            </div>
          </DialogBody>
          <DialogFooter>
            <Button variant="outline" onClick={() => void submit(null)} disabled={submitting || scopeEmpty}>
              <Sparkles aria-hidden data-icon="inline-start" />
              {rewrite ? t("prompt_authoring_ai_rewrite") : t("prompt_authoring_ai_write")}
            </Button>
            <Button onClick={() => void handOff()} disabled={submitting || scopeEmpty}>
              <Bot aria-hidden data-icon="inline-start" />
              {t("prompt_authoring_hand_to_agent")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={overwrite !== null}
        onOpenChange={(next) => {
          // 提交中不响应 Esc，避免请求还在途时对话框先消失
          if (!next && !submitting) setOverwrite(null);
        }}
      >
        <AlertDialogContent size="lg">
          <AlertDialogHeader>
            <AlertDialogTitle>{t("prompt_authoring_overwrite_title")}</AlertDialogTitle>
          </AlertDialogHeader>
          <AlertDialogBody tabIndex={0} role="region" aria-label={t("prompt_authoring_overwrite_title")}>
            <AlertDialogDescription>
              <span className="whitespace-pre-line wrap-break-word">
                {overwrite ? itemIdsInEpisodeText(overwrite.text) : null}
              </span>
            </AlertDialogDescription>
          </AlertDialogBody>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={submitting}>{t("common:cancel")}</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={submitting}
              onClick={() => void submit(overwrite?.revision ?? null)}
            >
              {submitting ? <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" /> : null}
              {t("prompt_authoring_ai_rewrite")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
