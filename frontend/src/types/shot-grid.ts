export type ShotGridStatus = "draft" | "generating" | "completed" | "confirmed" | "failed";

/** 弹窗开放的宫格规格（ADR 0098 D7 / 0099 D-1）。 */
export type ShotGridLayout = "2x2" | "2x3" | "3x3";

export interface ShotGridCell {
  index: number;
  prompt: string;
  time_ratio: number;
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
  /** 逐格镜头文本（`第N格（景别）：正文`），出图的唯一输入。 */
  prompt: string;
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

export interface SaveShotGridPayload {
  script_file: string;
  episode: number;
  shot_id: string;
  rows: number;
  cols: number;
  prompt: string;
}
