import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2 } from "lucide-react";
import { API } from "@/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useAppStore } from "@/stores/app-store";
import { useAssetSheetBatchStore } from "@/stores/asset-sheet-batch-store";
import { errMsg } from "@/utils/async";
import type { AssetSheetType, EpisodeAssetPlan } from "@/types";

const TYPES: AssetSheetType[] = ["character", "scene", "prop"];

/**
 * 生成本集资产的确认框：**纯确认**，不收集任何输入。
 *
 * 一键要跑「抽取 → 登记 → 生成描述 → 出图」四步，会调多次文本与图像模型、并落盘写资产，
 * 所以按钮与真正生成之间必须有这道关卡：把本批范围（多少资产、按类型分）与预计的模型调用
 * 次数摆出来给用户确认，点「确定」才发请求。
 */
export function EpisodeAssetGenerateDialog({
  projectName,
  episode,
  episodeTitle,
  plan,
  onClose,
  onSubmitted,
}: {
  projectName: string;
  episode: number;
  episodeTitle: string;
  plan: EpisodeAssetPlan;
  onClose: () => void;
  /** 提交成功后通知外部刷新本集资产列表。 */
  onSubmitted: () => void;
}) {
  const { t } = useTranslation(["assets", "common", "dashboard"]);
  const [submitting, setSubmitting] = useState(false);

  const handleConfirm = async () => {
    setSubmitting(true);
    try {
      const result = await API.generateEpisodeAssets(projectName, episode);
      const members = result.members.filter((member) => member.status !== "blocked");
      if (members.length > 0) {
        useAssetSheetBatchStore.getState().track({ batchId: result.batch_id, projectName, members });
      }
      const queuedCount = members.filter((member) => member.task_id).length;
      if (queuedCount > 0) {
        useAppStore.getState().pushToast(t("sheet_batch_submitted", { ns: "assets", count: queuedCount }), "success");
      }
      const registeredCount = Object.values(result.registered).reduce((sum, names) => sum + (names?.length ?? 0), 0);
      if (registeredCount > 0) {
        useAppStore
          .getState()
          .pushToast(t("episode_assets_registered", { ns: "dashboard", count: registeredCount }), "success");
      }
      if (result.description_filled.length > 0) {
        useAppStore
          .getState()
          .pushToast(
            t("episode_assets_description_filled", { ns: "dashboard", count: result.description_filled.length }),
            "success",
          );
      }
      if (result.description_fill_failed.length > 0) {
        useAppStore
          .getState()
          .pushToast(
            t("episode_assets_description_fill_failed", {
              ns: "dashboard",
              count: result.description_fill_failed.length,
            }),
            "warning",
          );
      }
      if (result.without_description.length > 0) {
        useAppStore
          .getState()
          .pushToast(
            t("episode_assets_missing_description", {
              ns: "dashboard",
              count: result.without_description.length,
            }),
            "warning",
          );
      }
      onSubmitted();
      onClose();
    } catch (err) {
      useAppStore.getState().pushToast(errMsg(err), "error");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        // 提交在途时忽略 Esc 与遮罩点击
        if (!next && !submitting) onClose();
      }}
    >
      <DialogContent showCloseButton={!submitting}>
        <DialogHeader>
          <DialogTitle>{t("episode_assets_generate", { ns: "dashboard" })}</DialogTitle>
          <DialogDescription>
            {t("episode_assets_dialog_sub", { ns: "dashboard", episode, title: episodeTitle })}
          </DialogDescription>
        </DialogHeader>
        <DialogBody tabIndex={0} role="region" aria-label={t("episode_assets_generate", { ns: "dashboard" })}>
          <div className="flex flex-col gap-3 text-sm">
            <p className="text-muted-foreground">{t("episode_assets_dialog_hint", { ns: "dashboard" })}</p>
            <div className="rounded-lg border bg-muted/40 p-3" data-testid="episode-asset-batch">
              <p className="flex items-center gap-2 font-medium">
                <span aria-hidden className="size-1.5 shrink-0 rounded-full bg-primary" />
                {t("episode_assets_batch_title", { ns: "dashboard" })}
              </p>
              <div className="mt-2 pl-3.5">
                <p>{t("episode_assets_batch_total", { ns: "dashboard", count: plan.total })}</p>
                <p className="mt-1 flex flex-wrap gap-1.5">
                  {TYPES.map((type) => (
                    <span
                      key={type}
                      className="rounded-md border bg-background px-2 py-0.5 text-xs text-muted-foreground"
                    >
                      {t(`type.${type}`)} {plan.by_type[type] ?? 0}
                    </span>
                  ))}
                </p>
                <p className="mt-1.5 text-muted-foreground">
                  {t("episode_assets_batch_cost", {
                    ns: "dashboard",
                    text: plan.text_calls,
                    image: plan.image_calls,
                  })}
                </p>
              </div>
            </div>
          </div>
        </DialogBody>
        <DialogFooter>
          <DialogClose render={<Button variant="outline" />} disabled={submitting}>
            {t("common:cancel")}
          </DialogClose>
          <Button disabled={submitting} onClick={() => void handleConfirm()}>
            {submitting && <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />}
            {submitting ? t("episode_assets_generating", { ns: "dashboard" }) : t("episode_assets_confirm", { ns: "dashboard" })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
