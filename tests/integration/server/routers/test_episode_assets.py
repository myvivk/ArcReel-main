"""「本集资产」：GET /projects/{name}/episodes/{episode}/referenced-assets。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lib.infra.json_io import atomic_write_json
from lib.project.project_manager import ProjectManager
from server.auth import CurrentUserInfo, get_current_user
from server.error_handlers import register_error_handlers
from server.routers import episode_assets
from tests.auth_deps import AUTH_DEPENDENCIES

SCRIPT_FILE = "scripts/episode_1.json"


def _client(monkeypatch, tmp_path: Path, **fields) -> tuple[TestClient, ProjectManager, Path]:
    pm = ProjectManager(tmp_path / "projects")
    pm.create_project("demo")
    pm.create_project_metadata("demo", "Demo", "Anime", "narration")
    if fields:
        pm.update_project("demo", lambda project: project.update(**fields))
    monkeypatch.setattr(episode_assets, "get_project_manager", lambda: pm)
    app = FastAPI()
    register_error_handlers(app)
    app.dependency_overrides[get_current_user] = lambda: CurrentUserInfo(id="default", sub="testuser", role="admin")
    app.include_router(episode_assets.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    return TestClient(app), pm, pm.get_project_path("demo")


def _episode(episode: int = 1, script_file: str = SCRIPT_FILE) -> dict:
    return {
        "episode": episode,
        "title": f"第 {episode} 集",
        "script_file": script_file,
        "source_origin": "none",
    }


def _script(*, with_derivative: bool = True) -> dict:
    character = "阿岚/战损" if with_derivative else "阿岚"
    return {
        "episode": 1,
        "title": "第一集",
        "content_mode": "drama",
        "scenes": [
            {
                "scene_id": "S1",
                "characters_in_scene": [character],
                "scenes": ["茶楼"],
                "props": ["玉佩"],
                "utterances": [{"speaker": "阿岚", "text": "我来了"}],
            }
        ],
    }


class TestReferencedAssets:
    def test_lists_the_registered_assets_the_script_names(self, tmp_path, monkeypatch):
        client, pm, project_path = _client(monkeypatch, tmp_path, episodes=[_episode()])
        pm.add_character("demo", "阿岚", "白衣少女")
        pm.update_project(
            "demo",
            lambda project: project["characters"]["阿岚"].update(
                {"derivatives": {"战损": {"description": "衣服破损"}}}
            ),
        )
        pm.add_project_scene("demo", "茶楼", "临街二层")
        pm.add_prop("demo", "玉佩", "温润白玉")
        pm.add_prop("demo", "别集的道具", "只在别集出现")
        atomic_write_json(project_path / SCRIPT_FILE, _script())

        with client:
            resp = client.get("/api/v1/projects/demo/episodes/1/referenced-assets")

        assert resp.status_code == 200
        body = resp.json()
        assert body["has_script"] is True
        # 只列本体：衍生是本体资产图的一次编辑，不单独成条；未被引用的道具不出现。
        assert [(item["asset_type"], item["asset_name"]) for item in body["assets"]] == [
            ("character", "阿岚"),
            ("prop", "玉佩"),
            ("scene", "茶楼"),
        ]
        assert body["assets"][0]["asset"]["description"] == "白衣少女"

    def test_an_episode_without_its_script_file_has_no_assets(self, tmp_path, monkeypatch):
        client, _pm, _project_path = _client(monkeypatch, tmp_path, episodes=[_episode()])

        with client:
            resp = client.get("/api/v1/projects/demo/episodes/1/referenced-assets")

        assert resp.status_code == 200
        assert resp.json() == {"episode": 1, "has_script": False, "assets": []}


class TestReferencedAssetsLookups:
    def test_unknown_project_and_unknown_episode_are_not_found(self, tmp_path, monkeypatch):
        client, _pm, _project_path = _client(monkeypatch, tmp_path, episodes=[_episode()])

        with client:
            assert client.get("/api/v1/projects/missing/episodes/1/referenced-assets").status_code == 404
            assert client.get("/api/v1/projects/demo/episodes/9/referenced-assets").status_code == 404
