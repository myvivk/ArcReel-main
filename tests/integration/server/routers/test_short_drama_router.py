from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lib.project.project_manager import ProjectManager
from server.auth import CurrentUserInfo, get_current_user
from server.error_handlers import register_error_handlers
from server.routers import short_drama
from tests.auth_deps import AUTH_DEPENDENCIES

MARKDOWN = """\
第【1】集：雨夜重逢
【本集时长】：8秒
【本集断章卡点】：门外的人竟然是她

[场景]：旧公寓
[人物]：林夏、周明
△ 门外暴雨倾盆，林夏握紧门把手。
周明（压低声音）：开门。
"""


def _client(monkeypatch, tmp_path: Path) -> tuple[TestClient, ProjectManager]:
    manager = ProjectManager(tmp_path / "projects")
    manager.create_project("demo", content_mode="drama")
    manager.create_project_metadata("demo", "Demo", "Anime", "drama")
    manager.update_project(
        "demo",
        lambda project: project.update(
            episodes=[{"episode": 1, "title": "", "source_origin": "none"}],
            episode_id_high_water=1,
        ),
    )
    monkeypatch.setattr(short_drama, "get_project_manager", lambda: manager)
    app = FastAPI()
    register_error_handlers(app)
    app.dependency_overrides[get_current_user] = lambda: CurrentUserInfo(
        id="default",
        sub="testuser",
        role="admin",
    )
    app.include_router(short_drama.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    return TestClient(app), manager


def test_source_legacy_state_save_and_revision_conflict(monkeypatch, tmp_path: Path) -> None:
    client, _manager = _client(monkeypatch, tmp_path)

    with client:
        legacy = client.get("/api/v1/projects/demo/episodes/1/short-drama-source")
        saved = client.put(
            "/api/v1/projects/demo/episodes/1/short-drama-source",
            json={"markdown": MARKDOWN, "expected_revision": None},
        )
        conflict = client.put(
            "/api/v1/projects/demo/episodes/1/short-drama-source",
            json={"markdown": f"{MARKDOWN}\n改动", "expected_revision": None},
        )

    assert legacy.status_code == 200
    assert legacy.json()["exists"] is False
    assert legacy.json()["path"] == "source/short_drama/episode_1.md"
    assert saved.status_code == 200
    assert saved.json()["exists"] is True
    assert saved.json()["revision"]
    assert conflict.status_code == 409


def test_reimport_uses_saved_source_and_registers_assets(monkeypatch, tmp_path: Path) -> None:
    client, manager = _client(monkeypatch, tmp_path)

    with client:
        saved = client.put(
            "/api/v1/projects/demo/episodes/1/short-drama-source",
            json={"markdown": MARKDOWN, "expected_revision": None},
        ).json()
        imported = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": saved["revision"], "confirm_artifacts": False},
        )

    assert imported.status_code == 200
    assert imported.json()["scenes"] == 1
    script = manager.load_script("demo", "episode_1.json")
    assert script["short_drama_meta"]["source_path"] == "source/short_drama/episode_1.md"
    assert script["short_drama_meta"]["source_revision"] == saved["revision"]
    project = manager.load_project("demo")
    assert "林夏" in project["characters"]
    assert "周明" in project["characters"]
    assert "旧公寓" in project["scenes"]


def test_initial_import_persists_markdown_source(monkeypatch, tmp_path: Path) -> None:
    client, manager = _client(monkeypatch, tmp_path)

    with client:
        imported = client.post(
            "/api/v1/projects/demo/short-drama/import",
            json={"episode_id": 1, "markdown": MARKDOWN},
        )
        source = client.get("/api/v1/projects/demo/episodes/1/short-drama-source")

    assert imported.status_code == 200
    assert source.status_code == 200
    assert source.json()["markdown"] == MARKDOWN
    assert (
        manager.load_script("demo", "episode_1.json")["short_drama_meta"]["source_revision"]
        == source.json()["revision"]
    )


def test_reimport_requires_confirmation_when_media_exists(monkeypatch, tmp_path: Path) -> None:
    client, manager = _client(monkeypatch, tmp_path)

    with client:
        imported = client.post(
            "/api/v1/projects/demo/short-drama/import",
            json={"episode_id": 1, "markdown": MARKDOWN},
        ).json()
        script = manager.load_script("demo", "episode_1.json")
        script["scenes"][0]["generated_assets"]["storyboard_image"] = "storyboards/scene_E1S1.png"
        manager.save_script("demo", script, "episode_1.json")
        refused = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": imported["source_revision"], "confirm_artifacts": False},
        )
        confirmed = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": imported["source_revision"], "confirm_artifacts": True},
        )

    assert refused.status_code == 409
    assert confirmed.status_code == 200
