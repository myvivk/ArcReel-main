/**
 * Reference-to-video unit types — mirrors lib/script/script_models.py Pydantic models.
 *
 * One "unit" produces one rendered video clip. Its body (`text`) is the single
 * source of truth: reference images are resolved from the `@[名称]` mentions at
 * execution time and never persisted or transported.
 */

import type {
  AdmissionProblem,
} from "./workflow";

export type AssetKind = "product" | "character" | "scene" | "prop";

/** Project.json sheet field for each asset kind. Mirrors lib/project/asset_types.py SHEET_KEY. */
export const SHEET_FIELD: Record<AssetKind, "product_sheet" | "character_sheet" | "scene_sheet" | "prop_sheet"> = {
  product: "product_sheet",
  character: "character_sheet",
  scene: "scene_sheet",
  prop: "prop_sheet",
};

export interface ReferenceProjectionLocation {
  path: (string | number)[];
  line: number | null;
}

export interface ReferenceProjectionProblem {
  code: string;
  blocking: boolean;
  unit_id: string;
  locations: ReferenceProjectionLocation[];
  params: Record<string, unknown>;
  reason?: string;
  action: string;
  message?: string;
}

export interface ReferenceProjectionAdmission {
  allowed: false;
  kind: "reference_request_projection";
  unit_id: string;
  problems: ReferenceProjectionProblem[];
}

/**
 * 一个没能入队的目标。已创建的任务不因此被撤销，它们照常执行；这里列出的 unit
 * 本次没有任务、也没有计费，下次「缺失即生成」会正好补上它们。
 */
export interface ReferenceBatchEnqueueFailure {
  unit_id: string;
  problem: AdmissionProblem;
}
