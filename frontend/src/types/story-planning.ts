export interface ShortDramaArtifactView {
  path: string;
  revision: string;
  kind: string;
  episode: number | null;
  content: string;
}

export interface StoryPlanningArtifacts {
  character_bible: ShortDramaArtifactView | null;
  series_outline: ShortDramaArtifactView | null;
  ledger: ShortDramaArtifactView | null;
  episode_plans: ShortDramaArtifactView[];
}
