export type ShotGridStatus = "draft" | "generating" | "completed" | "confirmed" | "failed";

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
  composite_path: string | null;
  composite_url?: string;
  status: ShotGridStatus;
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
  cells: Array<{ index: number; prompt: string }>;
}
