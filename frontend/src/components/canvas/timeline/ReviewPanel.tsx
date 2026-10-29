import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { API } from "@/api";
import type { ReviewResult } from "@/types/review";
import { ReviewBlock } from "./ReviewBlock";

interface ReviewPanelProps {
  kind: "storyboard" | "reference" | "video";
  projectName: string;
  episode?: number;
  shotId: string;
  revision: string;
}

/** 只读评测面板：总分常显，维度、缺陷与建议可折叠；不写项目、不入队。 */
export function ReviewPanel({ kind, projectName, episode, shotId, revision }: ReviewPanelProps) {
  const { t } = useTranslation("dashboard");
  const [result, setResult] = useState<ReviewResult | null>(null);
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
          if (kind === "storyboard") {
            const data = await API.getStoryboardScore(projectName, episode, { signal: controller.signal });
            if (controller.signal.aborted) return;
            setResult(data.scenes.find((scene) => scene.scene_id === shotId) ?? null);
          } else if (kind === "reference") {
            const data = await API.getReferenceReview(projectName, episode, shotId, { signal: controller.signal });
            if (controller.signal.aborted) return;
            setResult(data);
          } else {
            const data = await API.getVideoReview(projectName, episode, shotId, { signal: controller.signal });
            if (controller.signal.aborted) return;
            setResult(data);
          }
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
  }, [kind, projectName, episode, shotId, revision]);

  if (episode === undefined) return null;
  if (error) return <p className="text-xs text-subtle-foreground">{t("review_error")}</p>;
  if (loading && !result) return <p className="text-xs text-subtle-foreground">{t("review_loading")}</p>;
  if (!result || result.dimensions.length === 0) return <p className="text-xs text-subtle-foreground">{t("review_empty")}</p>;
  if (kind === "video" && (result as { status?: string }).status !== "ok") {
    return <p className="text-xs text-subtle-foreground">{t("review_video_not_ready")}</p>;
  }
  if (kind === "reference" && (result as { status?: string }).status !== "ok") {
    return <p className="text-xs text-subtle-foreground">{t("review_reference_not_ready")}</p>;
  }

  const tone = result.score >= 85 ? "text-media-text" : result.score >= 60 ? "text-media-audio" : "text-destructive";

  return (
    <ReviewBlock
      title={t(kind === "storyboard" ? "review_storyboard_title" : kind === "reference" ? "review_reference_title" : "review_video_title")}
      score={result.score}
      max={result.max}
      tone={tone}
    >
      {result.coverage < 1 ? (
        <p className="text-subtle-foreground">
          {t("review_coverage", { percent: Math.round(result.coverage * 100) })}
        </p>
      ) : null}
      <div className="grid gap-1">
        {result.dimensions.map((dimension) => (
          <div key={dimension.id} className="flex items-center justify-between gap-2">
            <span className="text-muted-foreground">
              {t(`review_dim_${dimension.id}`, { defaultValue: dimension.id })}
            </span>
            <span className="tabular-nums">
              {dimension.score === null ? t("review_untested") : `${dimension.score} / ${dimension.max}`}
            </span>
          </div>
        ))}
      </div>
      {result.defects.length > 0 ? (
        <div>
          <p className="text-media-audio">{t("review_defects", { count: result.defects.length })}</p>
          <ul className="mt-1 list-disc pl-4 text-muted-foreground">
            {result.defects.map((defect) => <li key={defect.id}>{defect.message}</li>)}
          </ul>
        </div>
      ) : null}
      {result.advice.length > 0 ? (
        <div className="text-subtle-foreground">
          <span>{t("review_advice")}</span>
          <ul className="list-disc pl-4">
            {result.advice.map((item) => <li key={item}>{item}</li>)}
          </ul>
        </div>
      ) : null}
    </ReviewBlock>
  );
}
