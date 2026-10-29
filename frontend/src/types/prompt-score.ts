export interface VideoPromptScoreRow {
  scene_id: string;
  duration_seconds: number | null;
  action_chars: number;
  sentence_count: number;
  max_similarity: number;
  max_verbatim_run: number;
  static_density: number;
  warnings: string[];
  /** 未登记 / 没打 @ 的资产引用：问题与提示。 */
  mention_problems: string[];
  mention_warnings: string[];
  score: number;
  passed: boolean;
  problem: string | null;
  seam_problem: string | null;
}

export interface VideoPromptScoreResponse {
  episode: number;
  scenes: VideoPromptScoreRow[];
}
