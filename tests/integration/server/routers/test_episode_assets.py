"""「本集资产」：引用的已登记资产，与一键「抽取 → 登记 → 生成图」。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lib.custom_provider import make_provider_id
from lib.db.repositories.custom_provider_repo import CustomProviderRepository
from lib.generation.generation_queue import GenerationQueue
from lib.infra.json_io import atomic_write_json
from lib.project.project_manager import ProjectManager
from server.auth import CurrentUserInfo, get_current_user
from server.error_handlers import register_error_handlers
from server.routers import episode_assets
from tests.auth_deps import AUTH_DEPENDENCIES, override_auth

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


class TestScriptAssetNames:
    def test_reads_each_type_from_its_own_reference_arrays(self):
        script = _script()

        assert episode_assets.script_asset_names(script) == {
            "character": ["阿岚"],
            "scene": ["茶楼"],
            "prop": ["玉佩"],
        }

    def test_a_speaker_without_a_reference_array_is_not_a_registration(self):
        # 说话人可以是未登记的群演：它不构成「要登记这个资产」。
        script = _script()
        script["scenes"][0]["characters_in_scene"] = []
        script["scenes"][0]["utterances"] = [{"speaker": "路人", "text": "让一让"}]

        assert episode_assets.script_asset_names(script)["character"] == []


@pytest.fixture
async def generate_client(
    session_factory, tmp_path: Path, monkeypatch
) -> AsyncIterator[tuple[httpx.AsyncClient, ProjectManager, Path]]:
    pm = ProjectManager(tmp_path / "projects")
    pm.create_project("demo")
    pm.create_project_metadata("demo", "Demo", "Anime", "narration")
    pm.update_project("demo", lambda project: project.update({"episodes": [_episode()]}))
    queue = GenerationQueue(session_factory=session_factory, project_manager=pm)
    await queue.acquire_or_renew_worker_lease(name="default", owner_id="worker-a", ttl_seconds=60)
    async with session_factory() as session:
        provider = await CustomProviderRepository(session).create_provider(
            display_name="Images",
            discovery_format="openai",
            base_url="https://api.example.com",
            api_key="test",
            models=[
                {
                    "model_id": "text-image",
                    "display_name": "Text image",
                    "endpoint": "openai-images-generations",
                    "price_unit": "image",
                    "price_input": 0.04,
                    "currency": "USD",
                }
            ],
        )
        provider_id = make_provider_id(provider.id)
        await session.commit()
    pm.update_project("demo", lambda project: project.update({"image_provider_t2i": f"{provider_id}/text-image"}))
    monkeypatch.setattr(episode_assets, "get_project_manager", lambda: pm)
    monkeypatch.setattr(episode_assets, "get_generation_queue", lambda: queue)
    app = FastAPI()
    register_error_handlers(app)
    override_auth(app)
    app.include_router(episode_assets.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http, pm, pm.get_project_path("demo")


class TestGenerateEpisodeAssets:
    async def test_registers_the_referenced_names_then_queues_the_episode_batch(self, generate_client):
        client, pm, project_path = generate_client
        # 场景已登记且有描述，能直接出图；角色与道具只在剧本里出现，本条链路补登记。
        pm.add_project_scene("demo", "茶楼", "临街二层")
        atomic_write_json(project_path / SCRIPT_FILE, _script())

        response = await client.post("/api/v1/projects/demo/episodes/1/generate-assets")

        assert response.status_code == 200
        body = response.json()
        assert body["registered"] == {"character": ["阿岚"], "prop": ["玉佩"]}
        assert body["conflicts"] == []
        project = pm.load_project("demo")
        assert "阿岚" in project["characters"]
        assert "玉佩" in project["props"]

        members = {member["unit_id"]: member for member in body["members"]}
        # 补登记的资产没有描述，进不了这一批；已登记有描述的场景拿到了任务。
        assert members["scene/茶楼"]["task_id"]
        assert members["character/阿岚"]["status"] == "blocked"
        assert members["prop/玉佩"]["status"] == "blocked"
        assert [item["unit_id"] for item in body["without_description"]] == [
            "character/阿岚",
            "prop/玉佩",
        ]

    async def test_an_episode_without_its_formal_script_is_rejected(self, generate_client):
        client, _pm, _project_path = generate_client

        response = await client.post("/api/v1/projects/demo/episodes/1/generate-assets")

        assert response.status_code == 409
