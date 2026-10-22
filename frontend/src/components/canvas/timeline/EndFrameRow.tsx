import { useId, useState } from "react";
import { ChevronRight } from "lucide-react";
import { cn } from "cn";
import { Button } from "@/components/ui/button";
import { useTranslation } from "react-i18next";
import { API } from "@/api";
import { AspectFrame } from "@/components/canvas/shared/AspectFrame";
import { useDemoWorkbench } from "@/onboarding/use-demo-workbench";
import { useAppStore } from "@/stores/app-store";
import { useProjectsStore } from "@/stores/projects-store";
import { isResourceBusy, useActiveResourceIds } from "@/stores/tasks-store";
import type { EditorContentMode } from "@/utils/script-shape";
import { errMsg } from "@/utils/async";
import { itemIdWithinEpisode } from "@/utils/episode-display";
import { EndFramePicker } from "./EndFramePicker";

interface EndFrameRowProps {
  projectName: string;
  segmentId: string;
  scriptFile: string;
  contentMode: EditorContentMode;
  aspectRatio: "9:16" | "16:9";
  /** 当前已设置的尾帧快照路径（项目内相对路径），未设置为 null。 */
  endFramePath: string | null;
  /** 只读上下文（如剧本不可编辑时）：仅展示，不给写入入口。 */
  readOnly?: boolean;
  /** 提交在途状态回传：供父级同步禁用同卡片的视频生成 / 上传 / 恢复控件。 */
  onSubmittingChange?: (submitting: boolean) => void;
  /** 视频卡的手动上传占用：同一分镜的视频文件正在上传时反向禁用本行的写入通道，避免与其共享的资产落盘并发冲突。 */
  videoUploadBusy?: boolean;
  /** 本分镜的修改正在保存：禁止同时改写同一条目的快照字段。 */
  shotSaving?: boolean;
}

/**
 * 分镜尾帧设置行：收起显示摘要（未设置 / 已设置），展开为预览 + 说明 + 更换 / 清除。
 *
 * 占用态按仓库规范做三项检查：本分镜视频任务占用时两个写入控件同步禁用（开窗校验 +
 * 兄弟控件同步），提交时刻再从 store 直读一次最新占用态（打开面板后状态可能已变化）。
 * 源图侧零占用——尾帧是快照复制，与源图的生成任务无关；分镜 / 宫格任务同样不参与判定。
 *
 * 快照保存在本镜，供下一镜的首帧接续使用；本镜已有成片时优先接续成片实际尾帧。
 * 快照不进入本镜的尾帧槽，设置入口不依赖模型尾帧能力。
 */
export function EndFrameRow({
  projectName,
  segmentId,
  scriptFile,
  contentMode,
  aspectRatio,
  endFramePath,
  readOnly = false,
  onSubmittingChange,
  videoUploadBusy = false,
  shotSaving = false,
}: EndFrameRowProps) {
  const { t } = useTranslation("dashboard");
  const panelId = useId();
  const [expanded, setExpanded] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const viewOnly = useDemoWorkbench() || readOnly;

  const videoBusyIds = useActiveResourceIds("video", projectName);
  // 占用不区分来源（任务队列在跑 / 视频卡手动上传在途）：二者都在写同一份 project.json，
  // 并发写入都要拦。
  const videoBusy = videoBusyIds.has(segmentId) || videoUploadBusy;
  const fp = useProjectsStore((s) => (endFramePath ? s.getAssetFingerprint(endFramePath) : null));

  // 兄弟控件同步：更换 / 清除 / 选图器的提交入口共读这一个值。
  const controlsDisabled = videoBusy || shotSaving || submitting || viewOnly;

  // 灰化控件的 hover 原因。
  const disabledHint = viewOnly
    ? undefined
    : videoBusy
      ? t("end_frame_busy_hint")
      : shotSaving
        ? t("common:save_status_saving")
        : undefined;

  /**
   * 提交时刻复核最新占用态：面板 / 选图器打开后本分镜可能已被入队，只查开窗时刻会留
   * 一个竞态窗口。命中则拒绝并给出可见反馈。
   */
  const rejectIfDisabled = (): boolean => {
    if (viewOnly || submitting) return true;
    if (shotSaving) {
      useAppStore.getState().pushToast(t("common:save_status_saving"), "info");
      return true;
    }
    if (videoUploadBusy) {
      useAppStore.getState().pushToast(t("end_frame_busy_hint"), "info");
      return true;
    }
    if (!isResourceBusy("video", projectName, segmentId)) return false;
    useAppStore.getState().pushToast(t("end_frame_busy_hint"), "info");
    return true;
  };

  const updateSubmitting = (value: boolean) => {
    setSubmitting(value);
    onSubmittingChange?.(value);
  };

  // 设置与清除尾帧是即时动作，成功不弹提示
  const runWrite = async (action: () => Promise<unknown>) => {
    if (rejectIfDisabled()) return;
    updateSubmitting(true);
    try {
      await action();
    } catch (err) {
      useAppStore
        .getState()
        .pushToast(t("end_frame_action_failed", { message: errMsg(err) }), "error");
      return;
    } finally {
      updateSubmitting(false);
    }
    setPickerOpen(false);
    // 快照路径固定、换图原地覆盖，须重取项目数据拿新的资产指纹才能 cache-bust。
    // refreshProject 内部吞掉请求错误、以返回值表达结果（从不 reject）：写入已经成功，
    // 刷新失败要单独提示，不能把它误报成尾帧写入失败；刷新被项目切换取消则不代表出错，
    // 静默跳过，否则设置尾帧后立刻切项目会误报一条刷新失败。
    const refreshResult = await useProjectsStore.getState().refreshProject(projectName);
    if (refreshResult === "failed") {
      useAppStore.getState().pushToast(t("end_frame_refresh_failed"), "error");
    }
  };

  const handlePickProjectImage = (sourcePath: string) =>
    void runWrite(() => API.selectEndFrame(projectName, segmentId, scriptFile, sourcePath));

  const handlePickUpload = (file: File) =>
    void runWrite(() => API.uploadEndFrame(projectName, segmentId, scriptFile, file));

  const handleClear = () =>
    void runWrite(() => API.clearEndFrame(projectName, segmentId, scriptFile));

  // 摘要行与展开面板里都只是小图，取 160 宽的缩略图
  const previewUrl = endFramePath ? API.getFileUrl(projectName, endFramePath, fp, { width: 160 }) : null;

  const summary = endFramePath ? t("end_frame_summary_set") : t("end_frame_summary_unset");

  return (
    <div className="mb-2.5 rounded-lg border border-border bg-muted/30">
      <button
        type="button"
        onClick={() => setExpanded((prev) => !prev)}
        aria-expanded={expanded}
        aria-controls={panelId}
        className="group/end-frame focus-ring flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left"
      >
        <ChevronRight
          aria-hidden
          className="size-3.5 text-muted-foreground transition-transform group-aria-expanded/end-frame:rotate-90"
        />
        <span className="text-xs font-medium text-subtle-foreground">{t("end_frame_title")}</span>
        <span className="flex-1" />
        {previewUrl && (
          <img
            src={previewUrl}
            alt=""
            aria-hidden
            decoding="async"
            className="h-4 w-2.5 rounded-xs border border-primary/25 object-cover"
          />
        )}
        <span
          aria-live="polite"
          className={cn("text-xs", endFramePath ? "text-primary" : "text-muted-foreground")}
        >
          {summary}
        </span>
      </button>

      {expanded && (
        <div id={panelId} className="flex items-start gap-3 border-t border-border/50 px-3 pt-2.5 pb-3">
          <div
            className={cn(
              "w-16 shrink-0 overflow-hidden rounded-sm border",
              previewUrl ? "border-primary/25" : "border-dashed border-input bg-muted/50",
            )}
          >
            <AspectFrame ratio={aspectRatio}>
              {previewUrl ? (
                <img
                  src={previewUrl}
                  alt={t("end_frame_preview_alt", { id: itemIdWithinEpisode(segmentId) })}
                  className="size-full object-cover"
                  decoding="async"
                />
              ) : (
                <div className="grid size-full place-items-center text-xs text-muted-foreground">
                  {t("end_frame_summary_unset")}
                </div>
              )}
            </AspectFrame>
          </div>
          <div className="flex flex-1 flex-col gap-2">
            <p className="text-xs leading-relaxed text-muted-foreground">{t("end_frame_description")}</p>
            {!viewOnly && (
              <div className="flex items-center gap-1.5">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    if (rejectIfDisabled()) return;
                    setPickerOpen(true);
                  }}
                  disabled={controlsDisabled}
                  title={disabledHint}
                >
                  {endFramePath ? t("end_frame_replace") : t("end_frame_choose")}
                </Button>
                {endFramePath && (
                  <Button variant="ghost" size="sm" onClick={handleClear} disabled={controlsDisabled} title={disabledHint}>
                    {t("end_frame_clear")}
                  </Button>
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {pickerOpen && (
        <EndFramePicker
          projectName={projectName}
          scriptFile={scriptFile}
          contentMode={contentMode}
          aspectRatio={aspectRatio}
          submitting={submitting}
          disabled={controlsDisabled}
          onClose={() => setPickerOpen(false)}
          onPickProjectImage={handlePickProjectImage}
          onPickUpload={handlePickUpload}
        />
      )}
    </div>
  );
}
