import { describe, expect, it } from "vitest";
import type { EpisodeMeta } from "@/types";
import { trailingBlankEpisodes } from "./episode-blank";

function meta(episode: number, overrides: Partial<EpisodeMeta> = {}): EpisodeMeta {
  return { episode, title: `EP${episode}`, script_file: "", ...overrides };
}

describe("trailingBlankEpisodes", () => {
  it("账本未载入时没有可复用集", () => {
    expect(trailingBlankEpisodes(undefined)).toEqual([]);
  });

  it("全部为空时整本都是可复用集", () => {
    expect(trailingBlankEpisodes([meta(1), meta(2), meta(3)])).toEqual([1, 2, 3]);
  });

  it("只取末尾连续的一段空白集", () => {
    const list = [meta(1), meta(2, { script_status: "generated" }), meta(3), meta(4)];
    expect(trailingBlankEpisodes(list)).toEqual([3, 4]);
  });

  it("中段空白集不复用：填它会把新故事插进已有内容之间", () => {
    const list = [meta(1), meta(2, { script_status: "segmented" }), meta(3)];
    expect(trailingBlankEpisodes(list)).toEqual([3]);
  });

  it("自带原文的集不当作空白集", () => {
    const list = [meta(1, { source_origin: "own" }), meta(2, { source_range: { start: 0, end: 10 } })];
    expect(trailingBlankEpisodes(list)).toEqual([]);
  });

  it("末尾有内容时没有可复用集", () => {
    const list = [meta(1), meta(2, { script_status: "generated" })];
    expect(trailingBlankEpisodes(list)).toEqual([]);
  });
});
