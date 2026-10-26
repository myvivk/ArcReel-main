import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { GateRoundRow } from "./GateRoundRow";

describe("GateRoundRow", () => {
  it("shows the attempt, the rejection and the quality-gate reason; expands to the full text", () => {
    render(
      <GateRoundRow
        block={{
          type: "gate_round",
          gate: "storyboard_plan",
          episode: 1,
          attempt: 1,
          max_attempts: 5,
          outcome: "rejected",
          reason: "scene 2：镜头3 块过短",
          elapsed_ms: 241_234,
        }}
      />,
    );

    const row = screen.getByRole("button", { name: /质量校验.*AI 分镜.*第 1\/5 轮：scene 2：镜头3 块过短/ });
    expect(row).toHaveAttribute("aria-expanded", "false");
    expect(row).toHaveTextContent("失败");
    // 折叠时只显示一行摘要，不展开质量闸原文
    expect(screen.queryByText("校验意见")).not.toBeInTheDocument();

    fireEvent.click(row);
    expect(screen.getByText("校验意见")).toBeInTheDocument();
    expect(screen.getByText("scene 2：镜头3 块过短")).toBeInTheDocument();
    expect(screen.getByText("241.2 秒")).toBeInTheDocument();
  });

  it("renders a passed round without a failure mark and without a reason section", () => {
    render(
      <GateRoundRow
        block={{
          type: "gate_round",
          gate: "storyboard_plan",
          episode: 1,
          attempt: 3,
          max_attempts: 5,
          outcome: "passed",
          elapsed_ms: 188_500,
        }}
      />,
    );

    const row = screen.getByRole("button", { name: /第 3\/5 轮：通过/ });
    expect(row).not.toHaveTextContent("失败");
    expect(row).not.toHaveTextContent("未通过");
    fireEvent.click(row);
    expect(screen.queryByText("校验意见")).not.toBeInTheDocument();
    expect(screen.getByText("188.5 秒")).toBeInTheDocument();
  });

  it("falls back to a rejection label when the gate gave no reason text", () => {
    render(
      <GateRoundRow
        block={{
          type: "gate_round",
          gate: "storyboard_plan",
          attempt: 2,
          max_attempts: 5,
          outcome: "rejected",
        }}
      />,
    );

    expect(screen.getByRole("button", { name: /第 2\/5 轮：未通过/ })).toBeInTheDocument();
  });

  it("labels rewritten storyboards separately", () => {
    render(
      <GateRoundRow
        block={{
          type: "gate_round",
          gate: "storyboard_rewrite",
          episode: 2,
          attempt: 1,
          max_attempts: 5,
          outcome: "passed",
        }}
      />,
    );

    expect(screen.getByRole("button", { name: /质量校验.*分镜重写.*第 1\/5 轮：通过/ })).toBeInTheDocument();
  });
});
