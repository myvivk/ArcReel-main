import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import { ShotDetail } from "./ShotDetail";
import type { NarrationSegment } from "@/types";
import { makeNarrationSegment } from "@/test/factories";

function makeSegment(overrides: Partial<NarrationSegment> = {}): NarrationSegment {
  return makeNarrationSegment({ image_prompt: null, video_prompt: null, ...overrides });
}

function renderDetail(segment: NarrationSegment, props: Partial<Parameters<typeof ShotDetail>[0]> = {}) {
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

describe("ShotDetail 空提示词", () => {
  beforeEach(() => {
    vi.spyOn(API, "getShotGrid").mockResolvedValue(null);
  });
  it("两侧为 null 时不标「待生成」徽标，文本框留空", () => {
    renderDetail(makeSegment());
    // 只看两个提示词区块：资产状态徽标另有一枚同文案的「待生成」
    for (const title of ["分镜提示词", "视频提示词"]) {
      const section = screen.getByText(title).closest("section");
      expect(section).not.toBeNull();
      expect(within(section as HTMLElement).queryByText("待生成")).not.toBeInTheDocument();
    }
    const textareas = screen.getAllByPlaceholderText(/描述/);
    expect(textareas.every((el) => (el as HTMLTextAreaElement).value === "")).toBe(true);
  });

  it("在空提示词一侧输入再清空不算改动：保存按钮不可用、不发 PATCH", () => {
    const onUpdatePrompt = vi.fn();
    renderDetail(makeSegment(), { onUpdatePrompt });
    const [imageBox] = screen.getAllByPlaceholderText(/描述/);
    fireEvent.change(imageBox, { target: { value: "雨夜" } });
    expect(screen.getByRole("button", { name: "保存" })).toBeEnabled();
    fireEvent.change(imageBox, { target: { value: "" } });
    // 草稿回到干净态：保存入口收起，没有任何 PATCH 发出
    expect(screen.queryByRole("button", { name: "保存" })).not.toBeInTheDocument();
    expect(onUpdatePrompt).not.toHaveBeenCalled();
  });

  it("填写后保存只提交填写的那一侧", () => {
    const onUpdatePrompt = vi.fn();
    renderDetail(makeSegment(), { onUpdatePrompt });
    const [imageBox] = screen.getAllByPlaceholderText(/描述/);
    fireEvent.change(imageBox, { target: { value: "雨夜街道" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(onUpdatePrompt).toHaveBeenCalledWith("E1S01", { image_prompt: "雨夜街道" });
  });

  it("待编写且提示词为空时显示待编写提示", () => {
    renderDetail(makeSegment({ pending_authoring: true }));
    expect(screen.getByText(/^待编写/)).toBeInTheDocument();
  });

  it("待编写但已有提示词（机械切分骨架/预填）时不喊尚未编写", () => {
    // 短剧导入会预填一版骨架提示词，此时条目仍是待编写状态，但框里已有内容可微调。
    renderDetail(makeSegment({ image_prompt: "【镜头1】中景，平视机位，固定。", pending_authoring: true }));
    expect(screen.queryByText(/^待编写/)).not.toBeInTheDocument();
  });

  it("未带待编写标记的条目即使提示词为空也不显示待编写提示", () => {
    renderDetail(makeSegment());
    expect(screen.queryByText(/^待编写/)).not.toBeInTheDocument();
  });
});
