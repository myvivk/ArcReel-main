import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import { makeNarrationSegment } from "@/test/factories";
import { useAppStore } from "@/stores/app-store";
import { useProjectsStore } from "@/stores/projects-store";
import { useTasksStore } from "@/stores/tasks-store";
import { ShotDetail } from "./ShotDetail";

function renderDetail(onRestoreVideo: () => Promise<unknown>) {
  const segment = makeNarrationSegment();
  render(
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
      onUpdatePrompt={vi.fn()}
      onRestoreVideo={onRestoreVideo}
    />,
  );
}

function chooseVideo() {
  const input = screen.getByRole("button", { name: "上传视频" }).previousElementSibling as HTMLInputElement;
  fireEvent.change(input, { target: { files: [new File(["mp4"], "shot.mp4", { type: "video/mp4" })] } });
}

describe("ShotDetail 媒体上传", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    useAppStore.setState(useAppStore.getInitialState(), true);
    useProjectsStore.setState(useProjectsStore.getInitialState(), true);
    useTasksStore.setState(useTasksStore.getInitialState(), true);
    vi.spyOn(API, "getShotGrid").mockResolvedValue(null);
    vi.spyOn(API, "uploadShotMedia").mockResolvedValue({
      success: true,
      path: "videos/E1S01.mp4",
      version: 2,
      asset_fingerprints: {},
    });
  });

  it("上传后项目数据已刷新时报告上传完成", async () => {
    const restore = vi.fn().mockResolvedValue(true);
    renderDetail(restore);

    chooseVideo();

    await waitFor(() => expect(useAppStore.getState().toast).toMatchObject({ text: "「E1S01」上传完成", tone: "success" }));
    expect(API.uploadShotMedia).toHaveBeenCalledWith("demo", "episode_1.json", "E1S01", "video", expect.any(File));
  });

  it("上传后项目数据没刷新成功时不再报告上传完成", async () => {
    const pushToast = vi.spyOn(useAppStore.getState(), "pushToast");
    const restore = vi.fn().mockResolvedValue(false);
    renderDetail(restore);

    chooseVideo();

    await waitFor(() => expect(restore).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByRole("button", { name: "上传视频" })).toBeEnabled());
    expect(pushToast).not.toHaveBeenCalledWith(expect.anything(), "success");
  });
});
