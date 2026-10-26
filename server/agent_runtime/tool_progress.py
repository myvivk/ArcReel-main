"""质量闸逐轮进度载荷：宿主无关，只描述一轮校验的结果。

质量闸在 MCP 工具内部同步跑完（最多 ``_PLAN_ATTEMPTS`` 轮），模型在 ``tools/call``
期间被阻塞。因此逐轮结果分两路交付：

- 用户侧：宿主把每轮载荷定型为会话事件条目（``system`` / ``quality_gate_round``），
  经既有的 ``log_entry`` → SSE ``entry`` 通道实时呈现，刷新后仍可回看；
- 主 Agent 侧：同一批轮次随工具结果 ``script.planning_rounds`` 返回，工具返回即入上下文。

本模块只含标准库：不得 import ``mcp`` / ``claude_agent_sdk``，保持宿主无关。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

#: 整体 AI 分镜（含首轮与返修轮）。
GATE_STORYBOARD_PLAN = "storyboard_plan"
#: 按分镜重写（部分重生成）。
GATE_STORYBOARD_REWRITE = "storyboard_rewrite"

#: 会话事件日志里的系统条目子类型。
QUALITY_GATE_ROUND_SUBTYPE = "quality_gate_round"

#: 拒绝原因原文的截断长度：条目与工具结果都不该被一次超长校验文案撑爆。
MAX_REASON_CHARS = 300

type GateOutcome = Literal["rejected", "passed"]


@dataclass(frozen=True, slots=True)
class QualityGateRound:
    """一次质量闸尝试的结构化结果。

    ``attempt`` 是 1-based 轮次，``max_attempts`` 是本次闸门的上限（当前恒为 5）；
    ``elapsed_ms`` 只统计 ``generator.generate`` 的墙钟时长，不含校验与落盘，
    避免与用户感知的「等了几分钟」错位。
    """

    gate: str
    episode: int
    attempt: int
    max_attempts: int
    outcome: GateOutcome
    elapsed_ms: int
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "gate": self.gate,
            "episode": self.episode,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "outcome": self.outcome,
            "reason": self.reason,
            "elapsed_ms": self.elapsed_ms,
        }


#: 宿主注入的进度回调；宿主未注入（Web 按钮 / 远程 MCP / 单测）时为 ``None``。
type ProgressReporter = Callable[[QualityGateRound], Awaitable[None]]

__all__ = [
    "GATE_STORYBOARD_PLAN",
    "GATE_STORYBOARD_REWRITE",
    "MAX_REASON_CHARS",
    "QUALITY_GATE_ROUND_SUBTYPE",
    "GateOutcome",
    "ProgressReporter",
    "QualityGateRound",
]
