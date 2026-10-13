export interface StoryRecord {
  id: string;
  kind: "relationship" | "event" | "fact";
  text: string;
  characters: string[];
  episode: number | null;
  status: "pending" | "confirmed";
  source_file: string | null;
  source_id: string | null;
  source_revision: string | null;
}

export interface StoryPlan {
  premise: string;
  records: StoryRecord[];
  context_budget: number;
}

export interface StorySnapshot {
  plan: StoryPlan;
  revision: string;
  stale_ids: string[];
}
