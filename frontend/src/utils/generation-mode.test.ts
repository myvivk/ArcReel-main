import { describe, it, expect } from "vitest";
import { normalizeRoute, type GenerationRoute } from "./generation-mode";

describe("normalizeRoute", () => {
  it("keeps both routes", () => {
    for (const r of ["storyboard", "reference_video"] as GenerationRoute[]) {
      expect(normalizeRoute(r)).toBe(r);
    }
  });
  it("falls back to 'storyboard' when the field is absent or unparseable", () => {
    expect(normalizeRoute(undefined)).toBe("storyboard");
    expect(normalizeRoute(null)).toBe("storyboard");
    expect(normalizeRoute(42)).toBe("storyboard");
  });
});
