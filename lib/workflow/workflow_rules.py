"""The short-drama factory production workflow."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkflowStepRule:
    """One workflow step; the order is presentational only and never a precondition."""

    id: str
    applicable: bool


@dataclass(frozen=True, slots=True)
class WorkflowRule:
    """The complete workflow shape for one immutable project mode pair."""

    content_mode: str
    generation_mode: str
    skeleton_kind: str
    steps: tuple[WorkflowStepRule, ...]


_STEP_IDS = (
    "project_input",
    "asset_sheets",
    "final_script",
    "script_structure",
    "shot_grid",
    "video_prompt",
    "video",
    "edit",
)

WORKFLOW_RULES: dict[tuple[str, str], WorkflowRule] = {
    ("drama", "storyboard"): WorkflowRule(
        content_mode="drama",
        generation_mode="storyboard",
        skeleton_kind="scenes",
        steps=tuple(WorkflowStepRule(id=step, applicable=True) for step in _STEP_IDS),
    ),
}


def workflow_rule(content_mode: str, generation_mode: str) -> WorkflowRule:
    """Return the exhaustive rule for a validated project mode pair."""

    try:
        return WORKFLOW_RULES[(content_mode, generation_mode)]
    except KeyError as exc:
        raise ValueError(f"unsupported workflow mode pair: {content_mode!r}, {generation_mode!r}") from exc


__all__ = [
    "WORKFLOW_RULES",
    "WorkflowRule",
    "WorkflowStepRule",
    "workflow_rule",
]
