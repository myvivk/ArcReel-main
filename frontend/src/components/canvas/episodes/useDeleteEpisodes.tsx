import { useCallback, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { API } from "@/api";
import { refreshAfterWrite } from "@/components/canvas/shared/refreshAfterWrite";
import { useAppStore } from "@/stores/app-store";
import type { EpisodeBatchDeletionPreview } from "@/types/episodes-view";
import { errMsg } from "@/utils/async";

import { ImpactConfirmDialog, ImpactText } from "./ImpactConfirmDialog";

interface PendingBatchDeletion {
  episodes: number[];
  preview: EpisodeBatchDeletionPreview;
  /** 确认时服务端的清单已变，这是更新后的清单。 */
  changed: boolean;
}

/**
 * 批量删除多集：先向服务端取合并后的丢失清单，确认框只呈现服务端成文的清单。没有产物、原文也能重建时
 * 按普通确认，否则按危险操作确认。确认时清单已变，换成新清单再确认一次，不删除。删除后这些集从列表里
 * 消失，不另行提示。
 */
export function useDeleteEpisodes(projectName: string, onDeleted?: () => void) {
  const { t } = useTranslation(["dashboard", "common"]);
  const [pending, setPending] = useState<PendingBatchDeletion | null>(null);
  const [busy, setBusy] = useState(false);

  const requestDelete = useCallback(
    async (episodes: number[]) => {
      try {
        const response = await API.deleteEpisodes(projectName, episodes);
        if (response.status === "confirmation_required") {
          setPending({ episodes, preview: response.batch, changed: false });
        }
      } catch (err) {
        useAppStore.getState().pushToast(t("dashboard:episode_batch_delete_failed", { message: errMsg(err) }), "error");
      }
    },
    [projectName, t],
  );

  const confirm = async (): Promise<boolean> => {
    if (pending === null) return false;
    setBusy(true);
    try {
      const response = await API.deleteEpisodes(projectName, pending.episodes, pending.preview.revision);
      if (response.status === "confirmation_required") {
        setPending({ ...pending, preview: response.batch, changed: true });
        return false;
      }
      setPending(null);
      onDeleted?.();
      await refreshAfterWrite(projectName, t);
      return true;
    } catch (err) {
      useAppStore.getState().pushToast(t("dashboard:episode_batch_delete_failed", { message: errMsg(err) }), "error");
      return false;
    } finally {
      setBusy(false);
    }
  };

  const dialog: ReactNode = (
    <ImpactConfirmDialog
      request={
        pending === null
          ? null
          : {
              title: t("dashboard:episode_batch_delete_title", { count: pending.episodes.length }),
              body: (
                <ImpactText
                  text={pending.preview.text}
                  changedNotice={pending.changed ? t("dashboard:episode_delete_changed") : null}
                />
              ),
              confirmLabel: t("dashboard:episode_batch_delete_confirm"),
              runningLabel: t("dashboard:episode_batch_delete_running"),
              destructive: !pending.preview.recoverable,
            }
      }
      busy={busy}
      onConfirm={() => void confirm()}
      onCancel={() => setPending(null)}
    />
  );

  return { requestDelete, dialog };
}
