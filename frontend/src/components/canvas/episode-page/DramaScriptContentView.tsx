import { useTranslation } from "react-i18next";

import type { DramaEpisodeScript, DramaScene, Utterance } from "@/types/script";

/**
 * 短剧工厂导入集的「原文」视图：只读呈现转写后的剧本内容。
 *
 * 这类集没有集原文（Markdown 直转正式脚本），原文编辑器打开是空的、毫无意义；
 * 创作者想看的"原文"就是剧本本身——每幕的场景、画面描述与台词，逐幕只读铺开。
 * 要改内容去分镜视图；要整集重做，让 Agent 重新导入或重跑脚本规划。
 */
export function DramaScriptContentView({ script }: { script: DramaEpisodeScript }) {
  const { t } = useTranslation("dashboard");
  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto w-full max-w-3xl px-6 py-8">
        <h2 className="text-lg font-semibold">{script.title || t("drama_script_title_fallback")}</h2>
        {script.scenes.length === 0 ? (
          <p className="mt-4 text-sm text-muted-foreground">{t("drama_script_empty")}</p>
        ) : (
          script.scenes.map((scene) => (
            <DramaSceneBlock key={scene.scene_id} scene={scene} />
          ))
        )}
      </div>
    </div>
  );
}

function DramaSceneBlock({ scene }: { scene: DramaScene }) {
  const { t } = useTranslation("dashboard");
  return (
    <article className="mt-6 rounded-lg border border-subtle p-4">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground">
        <span className="num font-medium text-foreground">{scene.scene_id}</span>
        <span className="num">{t("drama_script_scene_seconds", { seconds: scene.duration_seconds })}</span>
        {(scene.scenes ?? []).map((location) => (
          <span key={location}>{location}</span>
        ))}
        {(scene.characters_in_scene ?? []).length > 0 && (
          <span>{scene.characters_in_scene.join("、")}</span>
        )}
      </header>
      {scene.scene_description && (
        <p className="mt-3 whitespace-pre-wrap text-sm leading-relaxed">{scene.scene_description}</p>
      )}
      {(scene.utterances ?? []).length > 0 && (
        <ul className="mt-3 space-y-1.5">
          {scene.utterances!.map((utterance, index) => (
            <UtteranceLine key={index} utterance={utterance} />
          ))}
        </ul>
      )}
    </article>
  );
}

function UtteranceLine({ utterance }: { utterance: Utterance }) {
  const { t } = useTranslation("dashboard");
  return (
    <li className="text-sm leading-relaxed">
      {utterance.kind === "dialogue" ? (
        <>
          <span className="font-medium">{utterance.speaker}{t("drama_script_speaker_suffix")}</span>
          <span>{utterance.text}</span>
        </>
      ) : (
        <>
          <span className="text-muted-foreground">{t("drama_script_voiceover")}{t("drama_script_speaker_suffix")}</span>
          <span className="text-muted-foreground">{utterance.text}</span>
        </>
      )}
    </li>
  );
}
