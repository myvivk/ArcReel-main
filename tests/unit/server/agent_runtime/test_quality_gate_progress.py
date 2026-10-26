"""质量闸逐轮进度：宿主无关载荷与事件日志条目的定型。"""

from __future__ import annotations

from server.agent_runtime.event_log import (
    ENTRY_SUBTYPE_QUALITY_GATE_ROUND,
    ENTRY_TYPE_SYSTEM,
    build_quality_gate_entry,
)
from server.agent_runtime.tool_progress import (
    GATE_STORYBOARD_PLAN,
    GATE_STORYBOARD_REWRITE,
    MAX_REASON_CHARS,
    QualityGateRound,
)
from server.services.short_drama_workflow import _quality_gate_round


def _round(**overrides: object) -> QualityGateRound:
    values: dict[str, object] = {
        "gate": GATE_STORYBOARD_PLAN,
        "episode": 1,
        "attempt": 1,
        "max_attempts": 5,
        "outcome": "rejected",
        "elapsed_ms": 241_234,
        "reason": "scene 2：镜头3 块过短",
    }
    values.update(overrides)
    return QualityGateRound(**values)  # type: ignore[arg-type]


def test_quality_gate_round_carries_the_contract_fields() -> None:
    payload = _round().as_dict()

    assert payload == {
        "gate": GATE_STORYBOARD_PLAN,
        "episode": 1,
        "attempt": 1,
        "max_attempts": 5,
        "outcome": "rejected",
        "reason": "scene 2：镜头3 块过短",
        "elapsed_ms": 241_234,
    }


def test_build_quality_gate_entry_is_a_stable_system_event() -> None:
    entry = build_quality_gate_entry(_round(outcome="passed", reason=None))

    assert entry["type"] == ENTRY_TYPE_SYSTEM
    assert entry["subtype"] == ENTRY_SUBTYPE_QUALITY_GATE_ROUND == "quality_gate_round"
    assert entry["uuid"].startswith("gate-")
    assert isinstance(entry["timestamp"], str)
    assert entry["timestamp"]
    assert entry["attempt"] == 1
    assert entry["max_attempts"] == 5
    assert entry["outcome"] == "passed"
    assert entry["reason"] is None
    assert entry["elapsed_ms"] == 241_234


def test_gate_round_builder_truncates_only_the_display_reason() -> None:
    long_reason = "场景 4：镜头 1 缺神态。" * 100

    built = _quality_gate_round(
        gate=GATE_STORYBOARD_REWRITE,
        episode=2,
        attempt=3,
        outcome="rejected",
        elapsed_ms=153_000,
        reason=long_reason,
    )

    assert built.reason is not None
    assert len(built.reason) == MAX_REASON_CHARS
    assert long_reason.startswith(built.reason)
    assert built.max_attempts == 5
