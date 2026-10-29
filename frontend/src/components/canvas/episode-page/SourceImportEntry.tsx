import { useState } from "react";
import { useLocation } from "wouter";
import { useTranslation } from "react-i18next";
import { FileText, Loader2 } from "lucide-react";

import { API } from "@/api";
import { episodeSourceViewPath } from "@/app-routes";
import { refreshAfterWrite } from "@/components/canvas/shared/refreshAfterWrite";
import { Button } from "@/components/ui/button";
import { useAppStore } from "@/stores/app-store";
import { errMsg } from "@/utils/async";

/**
 * 「导入原文」入口：概览、分集空态与分镜空态共用，导入模式下取代「AI 原创剧本」。
 * 项目还没有集时先建第 1 集（创建向导已自动建好，这里只兜底集被删光的情形），
 * 刷新成功后再跳到该集剧本视图，避免新集还不在 store 里。
 */
export function SourceImportEntry({
  projectName,
  episode,
  prominent = false,
}: {
  projectName: string;
  /** 要打开哪一集；null 表示项目还没有集，先建第 1 集。 */
  episode: number | null;
  prominent?: boolean;
}) {
  const { t } = useTranslation(["dashboard", "common"]);
  const [, navigate] = useLocation();
  const [busy, setBusy] = useState(false);

  const open = async () => {
    if (busy) return;
    if (episode !== null) {
      navigate(episodeSourceViewPath(episode));
      return;
    }
    setBusy(true);
    try {
      const created = await API.createEpisode(projectName, { after: null });
      const refreshed = await refreshAfterWrite(projectName, t);
      // 刷新失败时留在原处：新集不在 store 里，跳过去也只会看到旧账本。
      if (refreshed === "success") navigate(episodeSourceViewPath(created.episode));
    } catch (err) {
      useAppStore.getState().pushToast(errMsg(err), "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Button
      variant={prominent ? "default" : "outline"}
      size={prominent ? "default" : "sm"}
      onClick={() => void open()}
      disabled={busy}
    >
      {busy ? (
        <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
      ) : (
        <FileText aria-hidden data-icon="inline-start" />
      )}
      {t("dashboard:source_import_entry")}
    </Button>
  );
}
