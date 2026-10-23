"""AI 操作入口与制作状态共用一份准入：同一条件下，入口的拒绝理由码等于状态里该操作的理由码。"""

from __future__ import annotations

from pathlib import Path

from lib.project.project_manager import ProjectManager
from lib.workflow.workflow_plan import WorkflowPlanRequest
from server.services.project.workflow_planner import WorkflowPlanner


def _project(tmp_path: Path, content_mode: str) -> ProjectManager:
    projects = ProjectManager(tmp_path / "projects")
    projects.create_project("demo", content_mode=content_mode)
    projects.create_project_metadata("demo", "Demo", "", content_mode)
    return projects


async def test_unreadable_project_data_is_returned_as_a_plan_blocker(tmp_path: Path) -> None:
    projects = _project(tmp_path, "narration")
    (projects.get_project_path("demo") / "project.json").write_text("{", encoding="utf-8")

    plan = await WorkflowPlanner(projects).get_plan("demo", WorkflowPlanRequest(episode_id=1))

    assert [blocker.code for blocker in plan.blockers] == ["project_data_unavailable"]
    assert plan.status.content is None
    assert plan.steps[0].state == "blocked"
    assert plan.next_action.type == "none"
