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
  composite_path: string | null;
  composite_url?: string;
  status: ShotGridStatus;
  task_id?: string | null;
  created_at: string;
  updated_at: string;
  error_message: string | null;
}

/** PUT 只存规格：逐格文本由服务端在出图时从 `image_prompt.scene` 现解析（D-3 / D-4）。 */
export interface SaveShotGridPayload {
  script_file: string;
  episode: number;
  shot_id: string;
  rows: number;
  cols: number;
}
