/**
 * 短剧工厂动作层：与生成类入队不同，源稿重建是同步写盘（无任务队列），故此处只负责
 * 发请求、弹提示、透传结果，不占用 tasks-store 的乐观标记。批量建空白集复用已有的
 * ``POST /episodes`` 逐集创建。
 */
import { API } from "@/api";
import i18n from "@/i18n";
import { useAppStore } from "@/stores/app-store";
import { useTasksStore } from "@/stores/tasks-store";
import { promptAuthoringResourceId } from "@/actions/generation";
import { errMsg } from "@/utils/async";

export interface ShortDramaSource {
  episode: number;
  path: string;
  markdown: string;
  revision: string | null;
  exists: boolean;
  /** 本集源稿引用的资产里缺外观描述的清单；非空时分镜入口置灰，先去补齐。 */
  assets_missing_description?: Array<{ type: string; name: string; reason: string }>;
}

/** 从已保存的 Markdown 源稿机械重建分镜结构；整体机械分镜，或只重切所选分镜。 */
export async function reimportShortDramaSource(
  projectName: string,
  episode: number,
  sourceRevision: string,
  confirmArtifacts: boolean,
  forceRegenerate = false,
  regenerateSceneIds?: string[],
  options: { signal?: AbortSignal } = {},
): Promise<{
  episode: number;
  scenes: number;
  source_path: string;
  source_revision: string;
  mode?: string;
  splitter_version?: number;
  pending_authoring?: number;
  reused?: boolean;
  warnings?: Array<{ code: string; scene_id: string; detail: string; candidates: string[] }>;
  declared_seconds?: number;
  actual_seconds?: number;
  duration_shortfall?: boolean;
  regenerated_scenes?: number;
  /** 历史响应字段，只为旧调用方与测试桩兼容；服务端不再返回。 */
  planning_strength?: number;
  planning_cached?: boolean;
  ai_authoring?: boolean;
  assets_without_description?: Array<{ type: string; name: string; reason: string }>;
}> {
  const mark = useTasksStore.getState().beginOptimisticActive(
    projectName, "text_episode_script", promptAuthoringResourceId(episode), "text_episode_script",
  );
  try {
    options.signal?.throwIfAborted();
    const result = await API.reimportShortDramaSource(
      projectName,
      episode,
      sourceRevision,
      confirmArtifacts,
      forceRegenerate,
      regenerateSceneIds,
      options,
    );
    options.signal?.throwIfAborted();
    return result;
  } catch (err) {
    if (!options.signal?.aborted) useAppStore.getState().pushToast(errMsg(err), "error");
    throw err;
  } finally {
    mark.rollback();
  }
}

/**
 * 批量建空白集：在播出顺序末尾依次创建 `count` 个空集，返回新建的集 ID 列表。
 * 逐集调用已有的 ``POST /episodes``（每集独立落账本），数量大时调用次数较多但实现最简。
 */
export async function createBlankEpisodes(projectName: string, count: number, options: { signal?: AbortSignal } = {}): Promise<number[]> {
  const created: number[] = [];
  try {
    for (let i = 0; i < count; i++) {
      options.signal?.throwIfAborted();
      const res = await API.createEpisode(projectName, { after: created.length > 0 ? created[created.length - 1] : null }, options);
      options.signal?.throwIfAborted();
      created.push(res.episode);
    }
    useAppStore
      .getState()
      .pushToast(i18n.t("dashboard:short_drama_created_blank", { count: created.length }), "success");
    return created;
  } catch (err) {
    if (!options.signal?.aborted) useAppStore.getState().pushToast(errMsg(err), "error");
    throw err;
  }
}
