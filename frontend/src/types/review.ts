export interface ReviewDimension {
  id: string;
  score: number | null;
  max: number;
  findings: string[];
}

export interface ReviewDefect {
  id: string;
  severity: string;
  message: string;
}

export interface ReviewResult {
  score: number;
  max: number;
  coverage: number;
  verdict: string;
  dimensions: ReviewDimension[];
  defects: ReviewDefect[];
  advice: string[];
}

export interface StoryboardScoreRow extends ReviewResult {
  scene_id: string;
}

export interface StoryboardScoreResponse {
  episode: number;
  scenes: StoryboardScoreRow[];
}

export interface VideoReviewResponse extends ReviewResult {
  status: string;
  shot_id: string;
}

export interface ReferenceReviewResponse extends ReviewResult {
  status: string;
  shot_id: string;
}
