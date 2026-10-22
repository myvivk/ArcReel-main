import { useEffect, useState } from "react";
import { API } from "@/api";
import type { ShortDramaSource } from "@/actions/shortDrama";
import { useProjectsStore } from "@/stores/projects-store";
import { errMsg } from "@/utils/async";

/** 工厂身份来自实际源稿；项目更新后重新读取，不把空白集或普通 OWN 原文当作工厂剧本。 */
export function useFactoryEpisodeSource(projectName: string, episode: number, enabled: boolean) {
  const revision = useProjectsStore((s) => s.projectSnapshotRevisions[projectName] ?? 0);
  const [attempt, setAttempt] = useState(0);
  const scopeKey = `${projectName}:${episode}`;
  const requestKey = `${scopeKey}:${revision}:${attempt}`;
  const [result, setResult] = useState<{
    key: string;
    scope: string;
    value: ShortDramaSource | null;
    error: string | null;
  }>({ key: "", scope: "", value: null, error: null });

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    API.getShortDramaSource(projectName, episode, { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) setResult({ key: requestKey, scope: scopeKey, value, error: null });
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) setResult({ key: requestKey, scope: scopeKey, value: null, error: errMsg(cause) });
      });
    return () => controller.abort();
  }, [enabled, projectName, episode, scopeKey, requestKey]);

  const current = enabled && result.scope === scopeKey;
  const value = current ? result.value : null;
  return {
    value,
    error: current ? result.error : null,
    loading: enabled && result.key !== requestKey,
    exists: Boolean(value?.exists && value.path === `source/short_drama/episodes/episode_${String(episode).padStart(2, "0")}.md`),
    reload: () => setAttempt((value) => value + 1),
  };
}
