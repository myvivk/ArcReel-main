/**
 * 项目创作模式判定：只决定入口与剧本来源（AI 原创 / 导入原文），不参与内容类型与生成路线。
 * 存量项目没有 `creation_mode`，读侧按 AI 原创兜底（与后端迁移前的读侧一致）。
 */
import type { ProjectData } from "@/types";

/** 单集原文上限（字符数）：与后端 `source_import_text_too_long` 同一口径（5 万字）。 */
export const SOURCE_IMPORT_MAX_CHARS = 50_000;

/** 导入原文项目：剧本由用户原文改写，不提供故事规划与 AI 原创剧本入口。 */
export function isSourceImport(project: Pick<ProjectData, "creation_mode"> | null | undefined): boolean {
  return project?.creation_mode === "source_import";
}
