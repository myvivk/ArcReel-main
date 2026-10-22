import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import type { ShortDramaSource } from "@/actions/shortDrama";
import { useProjectsStore } from "@/stores/projects-store";
import { createDeferred } from "@/test/deferred";
import { useFactoryEpisodeSource } from "./useFactoryEpisodeSource";

const source: ShortDramaSource = {
  episode: 1, path: "source/short_drama/episodes/episode_01.md",
  markdown: "[人物]：林夏\n△ 开门。\n林夏：谁？", revision: "r1", exists: true,
};

describe("useFactoryEpisodeSource", () => {
  it.each([
    { ...source, exists: false },
    { ...source, path: "source/short_drama/episode_1.md" },
  ])("没有现行工厂文件时不把本集当作工厂集：$path / $exists", async (value) => {
    vi.spyOn(API, "getShortDramaSource").mockResolvedValue(value);
    const { result } = renderHook(() => useFactoryEpisodeSource("demo", 1, true));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.exists).toBe(false);
  });

  it("项目更新后重读原文，保留原始内容", async () => {
    const api = vi.spyOn(API, "getShortDramaSource").mockResolvedValue(source);
    const { result } = renderHook(() => useFactoryEpisodeSource("demo", 1, true));
    await waitFor(() => expect(result.current.exists).toBe(true));
    expect(result.current.value?.markdown).toBe(source.markdown);
    api.mockResolvedValue({ ...source, markdown: "更新后的原文", revision: "r2" });
    act(() => useProjectsStore.setState((s) => ({
      projectSnapshotRevisions: { ...s.projectSnapshotRevisions, demo: (s.projectSnapshotRevisions.demo ?? 0) + 1 },
    })));
    await waitFor(() => expect(result.current.value?.markdown).toBe("更新后的原文"));
  });

  it("切集取消旧请求，迟到响应不能覆盖当前集", async () => {
    const pending = createDeferred<ShortDramaSource>();
    let firstSignal: AbortSignal | undefined;
    vi.spyOn(API, "getShortDramaSource").mockImplementation((_name, episode, options) => {
      if (episode === 1) {
        firstSignal = options?.signal;
        return pending.promise;
      }
      return Promise.resolve({ ...source, episode: 2, path: "source/short_drama/episodes/episode_02.md", markdown: "第二集" });
    });
    const { result, rerender } = renderHook(({ episode }) => useFactoryEpisodeSource("demo", episode, true), { initialProps: { episode: 1 } });
    rerender({ episode: 2 });
    await waitFor(() => expect(result.current.value?.markdown).toBe("第二集"));
    expect(firstSignal?.aborted).toBe(true);
    await act(async () => pending.resolve(source));
    expect(result.current.value?.markdown).toBe("第二集");
  });

  it("失败后可以重试读取", async () => {
    vi.spyOn(API, "getShortDramaSource").mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(source);
    const { result } = renderHook(() => useFactoryEpisodeSource("demo", 1, true));
    await waitFor(() => expect(result.current.error).toBe("offline"));
    act(() => result.current.reload());
    await waitFor(() => expect(result.current.exists).toBe(true));
    expect(result.current.error).toBeNull();
  });
});
