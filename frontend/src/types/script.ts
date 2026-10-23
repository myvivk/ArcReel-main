/**
 * Script / segment / scene type definitions.
 *
 * Maps to backend models in:
 * - lib/script/script_models.py (NarrationSegment, DramaScene, ImagePrompt, VideoPrompt, etc.)
 */

export const SHOT_TYPES = [
  "Extreme Close-up",
  "Close-up",
  "Medium Close-up",
  "Medium Shot",
  "Medium Long Shot",
  "Long Shot",
  "Extreme Long Shot",
  "Over-the-shoulder",
  "Point-of-view",
] as const;

export type ShotType = (typeof SHOT_TYPES)[number];

export const CAMERA_MOTIONS = [
  "Static",
  "Pan Left",
  "Pan Right",
  "Tilt Up",
  "Tilt Down",
  "Zoom In",
  "Zoom Out",
  "Push In",
  "Pull Out",
  "Truck Left",
  "Truck Right",
  "Pedestal Up",
  "Pedestal Down",
  "Orbit",
  "Tracking Shot",
  "Shake",
] as const;

export type CameraMotion = (typeof CAMERA_MOTIONS)[number];

export type DurationSeconds = number;
export type AssetStatus = "pending" | "storyboard_ready" | "completed";

export interface Dialogue {
  speaker: string;
  line: string;
}

export type UtteranceKind = "dialogue" | "voiceover";

/**
 * Drama 分镜级有序发声条目，判别式联合（discriminated union）按 kind 收窄，把 kind ⇄ speaker
 * 约束编码进类型：dialogue 必带非空 speaker、voiceover 不得带 speaker。非法组合（dialogue 缺
 * speaker、voiceover 带 speaker）编译期即被拒，与后端 Utterance 契约一致。
 */
export interface DialogueUtterance {
  kind: "dialogue";
  /** 角色台词必带非空说话人。 */
  speaker: string;
  text: string;
}

export interface VoiceoverUtterance {
  kind: "voiceover";
  /** 无说话人画外音：speaker 恒为 null 或缺省。 */
  speaker?: null;
  text: string;
}

export type Utterance = DialogueUtterance | VoiceoverUtterance;

/** 本集新增资产的类型：只有角色、场景、道具会在规划里新增。 */
export type NewAssetType = "character" | "scene" | "prop";

/** 新增项的处理决定：登记为新资产 / 归到已有资产 / 登记为角色衍生 / 不登记。 */
export type NewAssetDecision = "register" | "merge" | "derivative" | "skip";

/**
 * 脚本规划带出的一项本集新增资产。映射后端 lib/script/script_models.py 的 PlanNewAsset：
 * 规划条目里的引用写 `name`，确认时按 `decision` 改写并登记。
 */
export interface PlanNewAsset {
  type: NewAssetType;
  /** 规划条目里写的称呼。 */
  name: string;
  decision: NewAssetDecision;
  /** AI 给出的一句依据。 */
  reason: string;
  /** register：外观描述；derivative：相对本体的变化描述。 */
  description: string;
  /** register：原文中的其他称呼。 */
  aliases: string[];
  /** merge：归入的同类资产名或本集另一新增项的称呼；derivative：本体角色名。 */
  target: string;
  /** register：登记名；derivative：衍生名；为空时取 `name`。 */
  asset_name: string;
}

export interface Composition {
  shot_type: ShotType;
  lighting: string;
  ambiance: string;
}

export interface ImagePrompt {
  scene: string;
  composition: Composition;
}

export interface VideoPromptShot {
  title?: string;
  timeline?: string;
  shot_size?: string;
  lens?: string;
  angle?: string;
  composition?: string;
  references?: string[];
  description?: string;
  camera_motion?: string;
  audio?: string;
}

export interface VideoPrompt {
  action: string;
  camera_motion: CameraMotion;
  ambiance_audio: string;
  dialogue: Dialogue[];
  shot_sequence?: VideoPromptShot[];
}

export interface GeneratedAssets {
  storyboard_image: string | null;
  storyboard_last_image: string | null;  // grid mode last frame
  storyboard_grid_images?: string[];     // complete ordered K0..Kn references
  grid_id: string | null;                // source grid ID
  grid_cell_index: number | null;        // cell index in source grid
  video_clip: string | null;
  video_thumbnail: string | null;
  video_uri: string | null;
  narration_audio?: string | null;       // narration audio file path
  status: AssetStatus;
}

export interface NarrationSegment {
  segment_id: string;
  episode: number;
  duration_seconds: DurationSeconds;
  segment_break: boolean;
  novel_text: string;
  characters_in_segment: string[];
  scenes?: string[];
  props?: string[];
  /** `null` = 待编写：内容确认转出的正式脚本只有内容层，提示词尚未编写。 */
  image_prompt: ImagePrompt | string | null;
  video_prompt: VideoPrompt | string | null;
  note?: string;
  /**
   * 尾帧快照路径（项目内相对路径）。视频从分镜图开场、过渡到这张图收尾。
   * 只由 /end-frame 的设置/清除端点写入——通用剧本 PATCH 刻意不接受该字段，
   * 避免绕过快照复制写出悬空引用（见 server/routers/end_frames.py）。
   */
  end_frame_image?: string | null;
  generated_assets?: GeneratedAssets;
  /** 待编写：视觉层尚未由提示词编写补出。新增条目时置位、提示词编写写回后清除，只读。 */
  pending_authoring?: boolean;
}

export interface DramaScene {
  scene_id: string;
  duration_seconds: DurationSeconds;
  segment_break: boolean;
  characters_in_scene: string[];
  scenes?: string[];
  props?: string[];
  /** 画面描述：动作行与神态的转写（提示词编写的视觉基底）。 */
  scene_description?: string;
  /** `null` = 待编写：内容确认转出的正式脚本只有内容层，提示词尚未编写。 */
  image_prompt: ImagePrompt | string | null;
  video_prompt: VideoPrompt | string | null;
  /**
   * 分镜级有序发声序列：角色台词与画外音按时序排列。新结构（drama）；
   * 存量 drama 走后端读时迁移，前端读到时此字段可能缺省。
   */
  utterances?: Utterance[];
  /** 对应原文：内容确认时从脚本规划透传，时间线只读；手动新增的分镜为空或缺省。 */
  source_text?: string;
  note?: string;
  /**
   * 尾帧快照路径（项目内相对路径）。视频从分镜图开场、过渡到这张图收尾。
   * 只由 /end-frame 的设置/清除端点写入——通用剧本 PATCH 刻意不接受该字段，
   * 避免绕过快照复制写出悬空引用（见 server/routers/end_frames.py）。
   */
  end_frame_image?: string | null;
  generated_assets?: GeneratedAssets;
  /** 待编写：视觉层尚未由提示词编写补出。新增条目时置位、提示词编写写回后清除，只读。 */
  pending_authoring?: boolean;
}

/** Novel source information (present in both episode script types). */
export interface NovelInfo {
  title: string;
  chapter: string;
}

export interface NarrationEpisodeScript {
  episode: number;
  title: string;
  content_mode: "narration";
  schema_version?: number;
  novel: NovelInfo;
  segments: NarrationSegment[];
}

export interface DramaEpisodeScript {
  episode: number;
  title: string;
  content_mode: "drama";
  schema_version?: number;
  /** 历史原生剧本的来源信息；工厂视觉状态不产生此字段。 */
  novel?: NovelInfo;
  scenes: DramaScene[];
  /**
   * 工厂 AI 分镜的源稿版本、路径与规划选项。
   */
  short_drama_meta?: Record<string, unknown>;
}

/** 广告/短片分镜（平铺 shots[]，口播文案一等）。 */
export interface AdShot {
  shot_id: string;
  /** 带货框架段落标签（hook/pain_point/... 八值引导，不硬枚举）。 */
  section: string;
  duration_seconds: DurationSeconds;
  /** 口播文案，字幕导出与后续配音的唯一来源。 */
  voiceover_text: string;
  characters_in_shot?: string[];
  scenes?: string[];
  props?: string[];
  /** 商品名称引用，非空即商品分镜。 */
  products_in_shot?: string[];
  /** 待编写分镜（手动新增）为 null，由提示词编写补出。 */
  image_prompt: ImagePrompt | string | null;
  video_prompt: VideoPrompt | string | null;
  note?: string;
  /**
   * 尾帧快照路径（项目内相对路径）。视频从分镜图开场、过渡到这张图收尾。
   * 只由 /end-frame 的设置/清除端点写入——通用剧本 PATCH 刻意不接受该字段，
   * 避免绕过快照复制写出悬空引用（见 server/routers/end_frames.py）。
   */
  end_frame_image?: string | null;
  generated_assets?: GeneratedAssets;
  /** 待编写：视觉层尚未由提示词编写补出。新增条目时置位、提示词编写写回后清除，只读。 */
  pending_authoring?: boolean;
}

export interface AdEpisodeScript {
  episode: number;
  title: string;
  content_mode: "ad";
  schema_version?: number;
  novel: NovelInfo;
  shots: AdShot[];
}

export type EpisodeScript = NarrationEpisodeScript | DramaEpisodeScript | AdEpisodeScript;

/** 提示词编写的请求：范围（省略为全部待编写）、是否显式重写、附加指令与覆盖令牌。 */
export interface AuthorPromptsRequest {
  entry_ids?: string[] | null;
  rewrite?: boolean;
  fields?: Array<"image_prompt" | "video_prompt"> | null;
  instructions?: string | null;
  overwrite_revision?: string | null;
}

/** 显式重写将覆盖的已有视觉层内容；`text` 是服务端渲染的丢失清单，`revision` 是认可令牌。 */
export interface PromptOverwrite {
  revision: string | null;
  entries: { id: string; fields: string[] }[];
  text: string;
}

export interface AuthorPromptsResponse {
  batch: { batch_id: string; members: { unit_id: string; task_id: string | null; deduped?: boolean }[] };
}

/**
 * 一侧提示词的最终渲染结果。`text` 与 `unavailable` 恰有一个非 null；
 * `unavailable` 已是后端按请求语言渲染的成品文案，前端不再二次翻译。
 */
export interface RenderedPromptPreview {
  text: string | null;
  unavailable: string | null;
  /** 该条目的这一侧提示词当前是文本形态。 */
  is_text_form: boolean;
  /** 渲染这份文本时产生的提示（如参考图超出后端上限被裁剪），已由后端按请求语言渲染成成品文案。 */
  warnings: string[];
}

/** 条目最终提示词预览：与执行期同一渲染出口，逐字等于实际发给模型的文本。 */
export interface ItemPromptPreview {
  item_id: string;
  content_mode: "narration" | "drama" | "ad";
  storyboard_image: RenderedPromptPreview;
  video: RenderedPromptPreview;
}
