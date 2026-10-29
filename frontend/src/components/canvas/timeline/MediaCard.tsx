import { useRef, useState } from "react";
import { isResourceBusy } from "@/stores/tasks-store";
import { Sparkles, ImageIcon, Film, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useTranslation } from "react-i18next";
import { API, type VersionInfo } from "@/api";
import { usePlaybackStart } from "@/hooks/usePlaybackStart";
import { useProjectsStore } from "@/stores/projects-store";
import { AspectFrame } from "@/components/canvas/shared/AspectFrame";
import { PreviewableImageFrame } from "@/components/canvas/shared/PreviewableImageFrame";
import { PresentationPlayer } from "@/components/shared/PresentationPlayer";
import {
  UPLOAD_IMAGE_ACCEPT,
  UPLOAD_VIDEO_ACCEPT,
  UploadIconButton,
} from "@/components/canvas/shared/UploadIconButton";
import { useDemoWorkbench } from "@/onboarding/use-demo-workbench";
import { formatCost } from "@/utils/cost-format";
import type { CostBreakdown } from "@/types";
import { itemIdWithinEpisode } from "@/utils/episode-display";
import { ImageEditButton } from "./ImageEditButton";
import { VersionTimeMachine } from "./VersionTimeMachine";

type MediaKind = "storyboard" | "video";

interface MediaCardProps {
  kind: MediaKind;
  restoring?: boolean;
  onRestoringChange?: (restoring: boolean) => void;
  checkBusy?: () => boolean;
  projectName: string;
  segmentId: string;
  /** 资产相对路径，如 storyboards/E1S2_v1.png */
  assetPath: string | null;
  /** 视频海报缩略图（仅 kind=video 用） */
  posterPath?: string | null;
  /** 渲染比例 */
  aspectRatio: "9:16" | "16:9";
  /** 生成按钮是否禁用（视频生成需要先有分镜图） */
  generateDisabled?: boolean;
  /** 自定义禁用 tooltip，未提供时使用默认（"分镜图未生成"）的视频禁用提示 */
  generateDisabledHint?: string;
  /** 进行中状态 */
  generating?: boolean;
  /** 估算费用（按币种 breakdown，例如 {USD: 0.12} 或 {CNY: 5.25}） */
  estimatedCost?: CostBreakdown;
  /** 触发生成 */
  onGenerate?: () => void;
  /** 生成按钮文案，缺省按有无产物写「生成」或「重新生成」；有未保存修改时传「保存并生成」。 */
  generateLabel?: string;
  /** 版本恢复回调；未提供时不显示版本入口（只读展示无版本可回滚） */
  onRestore?: () => Promise<unknown> | void;
  /** 自主上传回调（替换该分镜的分镜图/视频）；未提供时不显示上传入口 */
  onUpload?: (file: File) => Promise<void> | void;
  /** 本卡片的上传请求进行中 */
  uploading?: boolean;
  /** 其他上传进行中等需要互斥的场景：禁用上传入口但不显示 spinner */
  uploadDisabled?: boolean;
  /** 分镜编辑所需的剧集文件；提供时（且 kind=storyboard、已有图）显示编辑入口 */
  editScriptFile?: string | null;
}

const UPLOAD_ACCEPT: Record<MediaKind, string> = {
  storyboard: UPLOAD_IMAGE_ACCEPT,
  video: UPLOAD_VIDEO_ACCEPT,
};

export function MediaCard({
  kind,
  projectName,
  segmentId,
  assetPath,
  posterPath,
  aspectRatio,
  generateDisabled,
  generateDisabledHint,
  generating,
  estimatedCost,
  onGenerate,
  generateLabel: generateLabelOverride,
  onRestore,
  onUpload,
  uploading,
  uploadDisabled,
  editScriptFile,
  restoring: externalRestoring,
  onRestoringChange,
  checkBusy,
}: MediaCardProps) {
  const { t } = useTranslation("dashboard");
  const [restoring, setRestoring] = useState(false);
  const restoringRef = useRef(false);
  const freshBusy = () => restoringRef.current || Boolean(checkBusy?.()) || isResourceBusy(kind, projectName, segmentId);
  const setRestoreBusy = (next: boolean) => {
    restoringRef.current = next;
    setRestoring(next);
    onRestoringChange?.(next);
  };
  // 演示态只读：卡片上的四个写入口（上传 / 编辑 / 版本恢复 / 生成）从同一处判定关闭，
  // 不再各自靠「对应回调是否传入」推断——那让版本入口与其余入口分属两套机制。
  const demoReadOnly = useDemoWorkbench();

  const assetFp = useProjectsStore((s) =>
    assetPath ? s.getAssetFingerprint(assetPath) : null,
  );
  const playbackStart = usePlaybackStart("videos", segmentId);
  // 历史版本在同一个容器里预览：选中后容器切到该版本；当前媒体变了（恢复 / 重新生成 / 切换分镜）
  // 就退回当前。用键比较在渲染期派生，不在 effect 里同步 setState。
  const previewKey = `${segmentId}:${assetPath ?? ""}:${assetFp ?? ""}`;
  const [previewState, setPreviewState] = useState<{ key: string; version: VersionInfo | null }>({
    key: previewKey,
    version: null,
  });
  const previewVersion = previewState.key === previewKey ? previewState.version : null;
  const setPreviewVersion = (version: VersionInfo | null) => setPreviewState({ key: previewKey, version });
  // 原图只给全屏预览；卡片里按媒体栏宽度从 640 / 1280 两档缩略图中取，媒体栏最宽约 720px。
  const assetUrl = assetPath ? API.getFileUrl(projectName, assetPath, assetFp) : null;
  const storyboardSrcSet =
    assetPath && kind === "storyboard"
      ? `${API.getFileUrl(projectName, assetPath, assetFp, { width: 640 })} 640w, ${API.getFileUrl(projectName, assetPath, assetFp, { width: 1280 })} 1280w`
      : undefined;

  const Icon = kind === "storyboard" ? ImageIcon : Film;
  const title =
    kind === "storyboard" ? t("media_storyboard_title") : t("media_video_title");
  const generateLabel =
    generateLabelOverride ??
    (kind === "storyboard"
      ? assetPath
        ? t("media_regenerate_storyboard")
        : t("media_generate_storyboard")
      : assetPath
        ? t("media_regenerate_video")
        : t("media_generate_video"));
  const resourceType: "storyboards" | "videos" =
    kind === "storyboard" ? "storyboards" : "videos";
  // uploadDisabled 是本卡片之外的互斥占用（如同一分镜另一张卡在上传中）；
  // 编辑/版本恢复/生成同样写这个资源，须一并禁用，否则会与占用中的写操作并发冲突。
  const resourceBusy = generating || uploading || uploadDisabled || restoring || externalRestoring;

  return (
    <div>
      <div className="mb-2 flex min-h-7 items-center gap-1.5">
        <Icon aria-hidden className="size-3.5 text-muted-foreground" />
        <h3 className="text-xs font-medium text-subtle-foreground">{title}</h3>
        <span className="flex-1" />
        {onUpload && !demoReadOnly && (
          <UploadIconButton
            accept={UPLOAD_ACCEPT[kind]}
            label={
              kind === "storyboard"
                ? t("media_upload_storyboard")
                : t("media_upload_video")
            }
            busy={uploading}
            disabled={resourceBusy}
            onSelect={(f) => { if (!freshBusy()) void onUpload(f); }}
          />
        )}
        {kind === "storyboard" && editScriptFile && !demoReadOnly && (
          <ImageEditButton
            projectName={projectName}
            resourceType="storyboard"
            resourceId={segmentId}
            scriptFile={editScriptFile}
            hasImage={Boolean(assetPath)}
            busy={resourceBusy}
          />
        )}
        {onRestore && !demoReadOnly && (
          <VersionTimeMachine
            projectName={projectName}
            resourceType={resourceType}
            resourceId={segmentId}
            onRestore={onRestore}
            busy={resourceBusy}
            checkBusy={freshBusy}
            onRestoringChange={setRestoreBusy}
            onPreviewVersionChange={kind === "video"
              // 选中的就是当前版本、且卡片本来就有当前媒体时不进预览；当前媒体缺失
              // （有版本文件但脚本没引用它）时仍然就地播放这一版，否则用户看不到任何东西。
              ? (info) => setPreviewVersion(info && !(info.is_current && assetPath) ? info : null)
              : undefined}
          />
        )}
      </div>

      {previewVersion && (
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <span className="rounded-sm bg-warn/12 px-1.5 py-0.5 text-xs font-medium text-warn">
            {t("media_previewing_version", { version: previewVersion.version })}
          </span>
          <Button variant="ghost" size="xs" onClick={() => setPreviewVersion(null)}>
            {t("media_preview_back_to_current")}
          </Button>
        </div>
      )}

      {previewVersion ? (
        <div
          className={
            aspectRatio === "16:9"
              ? "w-full overflow-hidden rounded-lg ring-1 ring-border"
              : "w-full max-w-[450px] overflow-hidden rounded-lg ring-1 ring-border"
          }
        >
          <AspectFrame
            ratio={aspectRatio}
            className={aspectRatio === "16:9" ? "w-full" : "h-[265px] w-full max-w-[450px]"}
          >
            {previewVersion.presentation_available === true ? (
              <PresentationPlayer
                key={`${segmentId}:preview:${previewVersion.version}`}
                projectName={projectName}
                resourceType="videos"
                resourceId={segmentId}
                videoVersion={previewVersion.version}
                posterPath={posterPath}
              />
            ) : previewVersion.file_url ? (
              // eslint-disable-next-line jsx-a11y/media-has-caption -- 不能进共享成片读取器的历史版本只展示原始媒体
              <video
                src={previewVersion.file_url}
                aria-label={t("version_preview_alt", { version: previewVersion.version })}
                className="size-full object-contain"
                controls
                playsInline
                preload="none"
              />
            ) : null}
          </AspectFrame>
        </div>
      ) : assetUrl ? (
        kind === "storyboard" ? (
          <PreviewableImageFrame src={assetUrl} alt={`${itemIdWithinEpisode(segmentId)} ${title}`}>
            <AspectFrame ratio={aspectRatio}>
              <img
                src={assetUrl}
                srcSet={storyboardSrcSet}
                sizes="(min-width: 1280px) 720px, 640px"
                alt={`${itemIdWithinEpisode(segmentId)} ${title}`}
                loading="lazy"
                decoding="async"
                className="size-full object-cover"
              />
            </AspectFrame>
          </PreviewableImageFrame>
        ) : (
          <div
            className={
              aspectRatio === "16:9"
                ? "w-full overflow-hidden rounded-lg ring-1 ring-border"
                : "w-full max-w-[450px] overflow-hidden rounded-lg ring-1 ring-border"
            }
          >
            <AspectFrame
              ratio={aspectRatio}
              className={aspectRatio === "16:9" ? "w-full" : "h-[265px] w-full max-w-[450px]"}
            >
              <PresentationPlayer
                key={`${segmentId}:${assetFp ?? "current"}`}
                projectName={projectName}
                resourceType="videos"
                resourceId={segmentId}
                posterPath={posterPath}
                {...playbackStart}
              />
            </AspectFrame>
          </div>
        )
      ) : (
        <AspectFrame ratio={aspectRatio}>
          <div
            className="flex size-full flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-muted/30 text-muted-foreground"
            aria-busy={generating}
          >
            {generating ? (
              <Loader2 aria-hidden className="size-5 animate-spin" />
            ) : (
              <Icon aria-hidden className="size-5" />
            )}
            <span className="text-xs">{t(generating ? "media_generating" : "media_not_generated")}</span>
          </div>
        </AspectFrame>
      )}

      {onGenerate && !demoReadOnly && (
        <Button
          className="mt-2.5 w-full"
          size="lg"
          onClick={onGenerate}
          disabled={generateDisabled || resourceBusy}
          aria-busy={generating}
          title={
            generateDisabled
              ? (generateDisabledHint ?? t("media_generate_video_disabled_hint"))
              : undefined
          }
        >
          {generating ? (
            <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
          ) : (
            <Sparkles aria-hidden data-icon="inline-start" />
          )}
          {generating ? t("media_generating") : generateLabel}
          {!generating && estimatedCost && Object.values(estimatedCost).some((v) => v > 0) && (
            <span className="num text-xs font-normal">~{formatCost(estimatedCost)}</span>
          )}
        </Button>
      )}
    </div>
  );
}
