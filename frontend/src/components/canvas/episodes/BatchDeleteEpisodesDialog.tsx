import { useId, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2, Search, Trash2 } from "lucide-react";

import { TruncatedText } from "@/components/shared/TruncatedText";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogBody,
  DialogClose,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import type { EpisodeMeta } from "@/types";
import { episodeDisplayName } from "@/utils/episode-display";

interface BatchDeleteEpisodesDialogProps {
  /** 项目里的全部集，供勾选。 */
  episodes: EpisodeMeta[];
  /** 删除请求在途，禁用操作。 */
  busy: boolean;
  /** 确认勾选并提交。 */
  onSubmit: (episodes: number[]) => void;
  /** 关闭对话框。 */
  onClose: () => void;
}

/**
 * 批量删除分集：列出全部集供勾选，确认后交给调用方走两阶段删除确认。挂载即打开。
 */
export function BatchDeleteEpisodesDialog({ episodes, busy, onSubmit, onClose }: BatchDeleteEpisodesDialogProps) {
  const { t } = useTranslation(["dashboard", "common"]);
  const searchId = useId();
  const selectAllId = useId();
  const [selected, setSelected] = useState<Set<number>>(() => new Set());
  const [query, setQuery] = useState("");

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (q === "") return episodes;
    return episodes.filter((episode) => episodeDisplayName(episodes, episode.episode, t).toLowerCase().includes(q));
  }, [episodes, query, t]);

  const allSelected = episodes.length > 0 && selected.size === episodes.length;
  const someSelected = selected.size > 0 && !allSelected;

  const toggle = (episode: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(episode)) next.delete(episode);
      else next.add(episode);
      return next;
    });

  const toggleAll = () =>
    setSelected(() => (allSelected ? new Set() : new Set(episodes.map((episode) => episode.episode))));

  const submit = () => {
    if (selected.size === 0 || busy) return;
    onSubmit([...selected].sort((a, b) => a - b));
  };

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next && !busy) onClose();
      }}
    >
      <DialogContent showCloseButton={!busy} className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t("dashboard:episode_batch_delete_title", { count: episodes.length })}</DialogTitle>
        </DialogHeader>
        <DialogBody>
          <div className="flex min-h-0 flex-col gap-3">
            <p className="text-sm text-muted-foreground">{t("dashboard:episode_batch_delete_hint")}</p>
            <div className="relative flex items-center gap-2">
              <Search aria-hidden className="size-3.5 shrink-0 text-muted-foreground" />
              <Input
                id={searchId}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={t("dashboard:episode_batch_delete_search")}
                className="flex-1"
                disabled={busy}
              />
            </div>
            <label htmlFor={selectAllId} className="flex items-center gap-2 text-sm font-medium">
              <Checkbox
                id={selectAllId}
                checked={allSelected}
                indeterminate={someSelected}
                disabled={busy || episodes.length === 0}
                onCheckedChange={toggleAll}
              />
              {t("dashboard:episode_batch_delete_select_all")}
              <span className="num text-xs text-muted-foreground">
                {t("dashboard:episode_batch_delete_selected", { count: selected.size, total: episodes.length })}
              </span>
            </label>
            <ul className="relative min-h-0 flex-1 overflow-y-auto rounded-md border">
              {filtered.length === 0 ? (
                <li className="px-3 py-6 text-center text-sm text-muted-foreground">
                  {t("dashboard:episode_batch_delete_no_match")}
                </li>
              ) : (
                filtered.map((episode) => {
                  const id = episode.episode;
                  const checked = selected.has(id);
                  const rowId = `batch-episode-${id}`;
                  return (
                    <li key={id}>
                      <label
                        htmlFor={rowId}
                        className="flex cursor-pointer items-center gap-2 px-3 py-1.5 text-sm hover:bg-muted/50"
                      >
                        <Checkbox id={rowId} checked={checked} disabled={busy} onCheckedChange={() => toggle(id)} />
                        <TruncatedText text={episodeDisplayName(episodes, id, t)} focusable={false} className="flex-1" />
                      </label>
                    </li>
                  );
                })
              )}
            </ul>
          </div>
        </DialogBody>
        <DialogFooter>
          <DialogClose render={<Button variant="outline" disabled={busy} />}>{t("common:cancel")}</DialogClose>
          <Button variant="destructive" onClick={submit} disabled={busy || selected.size === 0}>
            {busy ? <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" /> : <Trash2 aria-hidden data-icon="inline-start" />}
            {t("dashboard:episode_batch_delete_submit", { count: selected.size })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
