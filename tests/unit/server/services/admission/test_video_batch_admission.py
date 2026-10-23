"""Web 与 Agent 共用的整批准入判定适配层：分镜图生视频与参考生视频的当前状态判定。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lib.generation.batch_admission import BatchAdmissionDecision
from lib.generation.generation_result import GenerationAction, GenerationSelectionMode
from lib.generation.video_request_facts import VideoRequestFactsFailure
from server.services.admission import video_batch_admission as admission_mod
from server.services.admission.video_batch_admission import admit_storyboard_video_batch
from tests.factories import make_video_request_facts


def _stub_state(
    monkeypatch: pytest.MonkeyPatch,
    *,
    active: list[dict[str, Any]] | None = None,
) -> list[str]:
    probes: list[str] = []

    async def _active(**_kwargs):
        probes.append("active_tasks")
        return list(active or [])

    monkeypatch.setattr(admission_mod, "get_active_tasks_for_resources", _active)
    return probes


async def test_storyboard_admission_consults_only_active_tasks(monkeypatch, tmp_path: Path):
    """分镜图生视频的档位由执行期按剧本取，整批准入唯一查询的状态是在途任务冲突。"""

    probes = _stub_state(monkeypatch)

    admission = await admit_storyboard_video_batch(
        project_name="demo",
        project={},
        script_file="episode_1.json",
        items=[("E1S01", {"duration_seconds": 4}), ("E1S02", {})],
        operation="generate_videos",
        selection=GenerationSelectionMode.MISSING_ONLY,
        video_request_facts=make_video_request_facts(),
    )

    assert admission.decision is BatchAdmissionDecision.ADMITTED
    assert admission.unit_ids == ("E1S01", "E1S02")
    assert probes == ["active_tasks"]


async def test_storyboard_facts_failure_blocks_each_target_with_code_params_and_action(monkeypatch, tmp_path: Path):
    """分镜路线只有一个桶：事实失败时每个目标都带同一问题码、参数与修复指引，不塌成一句通用错误。"""
    _stub_state(monkeypatch)
    failure = VideoRequestFactsFailure(
        "video_supported_durations_incompatible",
        (("provider", "gemini-aistudio"), ("model", "veo-3.1"), ("resolution", "1080p"), ("capability", "i2v")),
    )
    admission = await admit_storyboard_video_batch(
        project_name="demo",
        project={},
        script_file="episode_1.json",
        items=[("E1S01", {}), ("E1S02", {})],
        operation="generate_videos",
        selection=GenerationSelectionMode.MISSING_ONLY,
        video_request_facts=failure,
    )
    assert [ticket.unit_id for ticket in admission.tickets] == ["E1S01", "E1S02"]
    for ticket in admission.tickets:
        assert [(problem.code, problem.action, problem.params) for problem in ticket.problems] == [
            (failure.code, GenerationAction.CONFIGURE_PROVIDER, failure.parameters())
        ]
