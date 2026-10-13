import { describe, expect, it } from "vitest";

import { parseShortDramaBatch } from "./ShortDramaImportDialog";

describe("parseShortDramaBatch", () => {
  it("splits the native short-drama-factory episode heading format", () => {
    expect(
      parseShortDramaBatch(`第【1】集：第一集
△ 第一集内容

第【2】集：第二集
△ 第二集内容`),
    ).toEqual([
      { episode_id: 1, markdown: "第【1】集：第一集\n△ 第一集内容" },
      { episode_id: 2, markdown: "第【2】集：第二集\n△ 第二集内容" },
    ]);
  });

  it("keeps accepting plain numbered Markdown headings", () => {
    expect(parseShortDramaBatch("# 第 3 集 番外\n△ 内容")).toEqual([
      { episode_id: 3, markdown: "# 第 3 集 番外\n△ 内容" },
    ]);
  });
});
