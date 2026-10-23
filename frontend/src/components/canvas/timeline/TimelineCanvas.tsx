import { useCallback, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { PromptAuthoringButton } from "@/components/canvas/shared/PromptAuthoringButton";
import { RetainedEditUnit } from "@/components/shared/edit-unit/RetainedEditUnit";
import { ShotSplitView } from "./ShotSplitView";
import { StoryboardBatchDialog } from "./StoryboardBatchDialog";
import { EmptyScriptState } from "./EmptyScriptState";
import type { InsertShotHandler } from "./ShotStructureActions";
import { ShortDramaImportButton } from "@/components/canvas/shared/ShortDramaImportDialog";
import { ShortDramaGenerateButton } from "@/components/canvas/shared/ShortDramaGenerateDialog";
import { ShortDramaStoryboardButton } from "@/components/canvas/shared/ShortDramaStoryboardButton";
import { FactoryStoryboardStart } from "@/components/canvas/shared/FactoryStoryboardStart";
import { BatchFillButton, useBatchGap } from "@/components/canvas/episode-page/BatchFillButton";
import { EpisodeHeaderActions } from "@/components/canvas/episode-page/EpisodeHeaderActions";
import type { EpisodeCanvasContext } from "@/components/canvas/episode-page/EpisodePage";
import { useActiveResourceIds } from "@/stores/tasks-store";
import { getScriptItemId } from "@/utils/script-shape";
import { previewAspect } from "@/utils/preview-aspect";
import { ONBOARDING_ANCHORS } from "@/onboarding/anchors";
import { useDemoWorkbench } from "@/onboarding/use-demo-workbench";
import type { DurationOutOfRangeReason } from "@/hooks/useModelCapabilities";
import type {
  EpisodeScript,
  DramaEpisodeScript,
  DramaScene,
  ProjectData,
  StoryboardBatchKind,
} from "@/types";

type Segment = DramaScene;

interface TimelineCanvasProps extends EpisodeCanvasContext {
  projectName: string;
  episode: number;
  episodeScript: EpisodeScript | null;
  scriptFile?: string;
  projectData: ProjectData | null;
  /** 保存分镜字段：失败时抛错；resolve 为本地剧本是否已刷新到保存后的内容。 */
  onUpdatePrompt?: (segmentId: string, patch: Record<string, unknown>, scriptFile?: string) => Promise<boolean>;
  /** 分镜改序：移到 afterId 之后，null 移到最前；resolve 为是否移动成功 */
  onMoveShot?: (shotId: string, afterId: string | null, scriptFile?: string) => Promise<boolean>;
  /** 新增分镜（旁白带正文）：afterId 为 null 时追加到末尾；resolve 为是否成功 */
  onInsertShot?: (afterId: string | null, novelText: string | undefined, scriptFile?: string) => Promise<boolean>;
  /** 移除分镜，resolve 为是否成功 */
  onRemoveShot?: (itemId: string, scriptFile?: string) => Promise<boolean>;
  onGenerateStoryboard?: (segmentId: string, scriptFile?: string) => void;
  onGenerateVideo?: (segmentId: string, scriptFile?: string) => void | Promise<void>;
  onGenerateNarration?: (segmentId: string, scriptFile?: string) => void;
  onGenerateEpisodeNarration?: (scriptFile?: string) => void;
  durationOptions?: number[];
  /** 档位为空是因为这一维由端点固定（workflow 自己定片长），不是型号没登记时长。 */
  durationEndpointFixed?: boolean;
  videoModelUnresolved?: boolean;
  lastFrame?: boolean | null;
  capabilitiesLoading?: boolean;
  /** 已保存时长越界的成因判定；缺省时 ShotDetail 退回不区分成因的通用警告文案。 */
  durationWarningReason?: (seconds: number) => DurationOutOfRangeReason | null;
  onRestoreStoryboard?: () => Promise<unknown> | void;
  onRestoreVideo?: () => Promise<unknown> | void;
}

/**
 * 演示态作废的入参。分镜卡自身的写入口（生成 / 上传 / 编辑 / 版本恢复）由 `MediaCard`
 * 直读同一判定关闭，这里只列本画布额外承载的写能力，两处不重复兜同一个入口。
 */
const DEMO_READ_ONLY_PROPS = {
  onUpdatePrompt: undefined,
  onMoveShot: undefined,
  onInsertShot: undefined,
  onRemoveShot: undefined,
  onGenerateNarration: undefined,
  onGenerateEpisodeNarration: undefined,
} as const satisfies Partial<TimelineCanvasProps>;

export function TimelineCanvas(props: TimelineCanvasProps) {
  // 演示态只读判定直读单一来源，不经父级逐个回调透传——漏接一个回调就是漏一个写入口
  const demoReadOnly = useDemoWorkbench();
  const {
    projectName,
    episode,
    view,
    hasShortDramaSource,
    episodeScript,
    scriptFile,
    projectData,
    durationOptions,
    durationEndpointFixed,
    lastFrame,
    capabilitiesLoading,
    durationWarningReason,
    onUpdatePrompt,
    onMoveShot,
    onInsertShot,
    onRemoveShot,
    onGenerateStoryboard,
    onGenerateVideo,
    onGenerateNarration,
    onGenerateEpisodeNarration,
    onRestoreStoryboard,
    onRestoreVideo,
  } = demoReadOnly ? { ...props, ...DEMO_READ_ONLY_PROPS } : props;

  const { t } = useTranslation("dashboard");
  const editorContentMode = "drama" as const;

  const hasScript = Boolean(episodeScript);
  const [batchKind, setBatchKind] = useState<StoryboardBatchKind | null>(null);
  const shotGridGap = useBatchGap(projectName, episode, "shot-grids");
  const videoGap = useBatchGap(projectName, episode, "videos", "video");
  const narrationGap = useBatchGap(projectName, episode, "narration", "tts");

  const aspectRatio = previewAspect(projectData);

  const segments = useMemo<Segment[]>(
    () =>
      episodeScript?.content_mode === "drama" && "scenes" in episodeScript
        ? episodeScript.scenes
        : [],
    [episodeScript],
  );

  // 任务派生 loading：活跃 + 最新行胜出下沉到 store selector（各 task_type 一组活跃 resource）
  const storyboardBusyIds = useActiveResourceIds("storyboard", projectName);
  const videoBusyIds = useActiveResourceIds("video", projectName);
  const ttsBusyIds = useActiveResourceIds("tts", projectName);
  const generatingStoryboard = useCallback(
    (segId: string) => storyboardBusyIds.has(segId),
    [storyboardBusyIds],
  );
  const generatingVideo = useCallback(
    (segId: string) => videoBusyIds.has(segId),
    [videoBusyIds],
  );
  const generatingNarration = useCallback(
    (segId: string) => ttsBusyIds.has(segId),
    [ttsBusyIds],
  );
  // 批量旁白进行中：当前分集还有未完结的 tts 任务时禁用批量按钮，避免重复入队；
  // 按本集 segment 范围判定，不影响其他分集的批量入口
  const currentSegmentIds = useMemo(
    () => new Set(segments.map((s) => getScriptItemId(s, editorContentMode))),
    [segments, editorContentMode],
  );
  const narrationBatchBusy = useMemo(
    () => [...currentSegmentIds].some((id) => ttsBusyIds.has(id)),
    [ttsBusyIds, currentSegmentIds],
  );

  if (!projectData || !episodeScript) {
    // 工厂集默认落在剧本页；切到分镜页时这里是「还没分镜」，不是「没选剧集」。
    if (projectData && editorContentMode === "drama" && hasShortDramaSource && !demoReadOnly) {
      return (
        <FactoryStoryboardStart
          projectName={projectName}
          episode={episode}
          className="min-h-0"
        />
      );
    }
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        {t("select_episode_hint")}
      </div>
    );
  }

  const handleUpdatePrompt = onUpdatePrompt
    ? (segId: string, patch: Record<string, unknown>) => onUpdatePrompt(segId, patch, scriptFile)
    : undefined;
  const handleMoveShot = onMoveShot
    ? (shotId: string, afterId: string | null) => onMoveShot(shotId, afterId, scriptFile)
    : undefined;
  const handleInsertShot: InsertShotHandler | undefined = onInsertShot
    ? (afterId, novelText) => onInsertShot(afterId, novelText, scriptFile)
    : undefined;
  const handleRemoveShot = onRemoveShot
    ? (itemId: string) => onRemoveShot(itemId, scriptFile)
    : undefined;
  // 生成回调保持可选透传：未提供时编辑器隐藏对应生成入口，
  // 而非渲染一个点了没反应的按钮。
  const handleGenSb = onGenerateStoryboard
    ? (segId: string) => onGenerateStoryboard(segId, scriptFile)
    : undefined;
  const handleGenVid = onGenerateVideo
    ? (segId: string) => onGenerateVideo(segId, scriptFile)
    : undefined;
  const handleGenNarration = onGenerateNarration
    ? (segId: string) => onGenerateNarration(segId, scriptFile)
    : undefined;
  const importedShortDrama =
    Boolean(hasShortDramaSource && (episodeScript as DramaEpisodeScript).short_drama_meta);

  if (!importedShortDrama && !demoReadOnly) {
    return (
      <div className="relative flex min-h-0 flex-1 flex-col gap-4 overflow-auto p-6">
        <p className="text-sm text-muted-foreground">{t("timeline_no_editable_segments")}</p>
        <div className="flex flex-wrap gap-2">
          <ShortDramaGenerateButton projectName={projectName} episode={episode} />
          <ShortDramaImportButton projectName={projectName} episode={episode} />
        </div>
        <pre className="whitespace-pre-wrap break-words text-xs text-muted-foreground">
          {JSON.stringify(episodeScript, null, 2)}
        </pre>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {view === "board" && hasScript && (
        <EpisodeHeaderActions>
          {!demoReadOnly && (
            <>
              <ShortDramaGenerateButton projectName={projectName} episode={episode} />
              <ShortDramaImportButton projectName={projectName} episode={episode} />
            </>
          )}
          {!demoReadOnly && importedShortDrama && (
            <ShortDramaStoryboardButton
              projectName={projectName}
              episode={episode}
              storyboardCount={segments.length}
              sceneIds={segments.map((segment) => getScriptItemId(segment, editorContentMode))}
            />
          )}
          <PromptAuthoringButton projectName={projectName} episode={episode} scope="pending" />
          <BatchFillButton
            kind="shot-grids"
            count={shotGridGap}
            disabled={demoReadOnly}
            onClick={() => setBatchKind("shot-grids")}
          />
          <BatchFillButton
            kind="videos"
            count={videoGap}
            disabled={demoReadOnly}
            onClick={() => setBatchKind("videos")}
          />
          {onGenerateEpisodeNarration && (
            <BatchFillButton
              kind="narration"
              count={narrationGap}
              disabled={narrationBatchBusy}
              onClick={() => onGenerateEpisodeNarration(scriptFile)}
            />
          )}
        </EpisodeHeaderActions>
      )}

      {batchKind && (
        <StoryboardBatchDialog
          projectName={projectName}
          episode={episode}
          kind={batchKind}
          onClose={() => setBatchKind(null)}
        />
      )}

      {/* 主体 */}
      <div className="flex min-h-0 flex-1 flex-col" data-onboarding={ONBOARDING_ANCHORS.workbenchTimeline}>
        <RetainedEditUnit identity={segments.length > 0 ? "shots" : "empty"} message={t("shot_externally_removed")} value={segments.length > 0 ? (
          <ShotSplitView
            segments={segments}
            contentMode={editorContentMode}
            aspectRatio={aspectRatio}
            projectName={projectName}
            episode={episode}
            scriptFile={scriptFile}
            onUpdatePrompt={handleUpdatePrompt}
            onMoveShot={handleMoveShot}
            onInsertShot={handleInsertShot}
            onRemoveShot={handleRemoveShot}
            onGenerateStoryboard={handleGenSb}
            onGenerateVideo={handleGenVid}
            onGenerateNarration={handleGenNarration}
            onRestoreStoryboard={onRestoreStoryboard}
            onRestoreVideo={onRestoreVideo}
            generatingStoryboard={generatingStoryboard}
            generatingVideo={generatingVideo}
            generatingNarration={generatingNarration}
            durationOptions={durationOptions}
            durationEndpointFixed={durationEndpointFixed}
            lastFrame={lastFrame}
            capabilitiesLoading={capabilitiesLoading}
            durationWarningReason={durationWarningReason}
          />
        ) : episodeScript.content_mode === editorContentMode ? (
          <EmptyScriptState contentMode={editorContentMode} onInsert={handleInsertShot} />
        ) : (
          // 兜底：分镜视图下无可编辑分镜（未知 content_mode），
          // 或剧本回退后仍停留在分镜视图——给出指引而非空白
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
            {hasScript ? t("timeline_no_editable_segments") : t("timeline_script_not_ready")}
          </div>
        )}>
          {(content) => content}
        </RetainedEditUnit>
      </div>
    </div>
  );
}
