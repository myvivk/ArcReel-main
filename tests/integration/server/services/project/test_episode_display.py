"""Web 的诊断改写不改变机器定位、正文或 Agent 诊断原文。"""

from lib.i18n import _
from lib.project.project_manager import ProjectManager
from server.services.project.episode_display import present_episode_diagnostics
from server.services.project.episode_item_refs import with_episode_item_refs


def test_task_failure_uses_ledger_title_without_changing_ids(tmp_path) -> None:
    projects = ProjectManager(tmp_path / "projects")
    projects.create_project("demo")
    projects.create_project_metadata("demo", "Demo", "Anime", "drama")
    projects.add_episode("demo", 9, "雨夜", "scripts/episode_9.json")
    projects.add_episode("demo", 3, "", "scripts/episode_3.json")
    task = {
        "project_name": "demo",
        "resource_id": "E9S01",
        "error_message": "集（id=9）的 E9S01 编写失败",
        "error_params": {"episode": 9, "unit_id": "E9S01"},
        "result": {"warnings": ["Episode (id=3): E3S02"]},
    }
    [shown] = with_episode_item_refs([task], id_field="resource_id", ref_field="resource_ref", projects=projects)
    assert "雨夜 · S01" in shown["error_message"]
    assert shown["resource_id"] == task["resource_id"]
    assert shown["error_params"] == task["error_params"]
    assert shown["resource_ref"]["episode_position"] == 1
    assert shown["result"]["warnings"] == ["第 2 集: 第 2 集 · S02"]
    assert "id=9" in task["error_message"]


def test_diagnostic_rewrite_preserves_identifiers_and_user_text() -> None:
    value = {
        "unit_id": "E9S01",
        "repair_reason": "episode script scripts/episode_9.json is invalid",
        "affected_ids": ["E9S01"],
        "params": {"unit_id": "E9S01", "episode": 9},
        "content": {"text": "E9S01 是用户原文"},
        "problems": [{"reason": "集（id=9）: scripts/episode_9.json / E9S01", "unit_id": "E9S01"}],
    }
    project = {"episodes": [{"episode": 9, "title": "雨夜"}]}
    shown = present_episode_diagnostics(value, project, _)
    assert shown["unit_id"] == value["unit_id"]
    assert shown["affected_ids"] == value["affected_ids"]
    assert shown["repair_reason"] == "episode script 「雨夜」的剧本 is invalid"
    assert shown["params"] == value["params"]
    assert shown["content"] == value["content"]
    assert "雨夜 · S01" in shown["problems"][0]["reason"]
    assert "episode_9" not in shown["problems"][0]["reason"]
    assert shown["problems"][0]["unit_id"] == "E9S01"
