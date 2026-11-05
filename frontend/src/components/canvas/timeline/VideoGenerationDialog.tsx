import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2, Sparkles, Upload } from "lucide-react";
import { API } from "@/api";
import { refreshAfterWrite } from "@/components/canvas/shared/refreshAfterWrite";
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useProjectsStore } from "@/stores/projects-store";
import { errMsg } from "@/utils/async";
import { formatCost } from "@/utils/cost-format";
import type { CostBreakdown, DramaScene, Utterance } from "@/types";
import { UtteranceListEditor } from "./UtteranceListEditor";

/** 视频弹窗里可改的内容：发声条目与生成配置；动态分镜提示词只读消费。 */
interface VideoGenerationDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projectName: string;
  segmentId: string;
  scriptFile: string;
  scene: DramaScene;
  characters: readonly string[];
  cost?: CostBreakdown;
  generating?: boolean;
  /** 弹窗内容保存到剧本；返回是否保存并刷新成功。 */
  onSavePatch: (patch: Record<string, unknown>) => Promise<boolean>;
  /** 保存成功后的入队回调；seed 只对本次请求生效。 */
  onGenerate: (seed: number | null) => Promise<void> | void;
}

/** 视频生成配置弹窗：把一个分镜会送进视频模型的内容与当前参数收在一处。 */
export function VideoGenerationDialog({
  open,
  onOpenChange,
  projectName,
  segmentId,
  scriptFile,
  scene,
  characters,
  cost,
  generating,
  onSavePatch,
  onGenerate,
}: VideoGenerationDialogProps) {
  const { t } = useTranslation("dashboard");
  const project = useProjectsStore((state) => state.currentProjectData);
  const projectRecord = useMemo(() => (project ?? {}) as Record<string, unknown>, [project]);
  const initialModel = typeof projectRecord.video_provider_r2v === "string" ? projectRecord.video_provider_r2v : "";
  const initialSettings = (projectRecord.model_settings ?? {}) as Record<string, { resolution?: string | null }>;
  const initialResolution = initialSettings[initialModel]?.resolution ?? "";
  const initialAudio = projectRecord.video_generate_audio === true ? true : projectRecord.video_generate_audio === false ? false : null;
  const initialBinding = projectRecord.character_voice_binding === "reference_audio" ? "reference_audio" : "prompt";

  const [utterances, setUtterances] = useState<Utterance[]>(() => scene.utterances ?? []);
  const [seed, setSeed] = useState("");
  const [model, setModel] = useState(initialModel);
  const [resolution, setResolution] = useState(initialResolution);
  const [audio, setAudio] = useState<boolean | null>(initialAudio);
  const [binding, setBinding] = useState(initialBinding);
  const [voiceStyles, setVoiceStyles] = useState<Record<string, string>>({});
  const [bgmName, setBgmName] = useState<string | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  // 候选视频模型：弹窗里能选的不只是「跟随全局」，用户知道这次要用哪个模型，
  // 提示词格式随模型走，选择权必须在用户手上。
  const [modelOptions, setModelOptions] = useState<{ value: string; label: string }[]>([]);
  const [globalDefault, setGlobalDefault] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    void Promise.all([API.getModelCandidates(), API.getSystemConfig()])
      .then(([candidates, config]) => {
        if (!alive) return;
        const pool = candidates.video.buckets.r2v ?? candidates.video.default;
        setModelOptions(
          pool.map((value) => ({ value, label: candidates.model_names[value] ?? value })),
        );
        setGlobalDefault(
          config.settings.default_video_backend_r2v || config.settings.default_video_backend || "",
        );
      })
      .catch(() => {
        // 候选取不到不影响生成：保留当前值，模型仍按项目已保存的配置走。
        if (alive) setModelOptions([]);
      });
    return () => {
      alive = false;
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    void API.previewScriptItemPrompts(projectName, segmentId, scriptFile, {
      videoBackend: model || undefined,
    })
      .then((result) => {
        if (!alive) return;
        setPreview(result.video.text ?? result.video.unavailable ?? null);
      })
      .catch(() => {
        if (alive) setPreview(null);
      });
    return () => {
      alive = false;
    };
  }, [open, projectName, segmentId, scriptFile, model]);

  const summary = useMemo(() => {
    const aspect = typeof projectRecord.aspect_ratio === "string" ? projectRecord.aspect_ratio : "16:9";
    return {
      aspect,
      modelLabel: model || (globalDefault ? `跟随全局（${globalDefault}）` : "跟随全局"),
      resolutionLabel: !model ? "选模型后设置" : resolution || "模型默认",
      audioLabel: audio === false ? "无声" : audio === true ? "有声" : "跟随全局",
    };
  }, [projectRecord, model, resolution, audio, globalDefault]);

  const characterRecords = useMemo(() => {
    const bucket = (project?.characters ?? {}) as Record<string, { voice_style?: string; reference_audio?: string }>;
    const speakers = Array.from(
      new Set(
        utterances
          .filter((utterance) => utterance.kind === "dialogue")
          .map((utterance) => (utterance.kind === "dialogue" ? utterance.speaker : ""))
          .filter(Boolean),
      ),
    );
    return speakers.map((speaker) => ({
      speaker,
      projectStyle: bucket[speaker]?.voice_style ?? "",
      hasAudio: Boolean(bucket[speaker]?.reference_audio?.trim()),
    }));
  }, [project, utterances]);

  const uploadVoiceAudio = async (speaker: string, file: File) => {
    setSaving(true);
    setError(null);
    try {
      await API.uploadFile(projectName, "character_audio_ref", file, speaker);
      await refreshAfterWrite(projectName, t);
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setSaving(false);
    }
  };

  const uploadBgm = async (file: File) => {
    setSaving(true);
    setError(null);
    try {
      const result = await API.uploadBgm(projectName, file);
      setBgmName(result.bgm.name);
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setSaving(false);
    }
  };

  const busy = saving || Boolean(generating);
  const invalid = utterances.some((u) => u.kind === "dialogue" && !u.speaker.trim());

  const confirm = async () => {
    if (busy || invalid) return;
    setSaving(true);
    setError(null);
    try {
      const projectUpdates: Record<string, unknown> = {};
      if (model !== initialModel) projectUpdates.video_provider_r2v = model || null;
      if (model && (resolution !== initialResolution || model !== initialModel)) {
        projectUpdates.model_settings = {
          ...initialSettings,
          [model]: { ...(initialSettings[model] ?? {}), resolution: resolution || null },
        };
      }
      if (audio !== initialAudio) projectUpdates.video_generate_audio = audio;
      if (binding !== initialBinding) projectUpdates.character_voice_binding = binding;
      if (Object.keys(projectUpdates).length) {
        await API.updateProject(projectName, projectUpdates);
        await refreshAfterWrite(projectName, t);
      }

      for (const item of characterRecords) {
        const style = voiceStyles[item.speaker];
        if (style === undefined || style === item.projectStyle) continue;
        await API.updateCharacter(projectName, item.speaker, { voice_style: style });
      }

      const saved = await onSavePatch({ utterances });
      if (!saved) throw new Error("保存失败，未提交生成");
      const parsedSeed = seed.trim() ? Number(seed) : null;
      if (parsedSeed !== null && (!Number.isInteger(parsedSeed) || !Number.isSafeInteger(parsedSeed))) {
        throw new Error("seed 必须是整数");
      }
      await onGenerate(parsedSeed);
      onOpenChange(false);
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!busy) onOpenChange(next);
      }}
    >
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>生成视频 · {segmentId}</DialogTitle>
          <DialogDescription>
            {scene.duration_seconds}s · {summary.modelLabel} · {summary.aspect} · {summary.audioLabel}
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <div className="flex flex-col gap-5">
            <section className="flex flex-col gap-2">
              <div className="flex items-center justify-between">
                <Label>台词与画外音</Label>
                <span className="text-xs text-muted-foreground">画外音不送视频，只进字幕/后期</span>
              </div>
              <UtteranceListEditor
                utterances={utterances}
                onChange={setUtterances}
                speakerCandidates={characters}
              />
            </section>

            <section className="flex flex-col gap-2">
              <div className="flex items-center justify-between">
                <Label>项目默认（影响全项目）</Label>
                <span className="text-xs text-muted-foreground">改模型/分辨率会改变实际价格</span>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="flex flex-col gap-1.5">
                  <Label>视频模型</Label>
                  <select
                    className="h-9 rounded-md border border-input bg-background px-2 text-sm"
                    value={model}
                    onChange={(event) => setModel(event.target.value)}
                  >
                    <option value="">
                      跟随全局{globalDefault ? `（${globalDefault}）` : ""}
                    </option>
                    {modelOptions.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                    {model && !modelOptions.some((option) => option.value === model) ? (
                      <option value={model}>{model}</option>
                    ) : null}
                  </select>
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label>分辨率</Label>
                  <Input
                    value={resolution}
                    onChange={(event) => setResolution(event.target.value)}
                    placeholder={summary.resolutionLabel}
                    disabled={!model}
                  />
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-5 text-sm">
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={audio !== false}
                    onChange={(event) => setAudio(event.target.checked)}
                  />
                  生成有声视频
                </label>
                <label className="flex items-center gap-2">
                  角色声音绑定
                  <select
                    className="h-8 rounded-md border border-input bg-background px-2 text-sm"
                    value={binding}
                    onChange={(event) => setBinding(event.target.value)}
                  >
                    <option value="prompt">提示词软约束</option>
                    <option value="reference_audio">参考音频</option>
                  </select>
                </label>
              </div>
            </section>

            <section className="flex flex-col gap-2">
              <Label>角色声音</Label>
              {characterRecords.length === 0 ? (
                <p className="text-xs text-muted-foreground">本分镜没有台词角色。</p>
              ) : (
                characterRecords.map((item) => (
                  <div key={item.speaker} className="flex items-center gap-2">
                    <span className="w-16 shrink-0 truncate text-sm">{item.speaker}</span>
                    <Input
                      value={voiceStyles[item.speaker] ?? item.projectStyle}
                      onChange={(event) =>
                        setVoiceStyles((prev) => ({ ...prev, [item.speaker]: event.target.value }))
                      }
                      placeholder="声音风格，如：低沉、缓慢、略沙哑"
                      className="flex-1"
                    />
                    <label
                      className="inline-flex cursor-pointer items-center gap-1 rounded-md border border-input px-2 py-1 text-xs"
                      title="上传参考音频"
                    >
                      <Upload aria-hidden className="size-3.5" />
                      {item.hasAudio ? "换音频" : "参考音频"}
                      <input
                        type="file"
                        accept=".wav,.mp3"
                        className="hidden"
                        onChange={(event) => {
                          const file = event.target.files?.[0];
                          if (file) void uploadVoiceAudio(item.speaker, file);
                          event.currentTarget.value = "";
                        }}
                      />
                    </label>
                  </div>
                ))
              )}
              <p className="text-xs text-muted-foreground">
                绑定选“参考音频”时才会把音频挂给视频模型；否则只把声音风格写进提示词。
              </p>
            </section>

            <section className="flex flex-col gap-1.5">
              <Label>BGM（后期剪辑使用，不随视频生成）</Label>
              <div className="flex items-center gap-3 text-sm">
                <label className="inline-flex cursor-pointer items-center gap-1 rounded-md border border-input px-2 py-1 text-xs">
                  <Upload aria-hidden className="size-3.5" />
                  上传 BGM
                  <input
                    type="file"
                    accept=".mp3,.wav,.m4a"
                    className="hidden"
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (file) void uploadBgm(file);
                      event.currentTarget.value = "";
                    }}
                  />
                </label>
                <span className="text-muted-foreground">{bgmName ?? "未选择"}</span>
              </div>
            </section>

            <section className="flex flex-col gap-2">
              <Label>帧模式</Label>
              <p className="text-sm">
                参考图模式
                <span className="block text-xs text-muted-foreground">
                  K0–K3 全部按参考图按序送；不发首/尾帧，保角色一致性
                </span>
              </p>
            </section>

            <section className="grid grid-cols-2 gap-3">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="video-seed">seed（可选）</Label>
                <Input
                  id="video-seed"
                  value={seed}
                  onChange={(event) => setSeed(event.target.value)}
                  placeholder="留空随机"
                  inputMode="numeric"
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label>时长</Label>
                <Input value={`${scene.duration_seconds}s`} readOnly />
              </div>
            </section>

            <details className="rounded-md border border-border p-3">
              <summary className="cursor-pointer text-sm">
                查看将按所选模型发送的提示词
              </summary>
              <pre className="relative mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-xs text-muted-foreground">
                {preview ?? "暂无预览"}
              </pre>
            </details>
          </div>
        </DialogBody>
        <DialogFooter>
          <div className="flex flex-1 items-center text-sm text-muted-foreground">
            {cost ? `预估 ${formatCost(cost)}` : null}
          </div>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>
            取消
          </Button>
          <Button onClick={() => void confirm()} disabled={busy || invalid}>
            {busy ? <Loader2 className="animate-spin" aria-hidden /> : <Sparkles aria-hidden />}
            保存并生成
          </Button>
        </DialogFooter>
        {error ? <p className="px-6 pb-4 text-sm text-destructive">{error}</p> : null}
      </DialogContent>
    </Dialog>
  );
}
