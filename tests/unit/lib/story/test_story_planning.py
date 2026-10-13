from pathlib import Path

import pytest

from lib.artifacts.artifact_activation import resolve_current_artifact_basis
from lib.artifacts.artifact_manifest import ArtifactKey
from lib.infra.api_errors import ConflictError, UnprocessableError
from lib.infra.json_io import atomic_write_json
from lib.project.project_manager import ProjectManager
from lib.story.story_planning import (
    StoryPlan,
    StoryRecord,
    authoring_context,
    extract_script_events,
    save_plan,
    snapshot,
)


@pytest.fixture
def manager(tmp_path: Path) -> ProjectManager:
    pm = ProjectManager(tmp_path)
    pm.create_project("story")
    pm.create_project_metadata("story", "Story", "Anime", "drama")

    def setup(project: dict) -> None:
        project["characters"] = {"甲": {"description": "青年"}, "乙": {"description": "医生"}}
        project["episodes"] = [
            {"episode": ep, "title": title, "script_file": f"scripts/episode_{ep}.json"}
            for ep, title in ((8, "起点"), (2, "转折"), (5, "结尾"))
        ]

    pm.update_project("story", setup)
    for ep in (8, 2, 5):
        atomic_write_json(
            pm.get_project_path("story") / "scripts" / f"episode_{ep}.json",
            {"episode": ep, "content_mode": "drama", "scenes": []},
        )
    return pm


def test_context_uses_broadcast_order_and_separates_plan_from_facts(manager: ProjectManager):
    plan = StoryPlan(
        records=[
            StoryRecord(id="past", kind="fact", text="甲已受伤", characters=["甲"], episode=8, status="confirmed"),
            StoryRecord(
                id="now", kind="event", text="甲向乙求助", characters=["甲", "乙"], episode=2, status="confirmed"
            ),
            StoryRecord(id="end", kind="fact", text="甲已痊愈", episode=2, status="confirmed"),
            StoryRecord(id="future", kind="event", text="乙揭晓身份", episode=5, status="confirmed"),
            StoryRecord(id="pending", kind="relationship", text="乙是幕后主谋", characters=["甲", "乙"]),
        ]
    )
    save_plan(manager, "story", plan, snapshot(manager, "story").revision)
    context = authoring_context(manager, "story", 2)
    assert "甲已受伤" in context
    assert "甲向乙求助" in context
    assert all(text not in context for text in ("甲已痊愈", "乙揭晓身份", "乙是幕后主谋"))
    assert "甲已痊愈" in authoring_context(manager, "story", 5)


def test_compare_and_save_rejects_lost_updates_and_dangling_references(manager: ProjectManager):
    initial = snapshot(manager, "story")
    save_plan(manager, "story", StoryPlan(premise="寻找真相"), initial.revision)
    with pytest.raises(ConflictError):
        save_plan(manager, "story", StoryPlan(premise="另一个结局"), initial.revision)
    current = snapshot(manager, "story")
    with pytest.raises(UnprocessableError):
        save_plan(
            manager,
            "story",
            StoryPlan(records=[StoryRecord(id="bad", kind="fact", text="不存在的集", episode=999)]),
            current.revision,
        )
    assert snapshot(manager, "story").plan.premise == "寻找真相"


def test_extract_preserves_confirmation_until_narrative_source_changes(manager: ProjectManager):
    script_path = manager.get_project_path("story") / "scripts" / "episode_8.json"
    atomic_write_json(
        script_path,
        {
            "episode": 8,
            "title": "起点",
            "content_mode": "drama",
            "scenes": [
                {
                    "scene_id": "E8S01",
                    "duration_seconds": 4,
                    "characters_in_scene": ["甲"],
                    "scenes": [],
                    "props": [],
                    "scene_description": "甲捂住受伤的手臂",
                    "utterances": [],
                    "source_text": "甲受伤了",
                }
            ],
        },
    )
    initial = snapshot(manager, "story")
    extracted = extract_script_events(manager, "story", initial.revision)
    assert len(extracted.plan.records) == 1
    record = extracted.plan.records[0]
    assert record.status == "pending"
    assert record.source_id == "E8S01"
    record.status = "confirmed"
    confirmed = save_plan(manager, "story", extracted.plan, extracted.revision)
    assert "甲捂住受伤的手臂" in authoring_context(manager, "story", 8)
    again = extract_script_events(manager, "story", confirmed.revision)
    assert again.plan.records[0].status == "confirmed"
    script = manager.load_script_readonly("story", "episode_8.json")
    script["scenes"][0]["image_prompt"] = {"scene": "改变画面"}
    atomic_write_json(script_path, script)
    assert snapshot(manager, "story").stale_ids == []
    script["scenes"][0]["scene_description"] = "甲的手臂没有受伤"
    atomic_write_json(script_path, script)
    stale = snapshot(manager, "story")
    assert stale.stale_ids == [record.id]
    assert authoring_context(manager, "story", 8) == ""
    refreshed = extract_script_events(manager, "story", stale.revision)
    assert len(refreshed.plan.records) == 1
    assert refreshed.plan.records[0].status == "pending"
    assert refreshed.plan.records[0].text == "甲的手臂没有受伤"


def test_visual_scope_and_budget_never_cut_records(manager: ProjectManager):
    plan = StoryPlan(
        context_budget=400,
        premise="结局身份揭晓",
        records=[
            StoryRecord(
                id="related", kind="relationship", text="甲信任乙", characters=["甲", "乙"], status="confirmed"
            ),
            StoryRecord(
                id="large", kind="event", text="长事件" * 200, characters=["甲"], episode=2, status="confirmed"
            ),
        ],
    )
    save_plan(manager, "story", plan, snapshot(manager, "story").revision)
    context = authoring_context(manager, "story", 2, characters={"甲"}, visual=True)
    assert "甲信任乙" in context
    assert "长事件" not in context
    assert "结局身份揭晓" not in context
    assert len(context) <= 400
    assert authoring_context(manager, "story", 2, characters={"丙"}, visual=True) == ""


def test_episode_script_basis_tracks_only_its_scoped_story_context(manager: ProjectManager):
    script_path = manager.get_project_path("story") / "scripts" / "episode_8.json"
    atomic_write_json(
        script_path,
        {
            "episode": 8,
            "content_mode": "drama",
            "scenes": [{"scene_id": "E8S01", "characters_in_scene": ["甲", "乙"]}],
        },
    )
    plan = StoryPlan(
        records=[
            StoryRecord(
                id="relationship",
                kind="relationship",
                text="甲信任乙",
                characters=["甲", "乙"],
                status="confirmed",
            ),
            StoryRecord(id="future", kind="event", text="结局揭晓", episode=5, status="confirmed"),
        ]
    )
    current = save_plan(manager, "story", plan, snapshot(manager, "story").revision)
    baseline = resolve_current_artifact_basis(
        manager.get_project_path("story"),
        ArtifactKey.episode_script(8),
    )
    assert baseline is not None

    plan.records[1].text = "结局改变"
    current = save_plan(manager, "story", plan, current.revision)
    future_changed = resolve_current_artifact_basis(
        manager.get_project_path("story"),
        ArtifactKey.episode_script(8),
    )
    assert future_changed is not None
    assert future_changed.digest == baseline.digest

    plan.records[0].text = "甲不再信任乙"
    save_plan(manager, "story", plan, current.revision)
    relationship_changed = resolve_current_artifact_basis(
        manager.get_project_path("story"),
        ArtifactKey.episode_script(8),
    )
    assert relationship_changed is not None
    assert relationship_changed.digest != baseline.digest
