import { useCallback, useEffect, useId, useMemo, useState, type RefObject } from "react";
import { useTranslation } from "react-i18next";
import { AlertTriangle, Film, ImageIcon, Timer } from "lucide-react";
import type { DurationOutOfRangeReason } from "@/hooks/useModelCapabilities";
import type { NarrationSegment, DramaScene, AdShot, ImagePrompt, VideoPrompt } from "@/types";
import type { ShotGridPlan } from "@/types/shot-grid";
import { ShotDetailHeader } from "./ShotDetailHeader";
import { ShotDetailLayout, ShotGroup, ShotSection } from "./ShotDetailLayout";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { PartialSaveError, useEditUnit } from "@/components/shared/edit-unit/useEditUnit";
import { UnsavedChangesBar } from "@/components/shared/edit-unit/UnsavedChangesBar";
import { useStaysInEpisodeView } from "@/components/canvas/episode-page/EpisodeViewScope";
import { PromptPreviewButton } from "@/components/shared/PromptPreviewButton";
import { ReferencesSection } from "./ReferencesSection";
import { ReferenceMaterialsSection } from "./ReferenceMaterialsSection";
import { VideoPromptEditor } from "./VideoPromptEditor";
import { PromptReferenceMenu } from "./PromptReferenceMenu";
import { MediaCard } from "./MediaCard";
import { EndFrameRow } from "./EndFrameRow";
import { NotesDrawer } from "./NotesDrawer";
import { StatusBadge, statusFromAssets } from "./StatusBadge";
import { SegmentBreakToggle } from "./SegmentBreakToggle";
import { ShotStructureActions, type InsertShotHandler } from "./ShotStructureActions";
import { API } from "@/api";
import { useAppStore } from "@/stores/app-store";
import { isResourceBusy, isScriptFileBusy } from "@/stores/tasks-store";
import { useCostStore } from "@/stores/cost-store";
import { useProjectsStore } from "@/stores/projects-store";
import { durationIncompatibleLabel } from "@/components/shared/ShotDurationSelect";
import { errMsg } from "@/utils/async";
import { isContinuousIntegerRange } from "@/utils/duration_format";
import { itemIdWithinEpisode } from "@/utils/episode-display";
import { charactersFieldFor } from "@/utils/script-shape";
import { isStructuredImagePrompt, isStructuredVideoPrompt } from "@/utils/prompt-shape";
import { PromptAuthoringButton } from "@/components/canvas/shared/PromptAuthoringButton";

type Segment = NarrationSegment | DramaScene | AdShot;
type ContentMode = "narration" | "drama" | "ad";
type PromptSide = "image" | "video";

interface ShotDetailProps {
  segment: Segment;
  segmentId: string;
  contentMode: ContentMode;
  aspectRatio: "9:16" | "16:9";
  projectName: string;
  episode?: number;
  scriptFile?: string;
  selectedIndex: number;
  totalCount: number;
  onPrev: () => void;
  onNext: () => void;
  onUpdatePrompt?: (segmentId: string, patch: Record<string, unknown>) => Promise<boolean>;
  onMoveShot?: (shotId: string, direction: "earlier" | "later") => void | Promise<void>;
  movePending?: boolean;
  onInsertShot?: InsertShotHandler;
  onRemoveShot?: (itemId: string) => Promise<boolean>;
  structurePending?: boolean;
  onGenerateStoryboard?: (segmentId: string) => void;
  onGenerateVideo?: (segmentId: string) => void | Promise<void>;
  onGenerateNarration?: (segmentId: string) => void;
  onRestoreStoryboard?: () => Promise<unknown> | void;
  onRestoreVideo?: () => Promise<unknown> | void;
  generatingStoryboard?: boolean;
  generatingVideo?: boolean;
  generatingNarration?: boolean;
  durationOptions?: number[];
  durationEndpointFixed?: boolean;
  lastFrame?: boolean | null;
  capabilitiesLoading?: boolean;
  durationWarningReason?: (seconds: number) => DurationOutOfRangeReason | null;
  editRef?: RefObject<ShotEditHandle | null>;
}

export interface ShotEditHandle {
  dirty: boolean;
  save: () => void;
}

interface ShotFields {
  image_prompt: ImagePrompt | string | null;
  video_prompt: VideoPrompt | string | null;
  duration_seconds: number;
  note: string;
  characters: string[];
  scenes: string[];
  props: string[];
  segment_break?: boolean;
}

const FIELD_KEYS = [
  "image_prompt",
  "video_prompt",
  "duration_seconds",
  "note",
  "characters",
  "scenes",
  "props",
  "segment_break",
] as const satisfies readonly (keyof ShotFields)[];

function fieldEqual(a: unknown, b: unknown): boolean {
  return a === b || JSON.stringify(a ?? "") === JSON.stringify(b ?? "");
}

function fieldsEqual(a: ShotFields, b: ShotFields): boolean {
  return FIELD_KEYS.every((key) => fieldEqual(a[key], b[key]));
}

function imagePromptText(prompt: ShotFields["image_prompt"]): string {
  if (typeof prompt === "string") return prompt;
  return isStructuredImagePrompt(prompt) ? prompt.scene : "";
}

function videoPromptText(prompt: ShotFields["video_prompt"]): string {
  if (typeof prompt === "string") return prompt;
  if (!isStructuredVideoPrompt(prompt)) return "";
  const shots = (prompt as { shot_sequence?: unknown }).shot_sequence;
  if (Array.isArray(shots) && shots.length) {
    return shots
      .filter((shot): shot is Record<string, unknown> => !!shot && typeof shot === "object")
      .map((shot, index) => {
        const timeline = typeof shot.timeline === "string" && shot.timeline.trim() ? shot.timeline.trim() : `镜头${index + 1}`;
        const description = typeof shot.description === "string" ? shot.description.trim() : "";
        const audio = typeof shot.audio === "string" && shot.audio.trim() ? ` ${shot.audio.trim()}` : "";
        return `${timeline}：${description}${audio}`.trim();
      })
      .filter(Boolean)
      .join("\n");
  }
  return prompt.action;
}

function storyboardAssetReferenceText(name: string): string {
  return `${name}@[${name}]`;
}

function shotFieldsOf(segment: Segment, contentMode: ContentMode): ShotFields {
  const base = {
    image_prompt: segment.image_prompt,
    video_prompt: segment.video_prompt,
    duration_seconds: segment.duration_seconds ?? 0,
    note: segment.note ?? "",
    scenes: segment.scenes ?? [],
    props: segment.props ?? [],
  };
  if (contentMode === "ad") {
    return { ...base, characters: (segment as AdShot).characters_in_shot ?? [] };
  }
  if (contentMode === "drama") {
    const scene = segment as DramaScene;
    return { ...base, characters: scene.characters_in_scene ?? [], segment_break: scene.segment_break === true };
  }
  const narration = segment as NarrationSegment;
  return { ...base, characters: narration.characters_in_segment ?? [], segment_break: narration.segment_break === true };
}

function durationLockedByTasks(projectName: string, segmentId: string, scriptFile?: string): boolean {
  return (
    isResourceBusy("storyboard", projectName, segmentId) ||
    isResourceBusy("video", projectName, segmentId) ||
    isScriptFileBusy("shot_grid", scriptFile, projectName)
  );
}

interface DurationPillProps {
  seconds: number;
  segmentId: string;
  projectName: string;
  scriptFile?: string;
  durationOptions: number[];
  durationEndpointFixed?: boolean;
  durationWarningReason?: ShotDetailProps["durationWarningReason"];
  onChange?: (seconds: number) => void;
  busy?: boolean;
}

function DurationPill({
  seconds,
  segmentId,
  projectName,
  scriptFile,
  durationOptions,
  durationEndpointFixed = false,
  durationWarningReason,
  onChange,
  busy = false,
}: DurationPillProps) {
  const { t } = useTranslation("dashboard");
  const hintId = useId();
  const [open, setOpen] = useState(false);

  const rejectIfBusy = useCallback(() => {
    if (!busy && !durationLockedByTasks(projectName, segmentId, scriptFile)) return false;
    useAppStore.getState().pushToast(t("duration_locked_generating"), "info");
    return true;
  }, [busy, projectName, segmentId, scriptFile, t]);

  const noOptions = durationOptions.length === 0;
  const locked = noOptions || busy;
  const [prevLocked, setPrevLocked] = useState(locked);
  if (locked !== prevLocked) {
    setPrevLocked(locked);
    if (locked) setOpen(false);
  }

  const isIncompatible = durationOptions.length > 0 && !durationOptions.includes(seconds);
  const incompatibleLabel = durationIncompatibleLabel(t, seconds, durationOptions, durationWarningReason?.(seconds));
  const useSlider = isContinuousIntegerRange(durationOptions) && durationOptions.length >= 5;
  const content = (
    <>
      <Timer aria-hidden className="size-3.5" />
      <span className="num">{t("duration_seconds_value_text", { value: seconds })}</span>
      {isIncompatible ? (
        <>
          <AlertTriangle aria-hidden className="size-3.5 text-warn" />
          <span className="sr-only">{incompatibleLabel}</span>
        </>
      ) : null}
    </>
  );

  if (!onChange) {
    return <span className="inline-flex items-center gap-1 text-xs text-subtle-foreground">{content}</span>;
  }

  if (locked) {
    const reason = busy
      ? t("duration_locked_generating")
      : t(durationEndpointFixed ? "duration_not_driven_notice" : "duration_no_options");
    return (
      <>
        <Tooltip>
          <TooltipTrigger render={<span className="inline-flex" />}>
            <Button variant="outline" size="xs" disabled aria-describedby={hintId}>
              {content}
            </Button>
          </TooltipTrigger>
          <TooltipContent>{reason}</TooltipContent>
        </Tooltip>
        <span id={hintId} hidden>{reason}</span>
      </>
    );
  }

  const select = (next: number) => {
    if (rejectIfBusy()) {
      setOpen(false);
      return;
    }
    onChange(next);
  };

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        if (next && rejectIfBusy()) return;
        setOpen(next);
      }}
    >
      <PopoverTrigger render={<Button variant="outline" size="xs" />}>{content}</PopoverTrigger>
      <PopoverContent align="start" positionMethod="fixed" aria-label={t("duration_selector_aria")} className="w-auto max-w-80">
        {isIncompatible ? (
          <p className="flex items-start gap-1.5 text-xs text-subtle-foreground">
            <AlertTriangle aria-hidden className="mt-px size-3.5 shrink-0 text-warn" />
            {incompatibleLabel}
          </p>
        ) : null}
        {useSlider ? (
          <div className="flex items-center gap-2">
            <input
              type="range"
              aria-label={t("duration_selector_aria")}
              aria-valuetext={t("duration_seconds_value_text", { value: seconds })}
              min={durationOptions[0]}
              max={durationOptions[durationOptions.length - 1]}
              step={1}
              value={seconds}
              onChange={(event) => select(Number(event.target.value))}
              className="w-40 accent-primary"
            />
            <span className="num min-w-9 text-right text-xs text-subtle-foreground">
              {t("duration_seconds_value_text", { value: seconds })}
            </span>
          </div>
        ) : (
          <div className="num flex flex-wrap gap-1" role="radiogroup" aria-label={t("duration_selector_aria")}>
            {durationOptions.map((duration) => (
              <Button
                key={duration}
                role="radio"
                aria-checked={duration === seconds}
                variant={duration === seconds ? "default" : "outline"}
                size="xs"
                onClick={() => {
                  select(duration);
                  setOpen(false);
                }}
              >
                {t("duration_seconds_value_text", { value: duration })}
              </Button>
            ))}
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}

/** 切镜时重建编辑与加载状态，迟到的宫格响应不会进入下一镜。 */
export function ShotDetail(props: ShotDetailProps) {
  return <ShotDetailContent key={`${props.projectName}:${props.scriptFile}:${props.segmentId}`} {...props} />;
}

function ShotDetailContent({
  segment,
  segmentId,
  contentMode,
  aspectRatio,
  projectName,
  episode,
  scriptFile,
  selectedIndex,
  totalCount,
  onPrev,
  onNext,
  onUpdatePrompt,
  onMoveShot,
  movePending,
  onInsertShot,
  onRemoveShot,
  structurePending,
  onGenerateVideo,
  onRestoreVideo,
  generatingStoryboard,
  generatingVideo,
  generatingNarration,
  durationOptions = [],
  durationEndpointFixed,
  durationWarningReason,
  editRef,
}: ShotDetailProps) {
  const { t } = useTranslation("dashboard");
  const readOnly = !onUpdatePrompt;
  const supportsSegmentBreak = contentMode === "narration" || contentMode === "drama";
  const charField = charactersFieldFor(contentMode);
  const status = statusFromAssets(segment.generated_assets?.status);
  const segCost = useCostStore((state) => state.getSegmentCost(segmentId));
  const assets = segment.generated_assets;

  const source = useMemo(() => shotFieldsOf(segment, contentMode), [segment, contentMode]);
  const save = useCallback(async (value: ShotFields, saved: ShotFields) => {
    if (!onUpdatePrompt) return;
    const patch: Record<string, unknown> = {};
    for (const key of FIELD_KEYS) {
      if (fieldEqual(value[key], saved[key])) continue;
      patch[key === "characters" ? charField : key] = value[key];
    }
    if (!Object.keys(patch).length) return;
    if (
      "duration_seconds" in patch &&
      (generatingStoryboard || generatingVideo || durationLockedByTasks(projectName, segmentId, scriptFile))
    ) {
      throw new Error(t("duration_locked_generating"));
    }
    if (!await onUpdatePrompt(segmentId, patch)) {
      throw new PartialSaveError(t("shot_saved_not_refreshed"), { saved: value });
    }
  }, [onUpdatePrompt, charField, generatingStoryboard, generatingVideo, projectName, segmentId, scriptFile, t]);

  const allowNavigation = useStaysInEpisodeView();
  const unit = useEditUnit({
    source,
    save,
    isEqual: fieldsEqual,
    allowNavigation,
    leaveTitle: t("shot_leave_title", { id: itemIdWithinEpisode(segmentId) }),
  });
  const { value: fields, setValue, dirty, save: saveUnit } = unit;
  const saving = unit.status === "saving";
  const refreshPending = !fieldsEqual(source, unit.savedValue);

  const prepareGeneration = useCallback(
    async () => !readOnly && !refreshPending && await saveUnit(),
    [readOnly, refreshPending, saveUnit],
  );
  const preparePreview = useCallback(
    async () => !refreshPending && await saveUnit(),
    [refreshPending, saveUnit],
  );

  useEffect(() => {
    if (!editRef) return;
    editRef.current = { dirty, save: () => { if (!saving) void saveUnit(); } };
    return () => { editRef.current = null; };
  }, [editRef, dirty, saveUnit, saving]);

  const setField = <K extends keyof ShotFields>(key: K, value: ShotFields[K]) =>
    setValue((prev) => ({ ...prev, [key]: value }));
  const imageText = imagePromptText(fields.image_prompt);
  const videoText = videoPromptText(fields.video_prompt);
  const [plan, setPlan] = useState<ShotGridPlan | null>(null);
  const [materialsBusy, setMaterialsBusy] = useState(false);
  const [restoringMedia, setRestoringMedia] = useState(false);
  const [uploadingVideo, setUploadingVideo] = useState(false);
  const [endFrameSubmitting, setEndFrameSubmitting] = useState(false);
  const editingBlocked = readOnly || saving || materialsBusy;
  const materials = plan && ["completed", "confirmed"].includes(plan.status)
    ? plan.cells.filter((cell) => cell.image_path)
    : [];
  const hasConfirmedMaterials = materials.length > 0;
  const gridBusy = () =>
    isScriptFileBusy("shot_grid", scriptFile, projectName);
  const generateLabel = dirty ? t("common:save_and_generate") : undefined;
  const savingHint = saving ? t("common:save_status_saving") : undefined;
  const videoDisabled = editingBlocked || refreshPending || !!generatingVideo || !hasConfirmedMaterials;

  const handleVideoUpload = async (file: File) => {
    if (!scriptFile || uploadingVideo || restoringMedia || isResourceBusy("video", projectName, segmentId) || gridBusy()) return;
    setUploadingVideo(true);
    try {
      const result = await API.uploadShotMedia(projectName, scriptFile, segmentId, "video", file);
      useProjectsStore.getState().updateAssetFingerprints(result.asset_fingerprints);
      const refreshed = await onRestoreVideo?.();
      if (refreshed === false) return;
      useAppStore.getState().pushToast(t("media_upload_success", { id: segmentId }), "success");
    } catch (error) {
      useAppStore.getState().pushToast(t("media_upload_failed", { message: errMsg(error) }), "error");
    } finally {
      setUploadingVideo(false);
    }
  };

  const generateVideo = () => {
    if (!onGenerateVideo || videoDisabled || isResourceBusy("video", projectName, segmentId) || gridBusy()) return;
    void unit.saveAndGenerate(() => onGenerateVideo(segmentId));
  };

  const insertReference = (side: PromptSide, token: string) => {
    if (editingBlocked) return;
    const append = (text: string) => `${text}${text && !/\s$/.test(text) ? " " : ""}${token} `;
    setValue((prev) => {
      if (side === "image") {
        return { ...prev, image_prompt: append(imagePromptText(prev.image_prompt)) };
      }
      return { ...prev, video_prompt: append(videoPromptText(prev.video_prompt)) };
    });
  };

  const promptActions = (side: PromptSide) => (
    <>
      {scriptFile ? (
        <PromptPreviewButton
          title={t(side === "image" ? "prompt_preview_title_storyboard_image" : "prompt_preview_title_video")}
          load={async (signal) => (await API.previewScriptItemPrompts(
            projectName, segmentId, scriptFile, { signal },
          ))[side === "image" ? "storyboard_image" : "video"]}
          beforeOpen={preparePreview}
          saveFirst={dirty}
          disabled={saving || refreshPending || materialsBusy}
        />
      ) : null}
    </>
  );

  const navDisabled = !!movePending || !!structurePending;
  const navDisabledHint = movePending
    ? t("shot_move_pending")
    : structurePending
      ? t("shot_structure_pending")
      : undefined;
  const structureDisabled = navDisabled || dirty || saving;
  const structureDisabledHint = navDisabledHint ?? (dirty || saving ? t("shot_detail_save_first") : undefined);
  const durationBusy = !!generatingStoryboard || !!generatingVideo;

  const header = (
    <ShotDetailHeader
      segmentId={segmentId}
      index={selectedIndex}
      total={totalCount}
      meta={
        <>
          <DurationPill
            seconds={fields.duration_seconds}
            segmentId={segmentId}
            projectName={projectName}
            scriptFile={scriptFile}
            durationOptions={durationOptions}
            durationEndpointFixed={durationEndpointFixed}
            durationWarningReason={durationWarningReason}
            onChange={readOnly ? undefined : (seconds) => setField("duration_seconds", seconds)}
            busy={durationBusy}
          />
          <StatusBadge status={status} />
          {supportsSegmentBreak && !readOnly ? (
            <SegmentBreakToggle
              checked={fields.segment_break === true}
              onChange={(next) => setField("segment_break", next)}
              disabled={durationBusy}
            />
          ) : null}
        </>
      }
      onMoveEarlier={onMoveShot ? () => void onMoveShot(segmentId, "earlier") : undefined}
      onMoveLater={onMoveShot ? () => void onMoveShot(segmentId, "later") : undefined}
      structureActions={
        <ShotStructureActions
          segmentId={segmentId}
          contentMode={contentMode}
          disabled={structureDisabled}
          disabledHint={structureDisabledHint}
          removeBlockedHint={
            generatingStoryboard || generatingVideo || generatingNarration
              ? t("shot_remove_blocked_generating")
              : totalCount <= 1
                ? t("shot_remove_blocked_last")
                : undefined
          }
          onInsert={onInsertShot}
          onRemove={onRemoveShot}
        />
      }
      onPrev={onPrev}
      onNext={onNext}
      navDisabled={navDisabled}
      navDisabledHint={navDisabledHint}
      notes={
        readOnly ? null : (
          <NotesDrawer shotId={segmentId} value={fields.note} onChange={(note) => setField("note", note)} />
        )
      }
    />
  );

  const main = (
    <>
      <ShotGroup>
        <ReferencesSection
          projectName={projectName}
          contentMode={contentMode}
          characterNames={fields.characters}
          sceneNames={fields.scenes}
          propNames={fields.props}
          disabled={editingBlocked}
          onSave={(patch) => {
            if (editingBlocked) return;
            setValue((prev) => ({
              ...prev,
              characters: patch[charField] ?? prev.characters,
              scenes: patch.scenes ?? prev.scenes,
              props: patch.props ?? prev.props,
            }));
          }}
        />
      </ShotGroup>
      <ShotGroup>
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-medium text-subtle-foreground">{t("detail_section_prompts")}</h3>
        </div>
        {/* 有骨架提示词的条目不该再喊「尚未编写」：机械切分会预填一版可编辑的分镜提示词。 */}
        {segment.pending_authoring && !imageText ? (
          <p role="status" className="text-xs text-muted-foreground">{t("detail_pending_authoring_hint")}</p>
        ) : null}
        <ShotSection title={t("detail_image_prompt_title")} icon={<ImageIcon aria-hidden className="size-3.5" />}>
          <div className="flex flex-wrap items-center gap-2">
            <PromptReferenceMenu
              label={t("shot_insert_asset")}
              disabled={editingBlocked}
              groups={[
                { label: t("references_badge_character"), items: fields.characters.map((name) => ({ label: name, token: storyboardAssetReferenceText(name) })) },
                { label: t("references_badge_scene"), items: fields.scenes.map((name) => ({ label: name, token: storyboardAssetReferenceText(name) })) },
                { label: t("references_badge_prop"), items: fields.props.map((name) => ({ label: name, token: storyboardAssetReferenceText(name) })) },
              ]}
              onSelect={(token: string) => insertReference("image", token)}
            />
            {episode !== undefined && !readOnly ? (
              <PromptAuthoringButton
                projectName={projectName}
                episode={episode}
                scope="current"
                currentEntryId={segmentId}
                defaultRewrite
                defaultFields={["image_prompt"]}
                lockFields
                label={t("prompt_authoring_open_image")}
              />
            ) : null}
            {promptActions("image")}
          </div>
          <Textarea
            aria-label={t("detail_image_prompt_title")}
            value={imageText}
            onChange={(event) => setField("image_prompt", event.target.value)}
            readOnly={editingBlocked}
            placeholder={t("detail_image_prompt_placeholder")}
            className="max-h-none min-h-31"
          />
        </ShotSection>
        <ReferenceMaterialsSection
          key={`${projectName}:${scriptFile}:${segmentId}`}
          projectName={projectName}
          scriptFile={scriptFile}
          episode={episode}
          shotId={segmentId}
          aspectRatio={aspectRatio}
          readOnly={readOnly}
          disabled={saving || refreshPending || !!generatingStoryboard || !!generatingVideo}
          videoPromptFilled={videoText.trim().length > 0}
          saveFirst={dirty}
          beforeGenerate={prepareGeneration}
          onPlanChange={setPlan}
          onBusyChange={setMaterialsBusy}
        />
        <ShotSection title={t("detail_video_prompt_title")} icon={<Film aria-hidden className="size-3.5" />}>
          <div className="flex flex-wrap items-center gap-2">
            <PromptReferenceMenu
              label={t("shot_insert_material")}
              disabled={editingBlocked}
              groups={[{
                label: t("shot_reference_materials"),
                items: materials.map((cell) => ({
                  label: t("shot_keyframe", { index: cell.index }),
                  token: `K${cell.index}`,
                })),
              }]}
              onSelect={(token: string) => insertReference("video", token)}
            />
            {episode !== undefined && !readOnly && materials.length > 0 ? (
              <PromptAuthoringButton
                projectName={projectName}
                episode={episode}
                scope="current"
                currentEntryId={segmentId}
                defaultRewrite
                defaultFields={["video_prompt"]}
                lockFields
                label={t("prompt_authoring_open_video")}
              />
            ) : null}
            {promptActions("video")}
          </div>
          <VideoPromptEditor
            ariaLabel={t("detail_video_prompt_title")}
            value={videoText}
            onChange={(next) => setField("video_prompt", next)}
            readOnly={editingBlocked}
            placeholder={t("detail_video_prompt_placeholder")}
          />
        </ShotSection>
      </ShotGroup>
    </>
  );

  const videoDisabledHint = !hasConfirmedMaterials ? t("shot_materials_unavailable") : savingHint;
  const videoMedia = (
    <>
      {scriptFile && onGenerateVideo ? (
        <EndFrameRow
          projectName={projectName}
          segmentId={segmentId}
          scriptFile={scriptFile}
          contentMode={contentMode}
          aspectRatio={aspectRatio}
          endFramePath={segment.end_frame_image ?? null}
          readOnly={readOnly}
          onSubmittingChange={setEndFrameSubmitting}
          videoUploadBusy={uploadingVideo}
          shotSaving={saving}
        />
      ) : null}
      <MediaCard
        kind="video"
        projectName={projectName}
        segmentId={segmentId}
        assetPath={assets?.video_clip ?? null}
        posterPath={assets?.video_thumbnail ?? null}
        aspectRatio={aspectRatio}
        generating={generatingVideo}
        generateDisabled={videoDisabled}
        generateDisabledHint={videoDisabledHint}
        estimatedCost={segCost?.estimate?.video ?? undefined}
        onGenerate={onGenerateVideo ? generateVideo : undefined}
        generateLabel={generateLabel}
        restoring={restoringMedia}
        onRestoringChange={setRestoringMedia}
        checkBusy={() => uploadingVideo || saving || endFrameSubmitting || gridBusy()}
        onRestore={onRestoreVideo}
        onUpload={scriptFile && !readOnly ? handleVideoUpload : undefined}
        uploading={uploadingVideo}
        uploadDisabled={endFrameSubmitting}
      />
    </>
  );

  return (
    <ShotDetailLayout
      header={header}
      footer={<UnsavedChangesBar unit={unit} disabled={editingBlocked} className="mx-5 my-3 shrink-0" />}
      main={main}
      media={<div className="flex min-w-0 flex-col">{videoMedia}</div>}
    />
  );
}
