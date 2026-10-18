export type ShotGridStatus = "draft" | "generating" | "completed" | "confirmed" | "failed";

/** 开放的宫格规格（D-1）：2×2（默认）/ 2×3 / 3×3。 */
export type ShotGridLayout = "2x2" | "2x3" | "3x3";

export interface ShotGridCell {
  index: number;
  /** 本次出图实际使用的逐格文本，只作留证，不可编辑。 */
  prompt: string;
  time_ratio: number;
  /** 景别（全景 / 中景 / 特写…）。 */
  continuity_notes: string;
  image_path: string | null;
  confirmed: boolean;
}

export interface ShotGridPlan {
  id: string;
  episode: number;
  script_file: string;
  shot_id: string;
  rows: number;
  cols: number;
  cells: ShotGridCell[];
  /** 逐格镜头文本（`第N格（景别）：正文`），出图的优先输入；为空时回退到 scene 现解析。 */
  prompt?: string;
  /** 出图时 `image_prompt.scene` 的指纹；与当前 scene 不一致时提示重新生成。 */
  scene_revision?: string | null;
  composite_path: string | null;
  composite_url?: string;
  status: ShotGridStatus;
  task_id?: string | null;
  created_at: string;
  updated_at: string;
  error_message: string | null;
}

/** PUT 保存规格与逐格镜头文本；`prompt` 为 `null` 时保持原值（D-4）。 */
export interface SaveShotGridPayload {
  script_file: string;
  episode: number;
  shot_id: string;
  rows: number;
  cols: number;
  prompt: string | null;
}

/**
 * 草稿保存：关弹窗 / AI 生成返回时把输入框里的文本留下（D-5）。
 * 只写逐格文本，不动生成状态；`rows/cols` 只在记录尚不存在时用于新建。
 */
export interface SaveShotGridDraftPayload {
  script_file: string;
  episode: number;
  shot_id: string;
  rows: number;
  cols: number;
  prompt: string;
}
