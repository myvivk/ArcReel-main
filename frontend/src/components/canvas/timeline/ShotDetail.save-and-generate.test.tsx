import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import type { NarrationSegment } from "@/types";
import type { ShotGridPlan } from "@/types/shot-grid";
import { makeNarrationSegment } from "@/test/factories";
import { ShotDetail } from "./ShotDetail";

function grid(): ShotGridPlan {
  return {
    id: "shot_grid_123456789abc",
    episode: 1,
    script_file: "episode_1.json",
    shot_id: "E1S01",
    rows: 3,
    cols: 3,
    status: "confirmed",
    prompt: "第1格（远景）：雨夜街道\n第2格（中景）：霓虹灯下\n第3格（近景）：行人驻足",
    created_at: "",
    updated_at: "",
    error_message: null,
    composite_path: "shot_grids/contact.png",
    composite_url: "/contact.png",
    cells: Array.from({ length: 9 }, (_, index) => ({
      index,
      prompt: "",
      time_ratio: index / 8,
      continuity_notes: "",
      confirmed: true,
      image_path: `storyboards/cell-${index}.png`,
    })),
  };
}

function renderDetail(props: Partial<Parameters<typeof ShotDetail>[0]> = {}, segment: NarrationSegment = makeNarrationSegment()) {
  return render(
    <ShotDetail
      segment={segment}
      segmentId={segment.segment_id}
      contentMode="narration"
      aspectRatio="9:16"
      projectName="demo"
      scriptFile="episode_1.json"
      selectedIndex={0}
      totalCount={1}
      onPrev={() => {}}
      onNext={() => {}}
      durationOptions={[8]}
      {...props}
    />,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(API, "getShotGrid").mockResolvedValue(null);
});

describe("ShotDetail 保存并生成", () => {
  it("视频有未保存修改时，右侧媒体按钮先提交修改再生成", async () => {
    vi.mocked(API.getShotGrid).mockResolvedValue(grid());
    const onUpdatePrompt = vi.fn().mockResolvedValue(true);
    const onGenerateVideo = vi.fn();
    renderDetail({ onUpdatePrompt, onGenerateVideo });

    expect(screen.queryByRole("button", { name: /保存并生成/ })).not.toBeInTheDocument();
    fireEvent.change(screen.getByDisplayValue("雨夜街道"), { target: { value: "雨后的街道" } });
    await waitFor(() => expect(screen.getAllByRole("button", { name: "保存并生成" }).at(-1)).toBeEnabled());
    fireEvent.click(screen.getAllByRole("button", { name: "保存并生成" }).at(-1)!);

    await waitFor(() => expect(onGenerateVideo).toHaveBeenCalledWith("E1S01"));
    expect(onUpdatePrompt).toHaveBeenCalledWith("E1S01", { image_prompt: "雨后的街道" });
    expect(onUpdatePrompt.mock.invocationCallOrder[0]).toBeLessThan(onGenerateVideo.mock.invocationCallOrder[0]);
  });

  it("保存失败时不生成，错误显示在提示条上", async () => {
    vi.mocked(API.getShotGrid).mockResolvedValue(grid());
    const onUpdatePrompt = vi.fn().mockRejectedValue(new Error("提示词不能为空"));
    const onGenerateVideo = vi.fn();
    renderDetail({ onUpdatePrompt, onGenerateVideo });

    fireEvent.change(screen.getByDisplayValue("雨夜街道"), { target: { value: "雨后的街道" } });
    await waitFor(() => expect(screen.getAllByRole("button", { name: "保存并生成" }).at(-1)).toBeEnabled());
    fireEvent.click(screen.getAllByRole("button", { name: "保存并生成" }).at(-1)!);

    expect(await screen.findByRole("alert")).toHaveTextContent("提示词不能为空");
    expect(onGenerateVideo).not.toHaveBeenCalled();
  });

  it("视频保存并生成期间，尾帧不能更换或清除，保存后再生成", async () => {
    vi.mocked(API.getShotGrid).mockResolvedValue(grid());
    let finishSave: (refreshed: boolean) => void = () => {};
    const onUpdatePrompt = vi.fn(() => new Promise<boolean>((resolve) => { finishSave = resolve; }));
    const onGenerateVideo = vi.fn();
    const segment = makeNarrationSegment({
      end_frame_image: "end_frames/E1S01.png",
      generated_assets: {
        storyboard_image: "storyboards/E1S01.png",
        storyboard_last_image: null,
        grid_id: null,
        grid_cell_index: null,
        video_clip: null,
        video_thumbnail: null,
        video_uri: null,
        status: "storyboard_ready",
      },
    });
    renderDetail({ onUpdatePrompt, onGenerateVideo, lastFrame: true }, segment);

    await waitFor(() => expect(screen.getByRole("button", { name: "生成视频" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: /^尾帧/ }));
    fireEvent.change(screen.getByDisplayValue("雨夜街道"), { target: { value: "雨后的街道" } });
    fireEvent.click(screen.getAllByRole("button", { name: "保存并生成" }).at(-1)!);

    expect(screen.getByRole("button", { name: "更换图片" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "清除" })).toBeDisabled();
    expect(onGenerateVideo).not.toHaveBeenCalled();

    await act(async () => finishSave(true));
    await waitFor(() => expect(onGenerateVideo).toHaveBeenCalledWith("E1S01"));
  });

  it("备注与提示词一起保存", async () => {
    const onUpdatePrompt = vi.fn().mockResolvedValue(true);
    renderDetail({ onUpdatePrompt });

    fireEvent.click(screen.getByRole("button", { name: "备注" }));
    fireEvent.change(await screen.findByRole("textbox", { name: "备注" }), { target: { value: "换成雨后" } });
    fireEvent.change(screen.getByDisplayValue("雨夜街道"), { target: { value: "雨后的街道" } });

    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() =>
      expect(onUpdatePrompt).toHaveBeenCalledWith("E1S01", { image_prompt: "雨后的街道", note: "换成雨后" }),
    );
  });
});
