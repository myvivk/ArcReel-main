/** 项目资产图（本体与衍生）的状态、批量生成与重生影响。 */

import type { CostBreakdown } from "./cost";

export type AssetSheetType = "character" | "scene" | "prop";

/** 产物清单对一张资产图的判定：missing 即待生成（含登记了文件却读不到）。 */
export type AssetSheetStatus = "current" | "stale" | "missing" | "blocked";

export interface AssetSheetRef {
  /** `<类型>/<名字>`；衍生为 `<类型>/<本体>/<衍生>`。 */
  unit_id: string;
  asset_type: AssetSheetType;
  /** 本体名；衍生时是其本体的名字。 */
  name: string;
  derivative: string | null;
}

export interface AssetSheetStatusRow extends AssetSheetRef {
  status: AssetSheetStatus;
  description_missing: boolean;
  image_to_image: boolean;
}

/** 批量生成的范围：画廊类型页或集层，二选一。 */
export type AssetSheetBatchScope = { asset_type: AssetSheetType } | { episode_id: number };

export type AssetSheetSkipReason = "generating" | "missing_description" | "owner_sheet_missing" | "unavailable";

export interface AssetSheetBatchPreview {
  targets: (AssetSheetRef & { depends_on: string | null })[];
  skipped: (AssetSheetRef & { reason: AssetSheetSkipReason })[];
  estimated_cost: CostBreakdown | null;
}

export interface AssetSheetBatchMember extends AssetSheetRef {
  task_id: string | null;
  deduped: boolean;
  status: string;
  problem_code?: string | null;
}

export interface AssetSheetBatchSubmitted {
  batch_id: string;
  members: AssetSheetBatchMember[];
}

/** 一键的本批清单预估（只读、不落盘）：将处理多少资产、预计多少次文本 / 图像模型调用。 */
export interface EpisodeAssetPlan {
  total: number;
  by_type: Partial<Record<AssetSheetType, number>>;
  /** 描述为空、需要生成描述的资产数（AI 只填空字段，已有描述的不占调用）。 */
  text_calls: number;
  /** 资产图未就绪（缺图或过期）、将进生成队列的资产数。 */
  image_calls: number;
}

/** 本集引用到的已登记资产，与一键的本批清单预估。 */
export interface EpisodeReferencedAssets {
  episode: number;
  has_script: boolean;
  assets: { asset_type: AssetSheetType; asset_name: string; asset: Record<string, unknown> }[];
  plan: EpisodeAssetPlan;
}

/** 一键「抽取本集剧本引用 → 补登记 → 生成描述 → 生成图」的回执。 */
export interface EpisodeAssetGenerationResult extends AssetSheetBatchSubmitted {
  /** 本次补登记的资产名，按类型分组；已登记的一律不动。 */
  registered: Partial<Record<AssetSheetType, string[]>>;
  /** 名字被别的资产类型占用、没能登记的项。 */
  conflicts: { name: string; asset_type: AssetSheetType; reason: string }[];
  /** 本次按本集剧本补上描述的资产；已有描述的一律不动。 */
  description_filled: { asset_type: AssetSheetType; name: string }[];
  /** 补全调用失败（模型截断或调用异常）的资产，描述仍为空。 */
  description_fill_failed: { asset_type: AssetSheetType; name: string; reason: string }[];
  /** 描述没补上、进不了这一批生成的资产图。 */
  without_description: AssetSheetRef[];
}

export interface AssetRegenerationImpact {
  /** 服务端此刻对这张资产图的判定是否为过期。 */
  stale: boolean;
  storyboards: number;
  videos: number;
  derivatives: number;
}
