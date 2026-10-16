import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import { useTasksStore } from "@/stores/tasks-store";
import type { DramaScene } from "@/types";
import { ShotDetail } from "./ShotDetail";

const saved = () => vi.fn().mockResolvedValue(true);

function makeScene(durationSeconds: number): DramaScene {
  return {
    scene_id: "E1S01",
    duration_seconds: durationSeconds,
    segment_break: false,
    characters_in_scene: [],
    scenes: [],
    props: [],
    image_prompt: {
      scene: "重逢",
      composition: { shot_type: "Medium Shot", lighting: "暖光", ambiance: "怀旧" },
    },
    video_prompt: { action: "推门而入", camera_motion: "Static", ambiance_audio: "", dialogue: [] },
    utterances: [],
  };
}

function renderDetail(props: Partial<Parameters<typeof ShotDetail>[0]> = {}, seconds = 4) {
  return render(
    <ShotDetail
      segment={makeScene(seconds)}
      segmentId="E1S01"
      contentMode="drama"
      aspectRatio="9:16"
      projectName="demo"
      scriptFile="episode_1.json"
      selectedIndex={0}
      totalCount={1}
      onPrev={() => {}}
      onNext={() => {}}
      onUpdatePrompt={saved()}
      durationOptions={[8]}
      {...props}
    />,
  );
}

function warningLabel(): string | null {
  const name = screen.getByRole("button", { name: /^\d+ 秒/ }).textContent ?? "";
  return name.replace(/^\d+ 秒/, "") || null;
}

beforeEach(() => {
  vi.restoreAllMocks();
  useTasksStore.setState(useTasksStore.getInitialState(), true);
  vi.spyOn(API, "getShotGrid").mockResolvedValue(null);
});

describe("ShotDetail 时长候选与越界提示", () => {
  it("只呈现收窄后的候选，重选后随分镜保存", async () => {
    const onUpdatePrompt = saved();
    renderDetail({ onUpdatePrompt });

    expect(screen.getByRole("button", { name: /4 秒/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /4 秒/ }));
    expect(screen.getAllByRole("radio")).toHaveLength(1);
    fireEvent.click(screen.getByRole("radio", { name: /8 秒/ }));

    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(onUpdatePrompt).toHaveBeenCalledWith("E1S01", { duration_seconds: 8 }));
  });

  it("越界提示按成因区分", () => {
    const { unmount } = renderDetail({ durationWarningReason: () => "resolution" });
    expect(warningLabel()).toContain("当前分辨率");
    unmount();

    renderDetail({ durationWarningReason: () => "reference" });
    expect(warningLabel()).toContain("参考生视频");
  });
});

describe("ShotDetail 时长编辑的占用态门控", () => {
  it("该分镜生成中时禁用时长 pill 并说明原因", () => {
    for (const busyProp of ["generatingStoryboard", "generatingVideo"] as const) {
      const { unmount } = renderDetail({ [busyProp]: true }, 8);
      const pill = screen.getByRole("button", { name: /8 秒/ });
      expect(pill).toBeDisabled();
      expect(pill).toHaveAccessibleDescription("该分镜正在生成中，暂不能修改时长");
      unmount();
    }
  });

  it("store 已记录该分镜在跑时，面板打不开", () => {
    const onUpdatePrompt = saved();
    renderDetail({ onUpdatePrompt });
    useTasksStore.setState({
      tasks: [{
        project_name: "demo",
        task_type: "storyboard",
        media_type: "image",
        resource_id: "E1S01",
        resource_type: null,
        script_file: null,
        payload: {},
        task_id: "t2",
        status: "running",
        result: null,
        error_message: null,
        cancelled_by: null,
        provider_id: null,
        provider_job_id: null,
        source: "webui",
        queued_at: "2026-07-28T00:00:00Z",
        started_at: null,
        finished_at: null,
        updated_at: "2026-07-28T00:00:00Z",
      }],
    });

    fireEvent.click(screen.getByRole("button", { name: /4 秒/ }));
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    expect(onUpdatePrompt).not.toHaveBeenCalled();
  });

  it("时长由端点固定时不说成未配置", () => {
    renderDetail({ durationOptions: [], durationEndpointFixed: true });

    expect(screen.getByRole("button", { name: /4 秒/ })).toHaveAccessibleDescription(
      "时长由端点固定：每段成片多长由 workflow 决定。",
    );
  });
});
