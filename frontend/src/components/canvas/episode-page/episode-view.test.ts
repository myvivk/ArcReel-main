import { describe, expect, it } from "vitest";
import {
  defaultEpisodeView, episodeViewTabs, resolveEpisodeView, staysInEpisodeView, withEpisodeView,
  type EpisodeViewFacts,
} from "./episode-view";

const FACTORY: EpisodeViewFacts = { hasScript: true, hasShortDramaSource: true, demo: false };

describe("factory episode navigation", () => {
  it("offers the four factory views in production order", () => {
    expect(episodeViewTabs(FACTORY)).toEqual([
      { view: "source", disabled: false },
      { view: "assets", disabled: false },
      { view: "board", disabled: false },
      { view: "edit", disabled: false },
    ]);
  });

  it("opens source before storyboarding and enables assets from Markdown", () => {
    const source = { ...FACTORY, hasScript: false };
    expect(defaultEpisodeView(source)).toBe("source");
    expect(resolveEpisodeView("assets", source)).toBe("assets");
    expect(resolveEpisodeView("assets", { ...source, hasShortDramaSource: false })).toBe("source");
  });

  it.each(["plan", "setting", "grid", "unknown"])("removed URL view %s cannot restore an old workspace", (view) => {
    expect(resolveEpisodeView(view, FACTORY)).toBe("board");
    expect(resolveEpisodeView(view, { ...FACTORY, hasScript: false })).toBe("source");
  });

  it("keeps demo projects on the board", () => {
    const demo = { ...FACTORY, demo: true, hasScript: false };
    expect(episodeViewTabs(demo)).toEqual([{ view: "board", disabled: false }]);
    expect(defaultEpisodeView(demo)).toBe("board");
  });

  it("writes only non-default views without changing other params or the input", () => {
    const params = new URLSearchParams("unit=E1S2");
    expect(withEpisodeView(params, "source", FACTORY).toString()).toBe("unit=E1S2&view=source");
    expect(withEpisodeView(new URLSearchParams("view=source&unit=E1S2"), "board", FACTORY).toString()).toBe("unit=E1S2");
    expect(params.toString()).toBe("unit=E1S2");
  });

  it("requires a leave check when the visible editor or episode changes", () => {
    expect(staysInEpisodeView("?view=board&unit=E1S2", "/episodes/1", FACTORY)).toBe(true);
    expect(staysInEpisodeView("?view=source", "/episodes/1", FACTORY)).toBe(false);
    expect(staysInEpisodeView("?view=edit", "/episodes/1", FACTORY)).toBe(false);
    expect(staysInEpisodeView("/episodes/2", "/episodes/1", FACTORY)).toBe(false);
    expect(staysInEpisodeView("?view=grid", "/episodes/1", FACTORY)).toBe(true);
  });
});
