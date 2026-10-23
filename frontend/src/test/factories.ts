import type { DramaScene, NarrationSegment, TaskItem } from "@/types";
import type {
  WorkflowContent,
  WorkflowPlan,
  WorkflowPlanStep,
  WorkflowStatus,
} from "@/types/workflow";


/** Shared test factory for `TaskItem`. Defaults model a freshly-queued
 *  factory video task; callers override fields relevant to each scenario. */
export function makeTask(overrides: Partial<TaskItem> = {}): TaskItem {
  return {
    task_id: "t1",
    project_name: "proj",
    task_type: "video",
    media_type: "video",
    resource_id: "E1S01",
    resource_type: null,
    script_file: null,
    payload: {},
    status: "queued",
    result: null,
    error_message: null,
    cancelled_by: null,
    provider_id: null,
    provider_job_id: null,
    source: "webui",
    queued_at: "2026-04-20T00:00:00Z",
    started_at: null,
    finished_at: null,
    updated_at: "2026-04-20T00:00:00Z",
    ...overrides,
  };
}

/** 一个最小可用的计划步骤；各用例只覆盖自己关心的轴。 */
export function makeStep(overrides: Partial<WorkflowPlanStep> = {}): WorkflowPlanStep {
  return {
    id: "video",
    state: "ready",
    required: true,
    action: null,
    requested_ids: [],
    artifacts: {},
    problems: [],
    tasks: [],
    admission: null,
    contracts: {},
    ...overrides,
  };
}

/** 工厂一集的内容现状；缺省是正文在位、正式分镜 2 条、提示词已编写。 */
export function makeContent(overrides: Partial<WorkflowContent> = {}): WorkflowContent {
  return {
    episode_count: 1,
    episode_source: "present",
    script_from_short_drama: true,
    formal_script: "present",
    script_item_count: 2,
    pending_authoring_ids: [],
    referenced_assets_without_sheet: [],
    unregistered_references: [],
    referenced_asset_sheets_stale: [],
    referenced_assets_without_description: [],
    ...overrides,
  };
}

export function makeStatus(overrides: Partial<WorkflowStatus> = {}): WorkflowStatus {
  return {
    schema_version: 2,
    project_revision: "sha256-v1:project",
    source_revision: "sha256-v1:source",
    project: { content_mode: "drama", generation_mode: "storyboard" },
    target: { episode: 1, script: "scripts/episode_1.json", script_filename: "episode_1.json", source: "source/short_drama/episodes/episode_01.md" },
    blockers: [],
    issues: [],
    content: makeContent(),
    operations: {},
    gates: {},
    artifacts: {},
    next_action: {
      type: "generate_videos",
      args: {},
      requested_ids: [],
      requires_confirmation: false,
      reason: "next",
    },
    next_alternatives: [],
    ...overrides,
  };
}

export function makePlan(overrides: Partial<WorkflowPlan> = {}): WorkflowPlan {
  const status = overrides.status ?? makeStatus();
  return {
    schema_version: 2,
    status,
    steps: [makeStep()],
    blockers: status.blockers,
    problems: [],
    next_action: status.next_action,
    next_alternatives: status.next_alternatives,
    ...overrides,
  };
}

/** 工厂视觉分镜；视频提示词在参考素材确认后单独编写。 */
export function makeDramaScene(overrides: Partial<DramaScene> = {}): DramaScene {
  return {
    scene_id: "E1S01",
    duration_seconds: 8,
    segment_break: false,
    characters_in_scene: [],
    scenes: [],
    props: [],
    utterances: [],
    scene_description: "雨夜街道",
    image_prompt: "雨夜街道",
    video_prompt: null,
    ...overrides,
  };
}

/** 一条旁白分镜；默认两侧提示词都是文本形态，各用例按需覆盖为结构化或待编写（null）。 */
export function makeNarrationSegment(overrides: Partial<NarrationSegment> = {}): NarrationSegment {
  return {
    segment_id: "E1S01",
    episode: 1,
    duration_seconds: 8,
    segment_break: false,
    novel_text: "旁白正文",
    characters_in_segment: [],
    scenes: [],
    props: [],
    image_prompt: "雨夜街道",
    video_prompt: "撑伞走过",
    ...overrides,
  };
}
