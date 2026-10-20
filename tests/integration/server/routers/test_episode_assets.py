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
from server.services import asset_ai_fill
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
        assert resp.json()["has_script"] is False
        assert resp.json()["assets"] == []
        # 没有本集剧本（正式脚本与工厂源稿都没有）：本批清单为空。
        assert resp.json()["plan"] == {
            "total": 0,
            "by_type": {"character": 0, "scene": 0, "prop": 0},
            "text_calls": 0,
            "image_calls": 0,
        }


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


def _write_source_markdown(project_path: Path) -> None:
    """本集剧本（工厂源稿）：``[人物]/[场景]/[道具]`` 标记行自带外观描述。"""

    path = project_path / "source" / "short_drama" / "episodes" / "episode_01.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[场景]：城郊仓库｜水泥地，铁门单侧顶光\n"
        "[人物]：林辰（隐忍龙王｜三十岁上下，寸头，旧夹克磨白）\n"
        "[道具]：铁门｜锈迹斑斑，门轴下垂\n"
        "△ 林辰推开铁门。\n",
        encoding="utf-8",
    )


def _write_story_plan(project_path: Path) -> None:
    """故事规划：人物圣经的角色清单 + 分集规划里本集的资产提及。"""

    root = project_path / "source" / "short_drama"
    root.mkdir(parents=True, exist_ok=True)
    (root / "character-bible.md").write_text(
        "【角色】：林辰\n【外形】：三十岁上下，寸头\n【角色】：陈峰\n【外形】：宽脸浓眉\n",
        encoding="utf-8",
    )
    (root / "episode-plan.md").write_text(
        "第 1 集\n[人物]：陈峰\n[场景]：城郊仓库\n\n第 2 集\n[人物]：路人甲\n",
        encoding="utf-8",
    )


class TestEpisodeAssetExtraction:
    """抽取的三个来源：正式脚本引用数组、工厂源稿标记行、故事规划。"""

    def test_marker_lines_of_the_factory_source_are_extracted(self, tmp_path, monkeypatch):
        client, pm, project_path = _client(monkeypatch, tmp_path, episodes=[_episode()])
        _write_source_markdown(project_path)

        assert episode_assets.source_markdown_asset_names(pm, "demo", 1) == {
            "character": ["林辰"],
            "scene": ["城郊仓库"],
            "prop": ["铁门"],
        }
        # 没有本集剧本（正式脚本 + 工厂源稿都没有）的集：抽不到名字。
        assert episode_assets.source_markdown_asset_names(pm, "demo", 2) == {
            "character": [],
            "scene": [],
            "prop": [],
        }
        client.close()

    def test_story_plan_names_are_scoped_to_this_episode(self, tmp_path, monkeypatch):
        _client_instance, _pm, project_path = _client(monkeypatch, tmp_path, episodes=[_episode()])
        _client_instance.close()
        _write_story_plan(project_path)

        assert episode_assets.story_plan_asset_names(project_path, episode=1) == {
            "character": ["林辰", "陈峰"],
            "scene": ["城郊仓库"],
            "prop": [],
        }
        # 第 2 集的规划不该算进第 1 集：本集没有点名的名字不进本集资产。
        assert episode_assets.story_plan_asset_names(project_path, episode=2)["character"] == ["林辰", "陈峰", "路人甲"]

    def test_the_plan_preview_counts_the_batch(self, tmp_path, monkeypatch):
        client, pm, project_path = _client(monkeypatch, tmp_path, episodes=[_episode()])
        atomic_write_json(project_path / SCRIPT_FILE, _script())
        _write_source_markdown(project_path)
        _write_story_plan(project_path)
        # 茶楼已登记且有描述：不占文本模型调用（AI 只填空字段）。
        pm.add_project_scene("demo", "茶楼", "临街二层")

        with client:
            resp = client.get("/api/v1/projects/demo/episodes/1/referenced-assets")

        assert resp.status_code == 200
        body = resp.json()
        assert body["plan"]["total"] == 7
        assert body["plan"]["by_type"] == {"character": 3, "scene": 2, "prop": 2}
        assert body["plan"]["text_calls"] == 6
        assert body["plan"]["image_calls"] == 7


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


def _patch_fill(monkeypatch, *, description: str = "剧本补全出的描述", fail: bool = False, sink=None):
    """替掉文本模型调用：补全链路本身（并发、按本集取上下文、批量写回）仍走真实代码。"""

    async def _fake(project_name: str, asset_type: str, name: str, context: dict) -> asset_ai_fill.AssetFillResponse:
        if sink is not None:
            sink.append((asset_type, name, context))
        if fail:
            raise RuntimeError("text model unavailable")
        return asset_ai_fill.AssetFillResponse(description=description)

    monkeypatch.setattr(asset_ai_fill, "generate_asset_fill", _fake)


class TestGenerateEpisodeAssets:
    async def test_fills_descriptions_then_queues_the_episode_batch(self, generate_client, monkeypatch):
        client, pm, project_path = generate_client
        # 场景已登记且有描述；角色与道具只在剧本里出现，本条链路补登记并补描述。
        pm.add_project_scene("demo", "茶楼", "临街二层")
        atomic_write_json(project_path / SCRIPT_FILE, _script())
        fills: list[tuple[str, str, dict]] = []
        _patch_fill(monkeypatch, sink=fills)

        response = await client.post("/api/v1/projects/demo/episodes/1/generate-assets")

        assert response.status_code == 200
        body = response.json()
        assert body["registered"] == {"character": ["阿岚"], "prop": ["玉佩"]}
        assert body["conflicts"] == []

        # 只补缺描述的：已登记有描述的场景不进补全名单，描述也不被动过。
        assert {(asset_type, name) for asset_type, name, _ in fills} == {("character", "阿岚"), ("prop", "玉佩")}
        # D-35：本集剧本是补全依据，本集摘录单独成组且排在项目设定之前。
        assert all(context["focus_episode"] == "1" for _, _, context in fills)
        assert all(context["episode_excerpts"] for _, _, context in fills)
        assert body["description_filled"] == [
            {"asset_type": "character", "name": "阿岚"},
            {"asset_type": "prop", "name": "玉佩"},
        ]

        project = pm.load_project("demo")
        assert project["characters"]["阿岚"]["description"] == "剧本补全出的描述"
        assert project["props"]["玉佩"]["description"] == "剧本补全出的描述"
        assert project["scenes"]["茶楼"]["description"] == "临街二层"

        # 补上描述后三者都进得了这一批，不再有「登记了却出不了图」。
        members = {member["unit_id"]: member for member in body["members"]}
        assert members["scene/茶楼"]["task_id"]
        assert members["character/阿岚"]["task_id"]
        assert members["prop/玉佩"]["task_id"]
        assert body["without_description"] == []

    async def test_a_failed_fill_is_reported_and_blocks_only_that_asset(self, generate_client, monkeypatch):
        client, pm, project_path = generate_client
        pm.add_project_scene("demo", "茶楼", "临街二层")
        atomic_write_json(project_path / SCRIPT_FILE, _script())
        _patch_fill(monkeypatch, fail=True)

        response = await client.post("/api/v1/projects/demo/episodes/1/generate-assets")

        assert response.status_code == 200
        body = response.json()
        assert {(item["asset_type"], item["name"]) for item in body["description_fill_failed"]} == {
            ("character", "阿岚"),
            ("prop", "玉佩"),
        }
        assert body["description_filled"] == []
        members = {member["unit_id"]: member for member in body["members"]}
        # 补全失败不拖垮别的资产：已登记有描述的场景照常出图。
        assert members["scene/茶楼"]["task_id"]
        assert members["character/阿岚"]["status"] == "blocked"
        assert [item["unit_id"] for item in body["without_description"]] == ["character/阿岚", "prop/玉佩"]
        assert pm.load_project("demo")["characters"]["阿岚"]["description"] == ""

    async def test_registers_the_names_only_the_source_and_the_plan_wrote(self, generate_client, monkeypatch):
        # 本集还没有正式脚本：工厂源稿标记行与故事规划照样能抽出名字并登记（D-33 / D-36）。
        client, pm, project_path = generate_client
        _write_source_markdown(project_path)
        _write_story_plan(project_path)
        _patch_fill(monkeypatch)

        response = await client.post("/api/v1/projects/demo/episodes/1/generate-assets")

        assert response.status_code == 200
        body = response.json()
        # 剧本（工厂源稿）与故事规划里写了、资产表里没有的名字都补登记。
        assert body["registered"] == {
            "character": ["林辰", "陈峰"],
            "scene": ["城郊仓库"],
            "prop": ["铁门"],
        }
        assert {(item["asset_type"], item["name"]) for item in body["description_filled"]} == {
            ("character", "林辰"),
            ("character", "陈峰"),
            ("scene", "城郊仓库"),
            ("prop", "铁门"),
        }
        project = pm.load_project("demo")
        assert project["characters"]["陈峰"]["description"] == "剧本补全出的描述"

    async def test_an_episode_without_any_script_is_rejected(self, generate_client):
        client, _pm, _project_path = generate_client

        response = await client.post("/api/v1/projects/demo/episodes/1/generate-assets")

        assert response.status_code == 409
