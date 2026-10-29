import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { API } from "@/api";
import type { VideoPromptScoreRow } from "@/types/prompt-score";
import { ReviewBlock } from "./ReviewBlock";

interface VideoPromptScorePanelProps {
  projectName: string;
  episode?: number;
  shotId: string;
  revision: string;
}

/**
 * 只读视频提示词评分：总分常显，增量指标与提示可折叠。
 * 不写项目、不入队；评分失败只降级为一行提示，不影响编辑。
 */
export function VideoPromptScorePanel({ projectName, episode, shotId, revision }: VideoPromptScorePanelProps) {
  const { t } = useTranslation("dashboard");
  const [row, setRow] = useState<VideoPromptScoreRow | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (episode === undefined) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void (async () => {
        setLoading(true);
        setError(false);
        try {
          const data = await API.getVideoPromptScore(projectName, episode, { signal: controller.signal });
          if (controller.signal.aborted) return;
          setRow(data.scenes.find((scene) => scene.scene_id === shotId) ?? null);
        } catch {
          if (controller.signal.aborted) return;
          setError(true);
        } finally {
          if (!controller.signal.aborted) setLoading(false);
        }
      })();
    }, 600);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [projectName, episode, shotId, revision]);

  if (episode === undefined) return null;
  if (error) return <p className="text-xs text-subtle-foreground">{t("video_prompt_score_error")}</p>;
  if (loading && !row) return <p className="text-xs text-subtle-foreground">{t("video_prompt_score_loading")}</p>;
  if (!row || row.action_chars === 0) return <p className="text-xs text-subtle-foreground">{t("video_prompt_score_empty")}</p>;

  const tone = row.score >= 90 ? "text-media-text" : row.score >= 70 ? "text-media-audio" : "text-destructive";

  return (
    <ReviewBlock title={t("video_prompt_score_title")} score={row.score} max={100} tone={tone}>
      <p className="text-muted-foreground">
        {t("video_prompt_score_metrics", {
          similarity: row.max_similarity.toFixed(2),
          run: row.max_verbatim_run,
          density: Math.round(row.static_density * 100),
        })}
      </p>
      {!row.passed ? <p className="text-destructive">{t("video_prompt_score_problem")}</p> : null}
      {row.seam_problem ? <p className="text-destructive">{t("video_prompt_score_seam")}</p> : null}
      {[...row.mention_problems, ...row.mention_warnings].length > 0 ? (
        <p className="text-warn">
          <span className="text-subtle-foreground">{t("video_prompt_score_mentions")}</span>
          {[...row.mention_problems, ...row.mention_warnings].slice(0, 2).join("；")}
        </p>
      ) : null}
      {row.warnings.length > 0 ? (
        <p className="text-media-audio">{t("video_prompt_score_warnings", { count: row.warnings.length })}</p>
      ) : (
        <p className="text-media-text">{t("video_prompt_score_clean")}</p>
      )}
      <p className="text-subtle-foreground">{t("video_prompt_score_advice")}</p>
    </ReviewBlock>
  );
}
