import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { ShieldAlert, ShieldCheck } from "lucide-react";
import type { ContentBlock } from "@/types";
import { WorkDetail, WorkRow } from "./WorkRow";
import type { WorkStatus } from "./work-label";

// ---------------------------------------------------------------------------
// GateRoundRow – 质量闸的一轮结果：一行工序展示「第 N/5 轮：未通过 · 原因 / 通过」。
//
// 每轮都是独立条目（SSE 到达即显示），最终 tool_result 只回填工具块，不会覆盖这些行；
// 展开可看质量闸原文与模型耗时。拒绝用红色图标，让返修过程显眼。
// ---------------------------------------------------------------------------

function gateName(gate: string | undefined, t: TFunction): string {
  switch (gate) {
    case "storyboard_rewrite":
      return t("gate_storyboard_rewrite");
    case "storyboard_plan":
      return t("gate_storyboard_plan");
    default:
      return t("work_quality_gate");
  }
}

export function GateRoundRow({ block }: { block: ContentBlock }) {
  const { t } = useTranslation("dashboard");
  const rejected = block.outcome === "rejected";
  const status: WorkStatus = rejected ? "error" : "ok";
  const reason = block.reason?.trim() ?? "";
  const attempt = block.attempt ?? 1;
  const total = block.max_attempts ?? 5;
  // 拒绝轮直接展示质量闸原文（「第 1/5 轮：scene 2…」），通过轮展示「通过」。
  const outcome = rejected ? reason || t("gate_round_rejected") : t("gate_round_passed");
  const summary = t("gate_round_summary", { attempt, total, outcome });
  const elapsed = typeof block.elapsed_ms === "number" && block.elapsed_ms >= 0
    ? t("gate_round_elapsed", { seconds: (block.elapsed_ms / 1000).toFixed(1) })
    : "";

  return (
    <WorkRow
      icon={rejected ? ShieldAlert : ShieldCheck}
      name={`${t("work_quality_gate")} · ${gateName(block.gate, t)}`}
      summary={summary}
      status={status}
    >
      {reason && <WorkDetail label={t("gate_round_reason_label")} text={reason} error={rejected} />}
      {elapsed && <WorkDetail label={t("gate_round_duration_label")} text={elapsed} />}
    </WorkRow>
  );
}
