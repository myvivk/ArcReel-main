

/** 源文件类型：只有剧情演绎项目有。 */
export type SourceKind = "novel" | "screenplay";

export interface CreateEpisodeBody {
  /** 插在哪一集之后（集 ID）；缺省放在播出顺序末尾。 */
  after?: number | null;
  title?: string;
  hook?: string;
  /** 集原文；空白视同没有原文。 */
  source_text?: string | null;
  /** 集原文的源文件类型，只对剧情演绎项目生效。 */
  source_kind?: SourceKind | null;
}

/** 删除一集会丢失的内容。`text` 只在等待确认时出现，是服务端成文的确认文本。 */
export interface EpisodeDeletionImpact {
  episode: number;
  /** 删除不丢失任何无法在项目内重建的内容。 */
  recoverable: boolean;
  revision: string;
  text?: string;
}

export type EpisodeDeletionResponse =
  | { status: "deleted"; impact: EpisodeDeletionImpact }
  | { status: "confirmation_required"; impact: EpisodeDeletionImpact & { text: string } };

/** 批量删除等待确认时的服务端清单。 */
export interface EpisodeBatchDeletionPreview {
  episodes: number[];
  revision: string;
  recoverable: boolean;
  text: string;
}

export type EpisodeBatchDeletionResponse =
  | { status: "deleted"; episodes: number[] }
  | { status: "confirmation_required"; batch: EpisodeBatchDeletionPreview };
