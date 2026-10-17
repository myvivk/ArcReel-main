/**
 * 短剧工厂导入动作层：与生成类入队不同，短剧导入是同步写盘（无任务队列），故此处只负责
 * 发请求、弹提示、透传结果，不占用 tasks-store 的乐观标记。批量建空白集复用已有的
 * ``POST /episodes`` 逐集创建。
 */
import { API } from "@/api";
import i18n from "@/i18n";
import { useAppStore } from "@/stores/app-store";
import { errMsg } from "@/utils/async";

export interface ShortDramaEpisodeInput {
  episode_id: number;
  markdown: string;
}

export interface ShortDramaImportDetail {
  episode: number;
  ok: boolean;
  scenes?: number;
  error?: string;
  /** 声明时长与落盘实得时长不一致时由后端回执。 */
  declared_seconds?: number;
  actual_seconds?: number;
  duration_shortfall?: boolean;
  registered_assets?: Record<string, string[]>;
  asset_conflicts?: Array<Record<string, string>>;
}

export interface ShortDramaImportResult {
  imported: number;
  failed: number;
  details: ShortDramaImportDetail[];
}

export interface ShortDramaArtifactInput {
  path: string;
  content_base64: string;
}

export async function importShortDramaArtifacts(
  projectName: string,
  artifacts: ShortDramaArtifactInput[],
): Promise<{ artifacts: Array<{ path: string; revision: string; kind: string; episode: number | null }> }> {
  try {
    const result = await API.importShortDramaArtifacts(projectName, artifacts);
    useAppStore.getState().pushToast(i18n.t("dashboard:short_drama_imported_artifacts", { count: result.artifacts.length }), "success");
    return result;
  } catch (err) {
    useAppStore.getState().pushToast(errMsg(err), "error");
    throw err;
  }
}

export interface ShortDramaSource {
  episode: number;
  path: string;
  markdown: string;
  revision: string | null;
  exists: boolean;
}

export interface ShortDramaImportOneResult {
  episode: number;
  scenes: number;
  declared_seconds?: number;
  actual_seconds?: number;
  duration_shortfall?: boolean;
  registered_assets?: Record<string, string[]>;
  asset_conflicts?: Array<Record<string, string>>;
}

/** 导入一集短剧工厂剧本，成功/失败均弹提示。 */
export async function importShortDramaScript(
  projectName: string,
  episodeId: number,
  markdown: string,
): Promise<ShortDramaImportOneResult> {
  try {
    const res = await API.importShortDramaScript(projectName, episodeId, markdown);
    if (res.duration_shortfall) {
      useAppStore
        .getState()
        .pushToast(
          i18n.t("dashboard:short_drama_imported_one_shortfall", {
            episode: res.episode,
            declared: res.declared_seconds ?? 0,
            actual: res.actual_seconds ?? 0,
          }),
          "warning",
        );
    } else {
      useAppStore
        .getState()
        .pushToast(
          i18n.t("dashboard:short_drama_imported_one", { episode: res.episode, scenes: res.scenes }),
          "success",
        );
    }
    return res;
  } catch (err) {
    useAppStore.getState().pushToast(errMsg(err), "error");
    throw err;
  }
}

/** 批量导入多集短剧工厂剧本，按导入/失败计数弹提示。 */
export async function importShortDramaSeries(
  projectName: string,
  episodes: ShortDramaEpisodeInput[],
  continueOnError = true,
): Promise<ShortDramaImportResult> {
  try {
    const res = await API.importShortDramaSeries(projectName, episodes, continueOnError);
    if (res.failed === 0) {
      useAppStore
        .getState()
        .pushToast(i18n.t("dashboard:short_drama_imported_batch", { imported: res.imported, failed: res.failed }), "success");
    } else {
      useAppStore
        .getState()
        .pushToast(i18n.t("dashboard:short_drama_imported_batch", { imported: res.imported, failed: res.failed }), "warning");
    }
    return res;
  } catch (err) {
    useAppStore.getState().pushToast(errMsg(err), "error");
    throw err;
  }
}

/** 从已保存的 Markdown 源稿重建正式脚本；视觉提示词与媒体仍由用户后续显式生成。 */
export async function reimportShortDramaSource(
  projectName: string,
  episode: number,
  sourceRevision: string,
  confirmArtifacts: boolean,
): Promise<{ episode: number; scenes: number; source_path: string; source_revision: string }> {
  try {
    const result = await API.reimportShortDramaSource(
      projectName,
      episode,
      sourceRevision,
      confirmArtifacts,
    );
    useAppStore
      .getState()
      .pushToast(
        i18n.t("dashboard:short_drama_source_reimported", {
          episode: result.episode,
          scenes: result.scenes,
        }),
        "success",
      );
    return result;
  } catch (err) {
    useAppStore.getState().pushToast(errMsg(err), "error");
    throw err;
  }
}

/**
 * 批量建空白集：在播出顺序末尾依次创建 `count` 个空集，返回新建的集 ID 列表。
 * 逐集调用已有的 ``POST /episodes``（每集独立落账本），数量大时调用次数较多但实现最简。
 */
export async function createBlankEpisodes(projectName: string, count: number): Promise<number[]> {
  const created: number[] = [];
  try {
    for (let i = 0; i < count; i++) {
      const res = await API.createEpisode(projectName, { after: created.length > 0 ? created[created.length - 1] : null });
      created.push(res.episode);
    }
    useAppStore
      .getState()
      .pushToast(i18n.t("dashboard:short_drama_created_blank", { count: created.length }), "success");
    return created;
  } catch (err) {
    useAppStore.getState().pushToast(errMsg(err), "error");
    throw err;
  }
}
