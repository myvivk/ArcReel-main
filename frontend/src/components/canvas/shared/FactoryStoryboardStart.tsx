import { useTranslation } from "react-i18next";
import { EPISODE_VIEW_PARAM, EPISODE_VIEW_SOURCE, WORKSPACE_ROUTE_EPISODES } from "@/app-routes";
import { ShortDramaStoryboardButton } from "@/components/canvas/shared/ShortDramaStoryboardButton";
import { Link } from "wouter";

interface Props {
  projectName: string;
  episode: number;
  className?: string;
}

/**
 * 工厂集还没有分镜时的空态：告诉用户分镜可以从工厂剧本生成，
 * 取代原先误导的「请在左侧选择剧集」。
 */
export function FactoryStoryboardStart({ projectName, episode, className }: Props) {
  const { t } = useTranslation("dashboard");
  const sourceQuery = new URLSearchParams({ [EPISODE_VIEW_PARAM]: EPISODE_VIEW_SOURCE });
  return (
    <div
      className={`flex h-full flex-col items-center justify-center gap-3 px-6 text-center ${className ?? ""}`}
    >
      <p className="text-sm text-foreground">{t("short_drama_storyboard_empty")}</p>
      <p className="max-w-md text-xs text-muted-foreground">
        {t("short_drama_storyboard_empty_hint")}
      </p>
      <div className="mt-1 flex flex-wrap items-center justify-center gap-3">
        <ShortDramaStoryboardButton
          projectName={projectName}
          episode={episode}
          storyboardCount={0}
          sceneIds={[]}
          prominent
        />
        <Link
          href={`/${WORKSPACE_ROUTE_EPISODES}/${episode}?${sourceQuery.toString()}`}
          className="focus-ring rounded-sm text-sm text-subtle-foreground underline underline-offset-4 hover:text-foreground"
        >
          {t("short_drama_storyboard_view_script")}
        </Link>
      </div>
    </div>
  );
}
