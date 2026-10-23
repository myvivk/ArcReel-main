"""AI 操作结构准入谓词：每个操作只看自己的输入，结论带稳定理由码。"""

from __future__ import annotations

import pytest

from lib.workflow.operation_admission import (
    AdmissionReason,
    AdmissionState,
    OperationAdmission,
    admit_author_prompts,
    admit_edit_timeline,
    pending_authoring_entry_ids,
)


def _conclusion(admission: OperationAdmission) -> tuple[str, str | None]:
    return admission.state.value, admission.reason.value if admission.reason else None


def test_pending_authoring_ids_follow_script_order_and_the_skeleton_id_field() -> None:
    items = [
        {"segment_id": "E1S01", "pending_authoring": True},
        {"segment_id": "E1S02"},
        {"segment_id": "E1S03", "pending_authoring": True},
        "not-an-item",
    ]
    assert pending_authoring_entry_ids(items, "segments") == ["E1S01", "E1S03"]
    assert pending_authoring_entry_ids(items, None) == []


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"formal_script": True, "pending_ids": ["E1S01"], "draft_pending": False}, ("admitted", None)),
        (
            {"formal_script": True, "pending_ids": [], "draft_pending": False, "explicit_ids": ["E1S01"]},
            ("admitted", None),
        ),
        ({"formal_script": True, "pending_ids": [], "draft_pending": False}, ("refused", "no_pending_authoring")),
        ({"formal_script": False, "pending_ids": [], "draft_pending": False}, ("refused", "formal_script_missing")),
        (
            {"formal_script": False, "pending_ids": [], "draft_pending": True},
            ("refused", "prompt_authoring_draft_pending"),
        ),
    ],
)
def test_prompt_authoring_needs_pending_entries_and_no_pending_draft(kwargs: dict, expected: tuple) -> None:
    assert _conclusion(admit_author_prompts(**kwargs)) == expected


@pytest.mark.parametrize(
    ("available_videos", "expected"),
    [(1, ("admitted", None)), (0, ("refused", "no_available_video"))],
)
def test_edit_timeline_needs_at_least_one_available_video(available_videos: int, expected: tuple) -> None:
    assert _conclusion(admit_edit_timeline(available_videos=available_videos)) == expected


def test_refusals_always_carry_a_reason_and_admission_never_does() -> None:
    for state in AdmissionState:
        reason = None if state is AdmissionState.ADMITTED else AdmissionReason.NOT_APPLICABLE
        assert OperationAdmission(state=state, reason=reason).admitted is (state is AdmissionState.ADMITTED)
