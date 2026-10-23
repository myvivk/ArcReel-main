import { describe, expect, it } from "vitest";
import { episodesViewPath } from "./episodes-view-model";

describe("episodesViewPath", () => {
  it("保留稳定集号定位和集账本创建参数", () => {
    expect(episodesViewPath()).toBe("/episodes");
    expect(episodesViewPath({ episode: 7 })).toBe("/episodes?episode=7");
    expect(episodesViewPath({ create: true })).toBe("/episodes?create=1");
  });
});
