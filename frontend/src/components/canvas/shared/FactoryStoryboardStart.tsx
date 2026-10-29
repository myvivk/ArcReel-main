import { useTranslation } from "react-i18next";
import { EpisodeHeaderActions } from "@/components/canvas/episode-page/EpisodeHeaderActions";
import { ShortDramaStoryboardButton } from "@/components/canvas/shared/ShortDramaStoryboardButton";

interface Props {
  projectName: string;
  episode: number;
  className?: string;
}

/**
 * 工厂集还没有分镜时的空态：告诉用户分镜可以从工厂剧本生成，
 * 取代原先误导的「请在左侧选择剧集」。生成入口放在页头右上角，正文只留说明。
 */
export function FactoryStoryboardStart({ projectName, episode, className }: Props) {
  const { t } = useTranslation("dashboard");
  return (
    <>
      <EpisodeHeaderActions target="script">
        <ShortDramaStoryboardButton
          projectName={projectName}
          episode={episode}
          storyboardCount={0}
          sceneIds={[]}
          prominent
          agentOnly
        />
      </EpisodeHeaderActions>
      <div
        className={`flex h-full flex-col items-center justify-center gap-3 px-6 text-center ${className ?? ""}`}
      >
        <p className="text-sm text-foreground">{t("short_drama_storyboard_empty")}</p>
        <p className="max-w-md text-xs text-muted-foreground">
          {t("short_drama_storyboard_empty_hint")}
        </p>
      </div>
    </>
  );
}
