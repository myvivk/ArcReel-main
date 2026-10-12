import { useCallback, useMemo } from "react";
import { useTranslation } from "react-i18next";

import { API } from "@/api";
import { prefillAssistant } from "@/components/shared/DraftStatus";
import { PartialSaveError, useEditUnit } from "@/components/shared/edit-unit/useEditUnit";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useProjectsStore } from "@/stores/projects-store";
import { isResourceBusy } from "@/stores/tasks-store";
import type { EpisodeMeta } from "@/types";
import type { DramaEpisodeScript, DramaScene, Utterance } from "@/types/script";
import { episodeAgentRef } from "@/utils/episode-display";

/**
 * 短剧工厂导入集的剧本视图（页签按来源显示为「剧本」）：**可编辑并落盘生效**。
 *
 * 这类集没有集原文（Markdown 直转正式脚本），创作者要看与要改的都是剧本本身，故这里直接
 * 编辑正式脚本的内容层：每幕的画面描述与台词/旁白。保存走 `updateScene` + 项目刷新，
 * 与分镜视图 `ShotDetail` 同一条落库路径——改完即生效，不做二次转换。
 *
 * 两件刻意不做的事：
 * - **不在这里改时长**：时长编辑在分镜视图里带档位校验，两处都能改会出现校验口径不一致；
 * - **不在这里批量补生成分镜图**：页头「补齐分镜图」是分镜/宫格视图的批量入口，更快更省，
 *   这里只提示前往。改了内容后已有产物会过期，正是那个批量入口要补的。
 *
 * 页头「交给 Agent」按钮按来源分叉：短剧工厂导入的集（`short_drama_meta` 在场）重做就是
 * 「重写 Markdown + 重新导入」，只有整份导入才会重切幕、重算时长、重录视觉层；其余集走
 * `patch_episode_script` 的局部修改，保留结构与已写的提示词。
 */
export function DramaScriptContentView({
  script,
  projectName,
  episode,
  scriptFile,
  episodes,
  readOnly,
  hasArtifacts,
}: {
  script: DramaEpisodeScript;
  projectName: string;
  episode: number;
  /** 本集剧本文件名，落库 PATCH 的目标。 */
  scriptFile: string;
  /** 集账本，用于给 Agent 的集指称。 */
  episodes: EpisodeMeta[];
  /** 演示项目：只读。 */
  readOnly: boolean;
  /** 本集已有分镜图/视频产物：改动会让它们过期。 */
  hasArtifacts: boolean;
}) {
  const { t } = useTranslation("dashboard");
  const episodeRef = episodeAgentRef(episodes, episode, t);

  // 短剧工厂导入的集（顶层带 short_drama_meta）：「重做」要达到整份重来的粒度——
  // 重写 Markdown 后重新导入。局部 patch 改不动幕数、时长、视觉层与资产，达不到重做的效果。
  const shortDrama = script.short_drama_meta != null;

  const handOffRedo = useCallback(() => {
    const stepsKey = shortDrama
      ? "drama_script_agent_redo_short_drama_steps"
      : "drama_script_agent_redo_steps";
    const artifactsKey = shortDrama
      ? "drama_script_agent_redo_short_drama_artifacts"
      : "drama_script_agent_redo_artifacts";
    prefillAssistant(
      [t("drama_script_agent_redo_prefill", { episodeRef }), t(stepsKey), hasArtifacts ? t(artifactsKey) : ""]
        .filter(Boolean)
        .join("\n"),
    );
  }, [t, episodeRef, hasArtifacts, shortDrama]);

  return (
    <div className="relative min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto w-full max-w-3xl px-6 py-8">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-lg font-semibold">{script.title || t("drama_script_title_fallback")}</h2>
          {!readOnly && (
            <Button variant="outline" size="sm" onClick={handOffRedo}>
              {t(shortDrama ? "drama_script_agent_redo_short_drama" : "drama_script_agent_redo")}
            </Button>
          )}
        </div>
        {hasArtifacts && (
          <p className="mt-2 rounded-md border border-warn/40 bg-warn/5 px-3 py-2 text-sm text-warn">
            {t("drama_script_stale_hint")}
          </p>
        )}
        {script.scenes.length === 0 ? (
          <p className="mt-4 text-sm text-muted-foreground">{t("drama_script_empty")}</p>
        ) : (
          script.scenes.map((scene) => (
            <SceneEditor
              key={scene.scene_id}
              scene={scene}
              projectName={projectName}
              scriptFile={scriptFile}
              readOnly={readOnly}
            />
          ))
        )}
      </div>
    </div>
  );
}

/** 一幕的可编辑内容：画面描述与发声序列。 */
interface SceneFields {
  scene_description: string;
  utterances: Utterance[];
}

function SceneEditor({
  scene,
  projectName,
  scriptFile,
  readOnly,
}: {
  scene: DramaScene;
  projectName: string;
  scriptFile: string;
  readOnly: boolean;
}) {
  const { t } = useTranslation("dashboard");
  const source = useMemo<SceneFields>(
    () => ({ scene_description: scene.scene_description ?? "", utterances: scene.utterances ?? [] }),
    [scene.scene_description, scene.utterances],
  );

  const submit = useCallback(
    async (value: SceneFields) => {
      await API.updateScene(projectName, scene.scene_id, scriptFile, {
        scene_description: value.scene_description,
        utterances: value.utterances,
      });
      // 已落盘但界面没刷新时不能当保存成功：后续生成会用旧剧本入队。
      const refreshed = await useProjectsStore.getState().refreshProject(projectName);
      if (refreshed !== "success") {
        throw new PartialSaveError(t("drama_script_saved_not_refreshed"), { saved: value });
      }
    },
    [projectName, scene.scene_id, scriptFile, t],
  );

  const unit = useEditUnit<SceneFields>({
    source,
    save: submit,
    leaveTitle: t("drama_script_leave_title", { id: scene.scene_id }),
  });
  const { value, setValue, dirty, status, error, save, discard } = unit;

  // 该幕正在生成分镜图/视频时不给改：改完会立刻让在途产物失配。
  const busy =
    isResourceBusy("storyboard", projectName, scene.scene_id) ||
    isResourceBusy("video", projectName, scene.scene_id);
  const disabled = readOnly || busy;

  const setDescription = (next: string) => setValue((prev) => ({ ...prev, scene_description: next }));
  const setUtterances = (next: Utterance[]) => setValue((prev) => ({ ...prev, utterances: next }));

  const patchLine = (index: number, patch: Partial<Utterance>) =>
    setUtterances(value.utterances.map((line, i) => (i === index ? ({ ...line, ...patch } as Utterance) : line)));
  const removeLine = (index: number) => setUtterances(value.utterances.filter((_, i) => i !== index));

  return (
    <article className="mt-6 rounded-lg border border-border p-4">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground">
        <span className="num font-medium text-foreground">{scene.scene_id}</span>
        <span className="num">{t("drama_script_scene_seconds", { seconds: scene.duration_seconds })}</span>
        {(scene.scenes ?? []).map((location) => (
          <span key={location}>{location}</span>
        ))}
        {(scene.characters_in_scene ?? []).length > 0 && <span>{scene.characters_in_scene.join("、")}</span>}
      </header>

      <label className="mt-3 block text-sm text-muted-foreground" htmlFor={`desc-${scene.scene_id}`}>
        {t("drama_script_description_label")}
      </label>
      <Textarea
        id={`desc-${scene.scene_id}`}
        className="mt-1"
        value={value.scene_description}
        onChange={(event) => setDescription(event.target.value)}
        disabled={disabled}
        rows={4}
      />

      <div className="mt-3 text-sm text-muted-foreground">{t("drama_script_utterances_label")}</div>
      <ul className="mt-1 space-y-2">
        {value.utterances.map((line, index) => (
          <li key={index} className="flex items-start gap-2">
            {line.kind === "dialogue" ? (
              <Input
                className="w-28 shrink-0"
                value={line.speaker}
                placeholder={t("drama_script_speaker_placeholder")}
                onChange={(event) => patchLine(index, { speaker: event.target.value })}
                disabled={disabled}
              />
            ) : (
              <span className="w-28 shrink-0 pt-2 text-sm text-muted-foreground">
                {t("drama_script_voiceover")}
              </span>
            )}
            <Textarea
              className="flex-1"
              value={line.text}
              placeholder={t("drama_script_text_placeholder")}
              onChange={(event) => patchLine(index, { text: event.target.value })}
              disabled={disabled}
              rows={2}
            />
            {!disabled && (
              <Button
                variant="ghost"
                size="sm"
                aria-label={t("drama_script_remove_line")}
                onClick={() => removeLine(index)}
              >
                {t("drama_script_remove_line")}
              </Button>
            )}
          </li>
        ))}
      </ul>
      {!disabled && (
        <div className="mt-2 flex gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() =>
              setUtterances([
                ...value.utterances,
                { kind: "dialogue", speaker: value.utterances.find((line) => line.kind === "dialogue")?.speaker ?? "", text: "" },
              ])
            }
          >
            {t("drama_script_add_dialogue")}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setUtterances([...value.utterances, { kind: "voiceover", text: "" }])}
          >
            {t("drama_script_add_voiceover")}
          </Button>
        </div>
      )}

      {!disabled && dirty && (
        <div className="mt-3 flex items-center gap-2">
          <Button size="sm" onClick={() => void save()} disabled={status === "saving"}>
            {status === "saving" ? t("drama_script_saving") : t("drama_script_save")}
          </Button>
          <Button variant="ghost" size="sm" onClick={discard}>
            {t("drama_script_discard")}
          </Button>
          {status === "saved" && <span className="text-sm text-good">{t("drama_script_saved")}</span>}
          {error && <span className="text-sm text-destructive">{error}</span>}
        </div>
      )}
      {disabled && readOnly === false && (
        <p className="mt-3 text-sm text-muted-foreground">{t("drama_script_busy_hint")}</p>
      )}
    </article>
  );
}
