import { useEffect, useState } from "react";
import { Link, useLocation, useSearch } from "wouter";
import { useTranslation } from "react-i18next";
import { MoreHorizontal, Plus, Search, Trash2 } from "lucide-react";

import { WORKSPACE_ROUTE_EPISODES } from "@/app-routes";
import { ShortDramaGenerateButton, ShortDramaGenerateHost } from "@/components/canvas/shared/ShortDramaGenerateDialog";
import { SourceImportEntry } from "@/components/canvas/episode-page/SourceImportEntry";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { useProjectsStore } from "@/stores/projects-store";
import { isSourceImport } from "@/utils/creation-mode";
import { episodeDisplayName } from "@/utils/episode-display";

import { BatchDeleteEpisodesDialog } from "./BatchDeleteEpisodesDialog";
import { CreateEpisodeDialog } from "./CreateEpisodeDialog";
import { EpisodesHeader } from "./EpisodesHeader";
import { episodesViewPath } from "./episodes-view-model";
import { useDeleteEpisode } from "./useDeleteEpisode";
import { useDeleteEpisodes } from "./useDeleteEpisodes";

/** 工厂集账本：管理集目录，正文由工厂生成或工件入库提供。 */
export function EpisodesView({ projectName }: { projectName: string }) {
  const { t } = useTranslation(["dashboard", "common"]);
  const project = useProjectsStore((s) => s.currentProjectData);
  const episodes = project?.episodes ?? [];
  // 导入原文项目的剧本来自用户原文：空态引导到剧本页导入，不提供 AI 原创剧本入口。
  const sourceImport = isSourceImport(project);
  const search = useSearch();
  const [, setLocation] = useLocation();
  const [query, setQuery] = useState("");
  const [createAfter, setCreateAfter] = useState<number | null | undefined>(() =>
    new URLSearchParams(search).has("create") ? null : undefined,
  );
  const [previousSearch, setPreviousSearch] = useState(search);
  const [batchOpen, setBatchOpen] = useState(false);
  const deletion = useDeleteEpisode(projectName);
  const batchDeletion = useDeleteEpisodes(projectName);

  if (search !== previousSearch) {
    setPreviousSearch(search);
    if (new URLSearchParams(search).has("create")) setCreateAfter(null);
  }

  useEffect(() => {
    const params = new URLSearchParams(search);
    if (params.has("create")) {
      setLocation(episodesViewPath(), { replace: true });
    } else {
      const episode = Number(params.get("episode"));
      if (Number.isInteger(episode) && episode > 0) {
        setLocation(`/${WORKSPACE_ROUTE_EPISODES}/${episode}`, { replace: true });
      }
    }
  }, [search, setLocation]);

  const filtered = episodes.filter((episode) =>
    episodeDisplayName(episodes, episode.episode, t).toLowerCase().includes(query.trim().toLowerCase()),
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <EpisodesHeader
        projectName={projectName}
        episodeCount={episodes.length}
        sourceImport={sourceImport}
        onCreate={() => setCreateAfter(null)}
        onBatchDelete={() => setBatchOpen(true)}
      />
      <div className="relative min-h-0 flex-1 overflow-y-auto p-6">
        <div className="mx-auto flex max-w-4xl flex-col gap-4">
          <div className="flex items-center gap-2">
            <Search aria-hidden className="size-4 text-muted-foreground" />
            <Input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              aria-label={t("dashboard:episode_search_placeholder")}
              placeholder={t("dashboard:episode_search_placeholder")}
            />
          </div>
          {episodes.length === 0 ? (
            <div className="flex flex-col items-center gap-4 py-12 text-center">
              <p className="text-sm text-muted-foreground">{t("dashboard:no_episodes_yet")}</p>
              <div className="flex flex-wrap justify-center gap-2">
                {sourceImport ? (
                  <SourceImportEntry projectName={projectName} episode={null} />
                ) : (
                  <ShortDramaGenerateButton projectName={projectName} episode={0} />
                )}
              </div>
            </div>
          ) : (
            <ul className="flex flex-col divide-y rounded-lg border">
              {filtered.map((episode) => (
                <li key={episode.episode} className="flex items-center gap-3 p-3">
                  <Link
                    href={`/${WORKSPACE_ROUTE_EPISODES}/${episode.episode}`}
                    className="focus-ring min-w-0 flex-1 rounded-sm text-sm font-medium"
                  >
                    {episodeDisplayName(episodes, episode.episode, t)}
                  </Link>
                  <DropdownMenu>
                    <DropdownMenuTrigger render={
                      <Button variant="ghost" size="icon-sm" aria-label={t("dashboard:episode_menu_label", {
                        name: episodeDisplayName(episodes, episode.episode, t),
                      })} />
                    }>
                      <MoreHorizontal aria-hidden />
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem onClick={() => setCreateAfter(episode.episode)}>
                        <Plus aria-hidden />{t("dashboard:episodes_outline_create_after")}
                      </DropdownMenuItem>
                      <DropdownMenuItem variant="destructive" onClick={() => void deletion.requestDelete(episode.episode)}>
                        <Trash2 aria-hidden />{t("dashboard:episode_menu_delete")}
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </li>
              ))}
              {filtered.length === 0 && (
                <li className="p-6 text-center text-sm text-muted-foreground">{t("dashboard:no_episode_search_results")}</li>
              )}
            </ul>
          )}
        </div>
      </div>
      {createAfter !== undefined && (
        <CreateEpisodeDialog
          projectName={projectName}
          initialAfter={createAfter}
          onClose={() => setCreateAfter(undefined)}
          onCreated={() => setCreateAfter(undefined)}
        />
      )}
      {batchOpen && (
        <BatchDeleteEpisodesDialog
          episodes={episodes}
          busy={false}
          onSubmit={(selected) => {
            setBatchOpen(false);
            void batchDeletion.requestDelete(selected);
          }}
          onClose={() => setBatchOpen(false)}
        />
      )}
      {deletion.dialog}
      {batchDeletion.dialog}
      <ShortDramaGenerateHost projectName={projectName} episode={0} />
    </div>
  );
}
