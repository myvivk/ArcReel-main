import { useTranslation } from "react-i18next";
import { FilePlus, Trash2 } from "lucide-react";
import { ShortDramaGenerateButton } from "@/components/canvas/shared/ShortDramaGenerateDialog";
import { ShortDramaImportButton } from "@/components/canvas/shared/ShortDramaImportDialog";
import { Button } from "@/components/ui/button";

export function EpisodesHeader({
  projectName, episodeCount, onCreate, onBatchDelete,
}: {
  projectName: string;
  episodeCount: number;
  onCreate: () => void;
  onBatchDelete: () => void;
}) {
  const { t } = useTranslation("dashboard");
  return (
    <header className="flex shrink-0 flex-wrap items-center gap-3 border-b px-4 py-2">
      <h2 className="text-base font-semibold">{t("workspace_nav_episodes")}</h2>
      <span className="num text-xs text-muted-foreground">{t("episodes_view_episode_count", { count: episodeCount })}</span>
      <span className="flex-1" />
      <ShortDramaGenerateButton projectName={projectName} episode={0} />
      <ShortDramaImportButton projectName={projectName} />
      <Button variant="ghost" size="sm" onClick={onCreate}>
        <FilePlus aria-hidden data-icon="inline-start" />{t("episode_create_title")}
      </Button>
      {episodeCount > 0 && (
        <Button variant="ghost" size="sm" onClick={onBatchDelete}>
          <Trash2 aria-hidden data-icon="inline-start" />{t("episodes_header_batch_delete")}
        </Button>
      )}
    </header>
  );
}
