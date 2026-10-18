import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import type { ShotGridPlan } from "@/types/shot-grid";
import { ReferenceMaterialsSection } from "./ReferenceMaterialsSection";

const PROJECT = "demo";

function plan(overrides: Partial<ShotGridPlan> = {}): ShotGridPlan {
  return {
    id: "shot_grid_123456789abc",
    episode: 1,
    script_file: "episode_1.json",
    shot_id: "E1S01",
    rows: 2,
    cols: 2,
    cells: [],
    prompt: "",
    composite_path: "shot_grids/shot_grid_123456789abc.png",
    status: "completed",
    task_id: null,
    created_at: "",
    updated_at: "",
    error_message: null,
    ...overrides,
  };
}

function renderSection() {
  return render(
    <ReferenceMaterialsSection
      projectName={PROJECT}
      scriptFile="episode_1.json"
      episode={1}
      shotId="E1S01"
      beforeGenerate={async () => true}
      onPlanChange={vi.fn()}
      onBusyChange={vi.fn()}
    />,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ReferenceMaterialsSection", () => {
  it("宫格图缩略图按容器等比缩放，点击进查看器", async () => {
    vi.spyOn(API, "getShotGrid").mockResolvedValue(plan());

    renderSection();

    const thumbnail = await screen.findByRole("button", { name: "点击查看大图" });
    const image = thumbnail.querySelector("img");
    expect(image).toHaveClass("size-full", "object-contain");

    fireEvent.click(thumbnail);

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "镜头关键帧宫格图" })).toBeInTheDocument();
  });
});
