"""The production workflow follows factory source and visual dependencies."""

import hashlib
import json
from pathlib import Path

from lib.artifacts.artifact_activation import activate_artifact_target_state
from lib.project.project_manager import ProjectManager
from lib.project.project_schema import CURRENT_PROJECT_SCHEMA_VERSION
from lib.script.short_drama_source import short_drama_source_relative_path
from lib.workflow.workflow_plan import build_workflow_plan
from lib.workflow.workflow_state import WorkflowStateService


def factory_project(
    tmp_path: Path, *, source_present: bool, prompts_present: bool
) -> tuple[Path, WorkflowStateService]:
    directory = tmp_path / "projects" / "demo"
    directory.mkdir(parents=True)
    project = {
        "schema_version": CURRENT_PROJECT_SCHEMA_VERSION,
        "title": "测试",
        "content_mode": "drama",
        "generation_mode": "storyboard",
        "characters": {},
        "scenes": {},
        "props": {},
        "episodes": [{"episode": 1, "script_file": "scripts/episode_1.json", "source_origin": "own"}],
    }
    script = {
        "episode": 1,
        "title": "测试",
        "content_mode": "drama",
        "scenes": [
            {
                "scene_id": "E1S01",
                "duration_seconds": 8,
                "characters_in_scene": [],
                "scenes": [],
                "props": [],
                "utterances": [],
                "source_text": "男人推门。",
                "scene_description": "推门",
                "image_prompt": "【镜头1】男人推门。 【镜头2】走到窗边。" if prompts_present else None,
                "video_prompt": None,
            }
        ],
    }
    source = directory / "source"
    source.mkdir()
    (source / "episode_1.txt").write_text("男人推门。", encoding="utf-8")
    if source_present:
        canonical = directory / short_drama_source_relative_path(1)
        canonical.parent.mkdir(parents=True)
        canonical.write_text("# 第1集\n男人推门。\n", encoding="utf-8")
        script["short_drama_meta"] = {"source_revision": hashlib.sha256(canonical.read_bytes()).hexdigest()}
    (directory / "project.json").write_text(json.dumps(project), encoding="utf-8")
    (directory / "scripts").mkdir()
    (directory / "scripts" / "episode_1.json").write_text(json.dumps(script), encoding="utf-8")
    activate_artifact_target_state(directory, bump_schema=False)
    return directory, WorkflowStateService(ProjectManager.for_project_dir(directory))


def test_historical_json_enters_factory_input(tmp_path: Path) -> None:
    _directory, service = factory_project(tmp_path, source_present=False, prompts_present=True)
    status = service.get_status("demo", 1)
    assert status.next_action.type == "collect_project_input"
    assert status.next_alternatives == []


def test_factory_source_without_prompts_enters_ai_storyboards(tmp_path: Path) -> None:
    _directory, service = factory_project(tmp_path, source_present=True, prompts_present=False)
    status = service.get_status("demo", 1)
    assert status.next_action.type == "plan_short_drama_storyboards"


def test_factory_plan_uses_per_shot_reference_materials(tmp_path: Path) -> None:
    _directory, service = factory_project(tmp_path, source_present=True, prompts_present=True)
    status = service.get_status("demo", 1)
    assert status.next_action.type == "generate_shot_grids"
    assert status.next_action.requested_ids == ["E1S01"]
    assert [step.id for step in build_workflow_plan(status).steps] == [
        "project_input",
        "asset_sheets",
        "final_script",
        "script_structure",
        "shot_grid",
        "video_prompt",
        "video",
        "edit",
    ]


def test_source_change_returns_to_ai_storyboards(tmp_path: Path) -> None:
    directory, service = factory_project(tmp_path, source_present=True, prompts_present=True)
    (directory / short_drama_source_relative_path(1)).write_text("# 第1集\n男人离开。\n", encoding="utf-8")
    assert service.get_status("demo", 1).next_action.type == "plan_short_drama_storyboards"
