import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ShotDetail } from "./ShotDetail";
import { API } from "@/api";
import type { ItemPromptPreview, NarrationSegment } from "@/types";
import { makeNarrationSegment } from "@/test/factories";

const RENDERED_IMAGE_PROMPT = "Style: Anime\n\nScene: 雨夜街道";

beforeEach(() => {
  vi.spyOn(API, "getShotGrid").mockResolvedValue(null);
});

function makeSegment(overrides: Partial<NarrationSegment> = {}): NarrationSegment {
  return makeNarrationSegment({
    image_prompt: {
      scene: "雨夜街道",
      composition: { shot_type: "Medium Shot", lighting: "暖光", ambiance: "薄雾" },
    },
    video_prompt: { action: "撑伞走过", camera_motion: "Static", ambiance_audio: "雨声", dialogue: [] },
    ...overrides,
  });
}

function renderDetail(
  segment: NarrationSegment,
  props: Partial<Parameters<typeof ShotDetail>[0]> = {},
) {
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
      onUpdatePrompt={vi.fn()}
      {...props}
    />,
  );
}

function preview(): ItemPromptPreview {
  return {
    item_id: "E1S01",
    content_mode: "narration",
    storyboard_image: { text: RENDERED_IMAGE_PROMPT, unavailable: null, is_text_form: false, warnings: [] },
    video: { text: "最终视频提示词", unavailable: null, is_text_form: false, warnings: [] },
  };
}

describe("ShotDetail 文本提示词编辑", () => {
  it("不显示结构化切换；结构化存量只以主文本字段展示并保存为字符串", () => {
    const onUpdatePrompt = vi.fn();
    renderDetail(makeSegment(), { onUpdatePrompt });

    expect(screen.queryByRole("button", { name: "结构化" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "文本" })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "分镜提示词" })).toHaveValue("雨夜街道");
    expect(screen.getByRole("textbox", { name: "视频提示词" })).toHaveValue("撑伞走过");

    fireEvent.change(screen.getByRole("textbox", { name: "分镜提示词" }), { target: { value: "改过的画面" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(onUpdatePrompt).toHaveBeenCalledWith("E1S01", { image_prompt: "改过的画面" });
  });

  it("AI 已生成的标准文本原样回显并保存", () => {
    const onUpdatePrompt = vi.fn();
    renderDetail(makeSegment({
      image_prompt: "【镜头1】雨夜街道中，林清@[林清]撑伞站在街角。",
      video_prompt: "0-3秒：@林清缓缓抬头看向雨幕。\n3-6秒：镜头推近，伞沿雨水滴落。",
    }), { onUpdatePrompt });

    expect(screen.getByRole("textbox", { name: "分镜提示词" })).toHaveValue("【镜头1】雨夜街道中，林清@[林清]撑伞站在街角。");
    expect(screen.getByRole("textbox", { name: "视频提示词" })).toHaveValue("0-3秒：@林清缓缓抬头看向雨幕。\n3-6秒：镜头推近，伞沿雨水滴落。");
    fireEvent.change(screen.getByRole("textbox", { name: "分镜提示词" }), { target: { value: "雨后的街道" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    expect(onUpdatePrompt).toHaveBeenCalledWith("E1S01", { image_prompt: "雨后的街道" });
  });
});

describe("ShotDetail 最终提示词预览", () => {
  it("分镜图与视频各有入口，打开哪一侧就只展示哪一侧的最终文本", async () => {
    const spy = vi.spyOn(API, "previewScriptItemPrompts").mockResolvedValue(preview());
    renderDetail(makeSegment());

    const [, videoButton] = screen.getAllByRole("button", { name: "查看提示词" });
    expect(spy).not.toHaveBeenCalled();
    fireEvent.click(videoButton);

    const dialog = await screen.findByRole("dialog", { name: "视频最终提示词" });
    expect(await within(dialog).findByText("最终视频提示词")).toBeInTheDocument();
    expect(within(dialog).queryByText(/Scene: 雨夜街道/)).not.toBeInTheDocument();
    expect(spy).toHaveBeenCalledWith("demo", "E1S01", "episode_1.json", {
      signal: expect.any(AbortSignal),
    });
  });

  it("有修改时先保存再预览，预览请求排在保存成功之后", async () => {
    const save = vi.fn().mockResolvedValue(true);
    const request = vi.spyOn(API, "previewScriptItemPrompts").mockResolvedValue(preview());
    renderDetail(makeSegment(), { onUpdatePrompt: save });
    fireEvent.change(screen.getByDisplayValue("雨夜街道"), { target: { value: "改过的画面" } });
    fireEvent.click(screen.getAllByRole("button", { name: "保存并预览" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "分镜图最终提示词" });
    expect(await within(dialog).findByText(/Scene: 雨夜街道/)).toBeInTheDocument();
    expect(save.mock.invocationCallOrder[0]).toBeLessThan(request.mock.invocationCallOrder[0]);
    expect(save).toHaveBeenCalledWith("E1S01", { image_prompt: "改过的画面" });
  });

  it.each([false, new Error("写入失败")])("保存或刷新失败时不打开预览（%s）", async (result) => {
    const save = result instanceof Error ? vi.fn().mockRejectedValue(result) : vi.fn().mockResolvedValue(result);
    const request = vi.spyOn(API, "previewScriptItemPrompts").mockResolvedValue(preview());
    renderDetail(makeSegment(), { onUpdatePrompt: save });
    fireEvent.change(screen.getByDisplayValue("雨夜街道"), { target: { value: "改过的画面" } });
    fireEvent.click(screen.getAllByRole("button", { name: "保存并预览" })[0]);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
  });
});
