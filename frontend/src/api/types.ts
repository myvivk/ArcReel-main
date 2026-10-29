/**
 * API 共享类型：请求载荷、查询筛选与跨端点复用的响应结构。
 * 对外经 `@/api` 再导出，调用方继续从 `@/api` 导入。
 */

import type {
  AssistantSessionResumedPayload,
  CreationMode,
  EpisodeScript,
  NarrationDelivery,
  ProjectChangeBatchPayload,
  ProjectData,
  ProjectDeletedPayload,
  ProjectEventSnapshotPayload,
} from "@/types";
import type { SseStreamError } from "@/utils/sse-stream";

/** 项目内四类资产（与后端 ASSET_SPECS 的 asset_type 对齐）。 */
export type ProjectAssetType = "character" | "scene" | "prop";

/** 资产级联重命名的影响报告（dry_run 预览与执行同一结构）。 */
export interface AssetRenameResult {
  success: boolean;
  dry_run: boolean;
  old_name: string;
  new_name: string;
  episodes: number;
  references: number;
  files: number;
}

/** 删除资产前的引用预览（`DELETE …?dry_run=true`）：与重命名同一套扫描，按集号升序列出。 */
export interface AssetDeletionPreview {
  success: boolean;
  dry_run: true;
  name: string;
  /** 全部引用处数；个别引用归不到某一集时，会多于各集之和。 */
  references: number;
  episodes: { episode: number; references: number }[];
}

/** Login response from POST /auth/token (mirrors backend TokenResponse). */
export interface LoginResponse {
  access_token: string;
  token_type: string;
}

export type ScriptEditOperation =
  | { op: "update"; id: string; fields: Record<string, unknown> }
  | { op: "insert_after"; after_id: string | null; item: Record<string, unknown> }
  | { op: "move_after"; id: string; after_id: string | null }
  | { op: "remove"; id: string };

export interface ScriptEditCommand {
  script?: string;
  episode?: number;
  expected_revision: string;
  operations: ScriptEditOperation[];
}

export interface EpisodeScriptSnapshot {
  script: EpisodeScript;
  revision: string;
}

/** Version metadata returned by the versions API. */
export interface VersionInfo {
  version: number;
  filename: string;
  created_at: string;
  file_size: number;
  is_current: boolean;
  /** Whether this history record carries verified provenance for restore. */
  restorable?: boolean;
  /** Whether the shared presentation reader can preview/export this video version. */
  presentation_available?: boolean;
  file_url?: string;
  prompt?: string;
  restored_from?: number;
  /** 版本来源标记；"manual_upload" 表示用户手动上传 */
  source?: string;
}

/** 分镜/视频单元媒体上传的统一响应。 */
export interface ShotUploadResult {
  success: boolean;
  path: string;
  version: number;
  asset_fingerprints: Record<string, number>;
}

export interface ProjectEventStreamOptions {
  projectName: string;
  /** 每次建连（含断线重建）后服务端都会先发一次 snapshot。 */
  onSnapshot?: (payload: ProjectEventSnapshotPayload) => void;
  onChanges?: (payload: ProjectChangeBatchPayload) => void;
  /** 项目目录被删除后收到一次，随后服务端正常关流；订阅方应在此关闭句柄以停止自动重建。 */
  onProjectDeleted?: (payload: ProjectDeletedPayload) => void;
  /** 项目下某个 Agent 会话自主回到 running；当前会话经常驻 entry 流收到这一轮，此通知供会话列表等其他视图使用。 */
  onAssistantSessionResumed?: (payload: AssistantSessionResumedPayload) => void;
  /** 连接失败或中断；`retryable` 为 true 时客户端随后自动重建。 */
  onError?: (error: SseStreamError) => void;
}

export interface AssistantEntriesStreamOptions {
  projectName: string;
  sessionId: string;
  /** 冷订阅游标（seq）；断线重建的续传由 `Last-Event-ID` 承担。 */
  after?: number;
  onEvent: (event: string, payload: Record<string, unknown>) => void;
  onError?: (error: SseStreamError) => void;
}

/** Filters for {@link API.listTasks} and {@link API.listProjectTasks}. */
export interface TaskListFilters {
  projectName?: string;
  status?: string;
  taskType?: string;
  source?: string;
  page?: number;
  pageSize?: number;
}

/** {@link API.getUsageRecords} 的筛选；数组维度在查询串里逗号分隔。 */
export interface UsageRecordsQuery {
  /** 空串筛选端点试跑记录，`undefined` 表示不按项目筛。 */
  projectName?: string;
  providers?: readonly string[];
  models?: readonly string[];
  mediaTypes?: readonly string[];
  statuses?: readonly string[];
  segmentIds?: readonly string[];
  /** ISO 8601 时刻，半开区间 [since, until)，作用于 started_at。 */
  since?: string;
  until?: string;
  limit?: number;
  /** 上一页返回的不透明游标。 */
  cursor?: string;
}

/** {@link API.getUsageSummary} 的筛选；不收状态，pending 不进聚合。 */
export interface UsageSummaryQuery {
  /** 空串筛选端点试跑记录，`undefined` 表示不按项目筛。 */
  projectName?: string;
  provider?: string;
  model?: string;
  mediaType?: string;
  since?: string;
  until?: string;
  /** IANA 时区名，按此切天；缺省 UTC。 */
  tz?: string;
}

/** Generic success response used by many endpoints. */
export interface SuccessResponse {
  success: boolean;
  message?: string;
}

export interface AgentProfileStatus {
  customized: boolean;
  customized_files: string[];
}

/** Payload for {@link API.createProject}. */
export interface CreateProjectPayload {
  title: string;
  name?: string;
  content_mode?: "drama";
  aspect_ratio?: "9:16" | "16:9";
  generation_mode?: "storyboard";
  /** 创作模式；缺省由服务端按 AI 原创处理。 */
  creation_mode?: CreationMode;
  /** 口播语速估算（阅读单位 / 秒）；留空即按项目语言的默认速度估算。 */
  speech_rate_units_per_second?: number | null;
  default_duration?: number | null;
  /** 单集目标时长（秒）；未设即不传。 */
  episode_target_duration?: number | null;
  total_episodes?: number | null;
  style_template_id?: string | null;
  video_backend?: string | null;
  image_backend?: string | null;
  /** 项目默认图片模型。创建向导只暴露默认层（docs/adr/0054），任务类型桶留给项目设置页。 */
  default_image_backend?: string | null;
  text_backend_simple?: string | null;
  text_backend_complex?: string | null;
  default_text_backend?: string | null;
  model_settings?: Record<string, { resolution?: string | null }>;
  /** 旁白交付方式；服务端缺省后期配音。 */
  narration_delivery?: NarrationDelivery;
  /** TTS 快照（仅 use_tts）：省略的字段由服务端以全局默认预填。 */
  audio_backend?: string;
  narration_voice?: string;
  narration_speed?: number | null;
}

export interface VideoCapabilitiesQuery {
  signal?: AbortSignal;
  videoBackend?: string;
  /** undefined = 服务端按项目已保存档位；null = 显式「自动」（发空串）；字符串 = 按该档位。 */
  resolution?: string | null;
  usesReferenceImages?: boolean;
}

/** {@link API.createProject} 的响应；`first_episode` 只在导入原文模式创建时由服务端自动建集后回带。 */
export interface CreateProjectResponse {
  success: boolean;
  name: string;
  project: ProjectData;
  first_episode?: number;
}

/** 工厂机检的一条告警：导入原文不阻塞落盘，只在剧本页展示。 */
export interface SourceImportQualityWarning {
  code: string;
  message: string;
  line?: number;
}

/** {@link API.importSourceScript} 的响应：生成的源稿与机检告警。 */
export interface SourceImportResult {
  episode: number;
  /** 源稿相对路径，如 `source/short_drama/episodes/episode_01.md`。 */
  path: string;
  revision: string;
  markdown: string;
  /** 机检未通过项；结构校验失败会直接报错，不会出现在这里。 */
  quality_warnings: SourceImportQualityWarning[];
  /** 生成剧本所用的文本模型；未配置时缺省。 */
  model?: { provider: string; model: string };
  /** 非致命提示，如「本集资产尚未登记」。 */
  warnings?: string[];
}

/** {@link API.getSourceImportOriginal} 的响应：本集已保存的原文与同步状态。 */
export interface SourceImportOriginal {
  text: string;
  /** 当前原文 revision；还没有原文时为 null。 */
  revision: string | null;
  /** 生成剧本时所用原文 revision，用来判断原文改动后剧本是否已过期。 */
  generated_from_revision: string | null;
  /** 原文已改、剧本未重新生成。 */
  stale: boolean;
  exists: boolean;
}

/** {@link API.putSourceImportOriginal} 的响应：只落盘原文，不重生成剧本。 */
export interface SourceImportOriginalSaved {
  revision: string;
  stale: boolean;
}
