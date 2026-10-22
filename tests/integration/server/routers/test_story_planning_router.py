from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lib.project.project_manager import ProjectManager, get_project_manager
from lib.short_drama_ingest import ingest_short_drama_artifacts
from server.routers.story_planning import router
from tests.short_drama_support import factory_package

pytestmark = pytest.mark.usefixtures("factory_profile")


def test_factory_reader_and_continuation_ignore_native_story_plan(tmp_path: Path) -> None:
    manager = ProjectManager(tmp_path / "projects")
    manager.create_project("demo", content_mode="drama")
    manager.update_project("demo", lambda project: project.update(story_plan={"premise": "NATIVE-MUST-NOT-LEAK"}))
    first = factory_package()
    second = [(path, raw) for path, raw in factory_package(episode=2) if "/" in path]
    ingest_short_drama_artifacts(manager, "demo", first + second)
    app = FastAPI()
    app.dependency_overrides[get_project_manager] = lambda: manager
    app.include_router(router)

    with TestClient(app) as client:
        reader = client.get("/projects/demo/story-planning").json()
        context = client.get("/projects/demo/story-planning/context/2").json()["context"]

    assert reader["character_bible"]["content"] == dict(first)["character-bible.md"].decode()
    assert reader["series_outline"]["content"] == dict(first)["series-outline.md"].decode()
    assert reader["ledger"]["content"] == dict(first)["ledger.md"].decode()
    assert [plan["episode"] for plan in reader["episode_plans"]] == [1, 2]
    assert "NATIVE-MUST-NOT-LEAK" not in context
    assert "episodes/episode_01.md" in context
    assert "episodes/episode_02.md" not in context
    assert "episode-plan/episode_02.plan.md" in context
    assert "reports/" not in context
    assert "compliance/" not in context


def test_empty_factory_context_does_not_fall_back_to_native_plan(tmp_path: Path) -> None:
    manager = ProjectManager(tmp_path / "projects")
    manager.create_project("demo", content_mode="drama")
    manager.update_project("demo", lambda project: project.update(story_plan={"premise": "旧规划"}))
    app = FastAPI()
    app.dependency_overrides[get_project_manager] = lambda: manager
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get("/projects/demo/story-planning/context/1").json() == {"context": ""}
