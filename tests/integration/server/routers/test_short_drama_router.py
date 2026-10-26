from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lib.backends.text_generator import TextGenerator
from lib.project.project_manager import ProjectManager
from lib.script.short_drama_import import beat_catalog, parse_short_drama_episode
from lib.short_drama_ingest import ingest_short_drama_artifacts
from server.auth import CurrentUserInfo, get_current_user
from server.error_handlers import register_error_handlers
from server.routers import short_drama
from tests.auth_deps import AUTH_DEPENDENCIES
from tests.fakes import FakeTextGenerator
from tests.short_drama_support import factory_package

pytestmark = pytest.mark.usefixtures("factory_profile")


def _episode_markdown(newline: str = "\n") -> str:
    """合规单集：105 秒 53 拍，角色与道具都带外观，能通过工厂机检。"""
    lines = [
        "第【1】集：雨夜重逢",
        "【本集时长】：105 秒",
        "[场景]：旧公寓｜水泥楼道，声控灯忽明忽暗",
        "[人物]：林夏（女主｜齐肩短发，米色风衣，右腕有旧疤）、周明（前夫｜寸头，深灰西装，领口松开）",
        "[道具]：铁门｜锈迹斑斑，门轴下垂",
        "△ 铁门被砸得连响。",
    ]
    lines += [f"△ 铁门第{index}下被敲响。" for index in range(1, 49)]
    lines += [
        "林夏（压声）：谁在外面？",
        "周明：开门，我回来了。",
        "林夏：你走错门了。",
        "周明：我带了钥匙。",
        "【本集断章卡点】：门缝里塞进一张旧照片。（黑屏：下滑解锁第2集）",
    ]
    return newline.join(lines) + newline


MARKDOWN = _episode_markdown()


def _client(monkeypatch, tmp_path: Path, *, assets_ready: bool = False) -> tuple[TestClient, ProjectManager]:
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
    if assets_ready:
        _register_storyboard_assets(manager)
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


def _register_storyboard_assets(manager: ProjectManager) -> None:
    from lib.script.short_drama_import import referenced_asset_descriptions

    for asset_type, descriptions in referenced_asset_descriptions(parse_short_drama_episode(MARKDOWN)).items():
        manager.upsert_assets(
            "demo",
            {"character": "characters", "scene": "scenes", "prop": "props"}[asset_type],
            {name: {"description": description} for name, description in descriptions.items()},
        )


def test_ingest_preserves_bytes_and_does_not_project(monkeypatch, tmp_path: Path) -> None:
    client, manager = _client(monkeypatch, tmp_path)
    original = _episode_markdown("\r\n").encode("utf-8")
    package = factory_package(original.decode("utf-8"))
    ingest_short_drama_artifacts(manager, "demo", package)
    with client:
        listed = client.get("/api/v1/projects/demo/short-drama/artifacts")

    assert listed.status_code == 200
    project_path = manager.get_project_path("demo")
    assert (project_path / "source/short_drama/episodes/episode_01.md").read_bytes() == original
    assert not (project_path / "characters.json").exists()
    assert {item["path"] for item in listed.json()["artifacts"]} == {path for path, _ in package}
    assert (project_path / "source/episode_1.txt").read_bytes() == original
    project = manager.load_project("demo")
    assert not project.get("story_plan")
    assert not project.get("characters")
    assert not project.get("scenes")
    assert not project.get("props")
    assert not (project_path / "scripts/episode_1.json").exists()


def test_ingested_episode_artifact_visible_as_short_drama_source(monkeypatch, tmp_path: Path) -> None:
    """复现：工厂工件包入库后，分集页经 getShortDramaSource 应能读到本集正文。"""
    client, manager = _client(monkeypatch, tmp_path)
    original = MARKDOWN.encode("utf-8")
    ingest_short_drama_artifacts(manager, "demo", factory_package(original.decode("utf-8")))
    with client:
        source_resp = client.get("/api/v1/projects/demo/episodes/1/short-drama-source")

    assert source_resp.status_code == 200
    body = source_resp.json()
    assert body["exists"] is True, body
    assert "雨夜重逢" in body["markdown"]


def test_ingest_registers_episode_as_own_source(monkeypatch, tmp_path: Path) -> None:
    """复现断点：入库后每集工厂 Markdown 须登记为该集 OWN 源文本，分集页本集正文才有内容、下游才建得上。"""
    _http, manager = _client(monkeypatch, tmp_path)
    original = MARKDOWN.encode("utf-8")
    ingest_short_drama_artifacts(manager, "demo", factory_package(original.decode("utf-8")))
    project = manager.load_project("demo")
    entry = next(e for e in project["episodes"] if e.get("episode") == 1)
    assert entry.get("source_origin") == "own", entry
    source_file = manager.get_project_path("demo") / "source" / "episode_1.txt"
    assert source_file.read_text(encoding="utf-8") == original.decode("utf-8")


def test_project_registration_failure_rolls_back_entire_artifact_batch(monkeypatch, tmp_path: Path) -> None:
    from lib.infra.json_io import atomic_write_json

    _http, manager = _client(monkeypatch, tmp_path)
    project_path = manager.get_project_path("demo")
    before = manager.load_project("demo")

    def fail_metadata(path, data, **kwargs):
        if path.name == "project.json":
            raise OSError("metadata disk full")
        atomic_write_json(path, data, **kwargs)

    monkeypatch.setattr("lib.project.project_manager.atomic_write_json", fail_metadata)
    with pytest.raises(OSError, match="metadata disk full"):
        ingest_short_drama_artifacts(manager, "demo", factory_package())

    assert manager.load_project("demo") == before
    assert not list((project_path / "source/short_drama").rglob("*.md"))
    assert not (project_path / "source/episode_1.txt").exists()


def test_missing_episode_keeps_artifacts_without_creating_ledger_entry(monkeypatch, tmp_path: Path) -> None:
    _http, manager = _client(monkeypatch, tmp_path)
    ingest_short_drama_artifacts(manager, "demo", factory_package(episode=2))
    assert (manager.get_project_path("demo") / "source/short_drama/episodes/episode_02.md").exists()
    assert [entry["episode"] for entry in manager.load_project("demo")["episodes"]] == [1]
    assert not (manager.get_project_path("demo") / "source/episode_2.txt").exists()


def test_source_empty_state_is_read_only(monkeypatch, tmp_path: Path) -> None:
    client, _manager = _client(monkeypatch, tmp_path)

    with client:
        legacy = client.get("/api/v1/projects/demo/episodes/1/short-drama-source")
        refused = client.put(
            "/api/v1/projects/demo/episodes/1/short-drama-source",
            json={"markdown": MARKDOWN, "expected_revision": None},
        )

    assert legacy.status_code == 200
    assert legacy.json()["exists"] is False
    assert legacy.json()["path"] == "source/short_drama/episodes/episode_01.md"
    assert refused.status_code == 405


def test_reimport_uses_saved_source_without_registering_assets(monkeypatch, tmp_path: Path) -> None:
    client, manager = _client(monkeypatch, tmp_path, assets_ready=True)
    before = manager.load_project("demo")
    monkeypatch.setattr(TextGenerator, "create", FakeTextGenerator(_plan_payload(MARKDOWN)).create)

    with client:
        revision = _save_source(client, manager)
        imported = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": revision, "confirm_artifacts": False},
        )

    assert imported.status_code == 200
    assert imported.json()["scenes"] >= 1
    script = manager.load_script("demo", "episode_1.json")
    assert script["short_drama_meta"]["source_path"] == "source/short_drama/episodes/episode_01.md"
    assert script["short_drama_meta"]["source_revision"] == revision
    # AI 分镜是唯一路径：reimport 恒走文本模型并留痕 smart。
    assert script["short_drama_meta"]["ai_storyboard_options"]["mode"] == "smart"
    assert all(12 <= scene["duration_seconds"] <= 15 for scene in script["scenes"]), [
        scene["duration_seconds"] for scene in script["scenes"]
    ]
    project = manager.load_project("demo")
    assert "林夏" in project["characters"]
    assert "周明" in project["characters"]
    assert "旧公寓" in project["scenes"]
    assert "铁门" in project["props"]
    assert project["characters"]["林夏"]["description"] == "齐肩短发，米色风衣，右腕有旧疤"
    assert "assets_without_description" not in imported.json()
    for bucket in ("characters", "scenes", "props"):
        assert project[bucket] == before[bucket]
    assert "registered_assets" not in imported.json()


def test_reimport_refuses_missing_assets_before_calling_model(monkeypatch, tmp_path: Path) -> None:
    client, manager = _client(monkeypatch, tmp_path)
    manager.update_project(
        "demo",
        lambda project: project.setdefault("characters", {}).update({"周明": {"description": ""}}),
    )
    model = FakeTextGenerator(_plan_payload(MARKDOWN))
    monkeypatch.setattr(TextGenerator, "create", model.create)

    with client:
        revision = _save_source(client, manager)
        imported = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": revision, "confirm_artifacts": False},
        )

    assert imported.status_code == 422
    assert "资产步骤" in imported.json()["detail"]
    assert model.requests == []
    assert not (manager.get_project_path("demo") / "scripts/episode_1.json").exists()


@pytest.mark.parametrize("path", ["import", "import-batch"])
def test_legacy_import_endpoints_are_absent(monkeypatch, tmp_path: Path, path: str) -> None:
    client, manager = _client(monkeypatch, tmp_path)

    with client:
        imported = client.post(
            f"/api/v1/projects/demo/short-drama/{path}",
            json={"episode_id": 1, "markdown": MARKDOWN},
        )
        source = client.get("/api/v1/projects/demo/episodes/1/short-drama-source")

    assert imported.status_code == 404
    assert source.status_code == 200
    assert source.json()["exists"] is False
    assert not (manager.get_project_path("demo") / "scripts/episode_1.json").exists()


def test_reimport_requires_confirmation_when_media_exists(monkeypatch, tmp_path: Path) -> None:
    client, manager = _client(monkeypatch, tmp_path, assets_ready=True)
    monkeypatch.setattr(TextGenerator, "create", FakeTextGenerator(_plan_payload(MARKDOWN)).create)

    with client:
        revision = _save_source(client, manager)
        assert (
            client.post(
                "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
                json={"source_revision": revision},
            ).status_code
            == 200
        )
        script = manager.load_script("demo", "episode_1.json")
        script["scenes"][0]["generated_assets"]["storyboard_image"] = "storyboards/scene_E1S1.png"
        manager.save_script("demo", script, "episode_1.json")
        refused = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": revision, "confirm_artifacts": False},
        )
        confirmed = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": revision, "confirm_artifacts": True},
        )

    assert refused.status_code == 409
    assert confirmed.status_code == 200


def _shot_prompt(header: str = "【镜头1｜中景】平视机位，缓推。") -> str:
    return "\n".join(
        [
            header,
            "林夏攥紧门把手，侧耳贴门屏息，指节因用力而发白；",
            "旧公寓@[旧公寓]的水泥楼道里声控灯忽明忽暗，铁门的锈迹映出她绷紧的侧脸，一缕冷光扫过门缝。",
        ]
    )


def _plan_payload(markdown: str, *, header: str = "【镜头1｜中景】平视机位，缓推。", group_size: int = 10) -> str:
    """按源稿 beats 生成一份结构合规的规划回复。"""
    beats = beat_catalog(parse_short_drama_episode(markdown))
    groups = [
        list(range(start, min(start + group_size, len(beats) + 1))) for start in range(1, len(beats) + 1, group_size)
    ]
    return json.dumps(
        {
            "scenes": [
                {
                    "beat_ids": group,
                    "duration_seconds": 12,
                    "segment_break": False,
                    "scene_description": "林夏攥紧门把手，隔着铁门屏息听门外动静。",
                    "image_prompt": _shot_prompt(header),
                    "utterances": [],
                    "characters_in_scene": ["林夏"],
                    "scenes": ["旧公寓"],
                    "props": ["铁门"],
                }
                for group in groups
            ]
        },
        ensure_ascii=False,
    )


def _save_source(client: TestClient, manager: ProjectManager, markdown: str = MARKDOWN) -> str:
    ingest_short_drama_artifacts(manager, "demo", factory_package(markdown))
    return client.get("/api/v1/projects/demo/episodes/1/short-drama-source").json()["revision"]


def test_smart_reimport_writes_ai_shot_prompts(monkeypatch, tmp_path: Path) -> None:
    """智能切分走文本模型：每幕落单段 image_prompt，video_prompt 仍留空。"""
    client, manager = _client(monkeypatch, tmp_path, assets_ready=True)
    model = FakeTextGenerator(_plan_payload(MARKDOWN))
    monkeypatch.setattr(TextGenerator, "create", model.create)

    with client:
        revision = _save_source(client, manager)
        imported = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": revision, "confirm_artifacts": False},
        )

    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert body["ai_authoring"] is True
    assert "planning_fallback" not in body
    assert body["scenes"] >= 2
    # 整段回复都要解析成 JSON，必须要求供应商不截断。
    assert model.require_complete == [True]
    script = manager.load_script("demo", "episode_1.json")
    for scene in script["scenes"]:
        assert scene["image_prompt"].startswith("【镜头1｜中景】平视机位，缓推。")
        assert scene["video_prompt"] is None
        assert scene["pending_authoring"] is True


def test_smart_reimport_never_falls_back_after_exhausted_retries(monkeypatch, tmp_path: Path) -> None:
    """无机械回落：校验不合格就带原因持续返修，穷尽后报错，绝不交付骨架提示词。"""
    client, manager = _client(monkeypatch, tmp_path, assets_ready=True)
    broken = _plan_payload(MARKDOWN, header="【镜头1】景别，机位，运镜。")
    # 返修轮数由 _PLAN_ATTEMPTS 决定，这里给足同样失败的回复，逼它走到报错。
    model = FakeTextGenerator(*([broken] * 5))
    monkeypatch.setattr(TextGenerator, "create", model.create)

    with client:
        revision = _save_source(client, manager)
        imported = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": revision, "confirm_artifacts": False},
        )

    assert imported.status_code == 422, imported.text
    assert "缺景别" in imported.json()["detail"]
    assert len(model.requests) == 5
    assert "上一轮输出未通过校验" in model.requests[1].prompt
    # 报错即回滚：正式脚本里没有半成品，也没有「回落骨架」的 mode 留痕。
    assert not (manager.get_project_path("demo") / "scripts" / "episode_1.json").exists()


@pytest.mark.parametrize(
    ("header", "problem"),
    [
        ("【镜头1｜中景】缓推。", "缺机位"),
        ("【镜头1｜中景】平视机位。", "缺运镜"),
    ],
)
def test_smart_reimport_requires_both_camera_position_and_motion(monkeypatch, tmp_path, header, problem):
    client, manager = _client(monkeypatch, tmp_path, assets_ready=True)
    model = FakeTextGenerator(*([_plan_payload(MARKDOWN, header=header)] * 5))
    monkeypatch.setattr(TextGenerator, "create", model.create)
    with client:
        revision = _save_source(client, manager)
        response = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": revision},
        )
    assert response.status_code == 422
    assert problem in response.json()["detail"]
    assert len(model.requests) == 5
    assert not (manager.get_project_path("demo") / "scripts/episode_1.json").exists()


def test_reimport_refuses_to_overwrite_visual_edits_made_during_model_call(monkeypatch, tmp_path):
    client, manager = _client(monkeypatch, tmp_path, assets_ready=True)

    class EditingModel(FakeTextGenerator):
        edit = False

        async def generate(self, request, project_name=None, *, require_complete=False):
            if self.edit:
                with manager.locked_script("demo", "episode_1.json") as current:
                    current["scenes"][0]["scene_description"] = "用户刚保存的分镜描述"
            return await super().generate(request, project_name, require_complete=require_complete)

    model = EditingModel(_plan_payload(MARKDOWN), _plan_payload(MARKDOWN))
    monkeypatch.setattr(TextGenerator, "create", model.create)
    with client:
        revision = _save_source(client, manager)
        assert (
            client.post(
                "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
                json={"source_revision": revision},
            ).status_code
            == 200
        )
        before = (manager.get_project_path("demo") / "source/short_drama/episodes/episode_01.md").read_bytes()
        model.edit = True
        response = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": revision, "force_regenerate": True, "confirm_artifacts": True},
        )
    assert response.status_code == 409
    assert manager.load_script("demo", "episode_1.json")["scenes"][0]["scene_description"] == "用户刚保存的分镜描述"
    assert (manager.get_project_path("demo") / "source/short_drama/episodes/episode_01.md").read_bytes() == before


def test_smart_reimport_keeps_refining_until_plan_passes(monkeypatch, tmp_path: Path) -> None:
    """校验不通过就带原因返修、让 AI 继续优化，而非放宽闸门；多轮后通过即落盘 AI 提示词。"""
    client, manager = _client(monkeypatch, tmp_path, assets_ready=True)
    broken = _plan_payload(MARKDOWN, header="【镜头1】景别，机位，运镜。")
    good = _plan_payload(MARKDOWN, header="【镜头1｜中景】平视机位，缓推。")
    model = FakeTextGenerator(broken, good)
    monkeypatch.setattr(TextGenerator, "create", model.create)

    with client:
        revision = _save_source(client, manager)
        imported = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": revision, "confirm_artifacts": False},
        )

    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert body["ai_authoring"] is True
    assert "planning_fallback" not in body
    # 首轮被拒、第二轮优化通过：多轮优化机制确实生效，不是一次失败就回落或放宽。
    assert len(model.requests) == 2
    assert "上一轮输出未通过校验" in model.requests[1].prompt
    script = manager.load_script("demo", "episode_1.json")
    assert script["scenes"][0]["image_prompt"].startswith("【镜头1｜中景】平视机位，缓推。")


def test_smart_reimport_reuses_cached_plan(monkeypatch, tmp_path: Path) -> None:
    """同源稿同强度再切一次：直接复用缓存，不再调模型。"""
    client, manager = _client(monkeypatch, tmp_path, assets_ready=True)
    model = FakeTextGenerator(_plan_payload(MARKDOWN))
    monkeypatch.setattr(TextGenerator, "create", model.create)

    with client:
        revision = _save_source(client, manager)
        first = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": revision, "confirm_artifacts": False},
        ).json()
        second = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": revision, "confirm_artifacts": False},
        ).json()

    assert (first["planning_cached"], second["planning_cached"]) == (False, True)
    assert len(model.requests) == 1
    from server.services.short_drama_workflow import _PLAN_CACHE_VERSION

    assert _PLAN_CACHE_VERSION == "v5"
    assert list((manager.get_project_path("demo") / "drafts").glob("short_drama_plan_v5_*.json"))


def test_same_source_preserves_manual_prompt_and_media(monkeypatch, tmp_path: Path) -> None:
    client, manager = _client(monkeypatch, tmp_path, assets_ready=True)
    model = FakeTextGenerator(_plan_payload(MARKDOWN))
    monkeypatch.setattr(TextGenerator, "create", model.create)
    with client:
        revision = _save_source(client, manager)
        client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": revision},
        )
        current = manager.load_script("demo", "episode_1.json")
        current["scenes"][0]["image_prompt"] = _shot_prompt().replace("缓推", "缓推，镜头缓慢贴近门缝")
        current["scenes"][0]["generated_assets"]["video_clip"] = "videos/first.mp4"
        manager.save_script("demo", current, "episode_1.json")
        response = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": revision, "confirm_artifacts": True},
        )
    assert response.status_code == 200
    assert response.json()["planning_cached"] is True
    saved = manager.load_script("demo", "episode_1.json")
    assert saved["scenes"] == current["scenes"]
    assert len(model.requests) == 1


def test_same_source_refuses_to_reuse_dirty_script_without_force(monkeypatch, tmp_path: Path) -> None:
    """force=False 不得复用含台词行的旧脚本：明确要求整集重生成。"""
    client, manager = _client(monkeypatch, tmp_path, assets_ready=True)
    model = FakeTextGenerator(_plan_payload(MARKDOWN))
    monkeypatch.setattr(TextGenerator, "create", model.create)
    with client:
        revision = _save_source(client, manager)
        client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": revision},
        )
        current = manager.load_script("demo", "episode_1.json")
        current["scenes"][0]["image_prompt"] = f"{_shot_prompt()}\n台词：林夏：“谁在外面？”"
        manager.save_script("demo", current, "episode_1.json")
        response = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": revision, "confirm_artifacts": False},
        )

    assert response.status_code == 422
    assert "force_regenerate" in response.json()["detail"]
    assert len(model.requests) == 1


@pytest.mark.parametrize(
    "broken_prompt",
    [
        _shot_prompt() + "\n\n" + _shot_prompt().replace("【镜头1｜中景】", "【镜头2｜中景】"),
        _shot_prompt().replace("旧公寓@[旧公寓]", "风格锚@[短剧/气氛]"),
        _shot_prompt().replace("旧公寓@[旧公寓]", "路人@[路人]"),
    ],
)
def test_bad_shot_content_is_repaired_before_any_visual_state_is_written(
    monkeypatch,
    tmp_path: Path,
    broken_prompt: str,
) -> None:
    client, manager = _client(monkeypatch, tmp_path, assets_ready=True)
    broken = json.loads(_plan_payload(MARKDOWN))
    for scene in broken["scenes"]:
        scene["image_prompt"] = broken_prompt
    model = FakeTextGenerator(json.dumps(broken, ensure_ascii=False), _plan_payload(MARKDOWN))
    monkeypatch.setattr(TextGenerator, "create", model.create)
    with client:
        revision = _save_source(client, manager)
        response = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": revision},
        )
    assert response.status_code == 200
    assert len(model.requests) == 2
    assert "上一轮输出未通过校验" in model.requests[1].prompt
    assert manager.load_script("demo", "episode_1.json")["scenes"][0]["image_prompt"] == _shot_prompt()


def _rewrite_payload(scene_ids: list[str]) -> str:
    """按分镜重写的合规回复：scene_id 一一对应、时长落窗口、镜头块过丰满度校验。"""
    return json.dumps(
        {
            "scenes": [
                {
                    "scene_id": scene_id,
                    "duration_seconds": 13,
                    "scene_description": "林夏攥紧门把手，侧耳贴门，屏息听门外动静。",
                    "image_prompt": _shot_prompt(header="【镜头1｜特写】仰拍机位，缓拉。"),
                }
                for scene_id in scene_ids
            ]
        },
        ensure_ascii=False,
    )


def test_reimport_regenerates_selected_scenes_only(monkeypatch, tmp_path: Path) -> None:
    """按分镜重写：只重写所选分镜的描述与提示词，其余分镜原样保留。"""
    client, manager = _client(monkeypatch, tmp_path, assets_ready=True)
    model = FakeTextGenerator(_plan_payload(MARKDOWN), _rewrite_payload(["E1S2"]))
    monkeypatch.setattr(TextGenerator, "create", model.create)

    with client:
        revision = _save_source(client, manager)
        first = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": revision, "confirm_artifacts": False},
        ).json()
        second = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={
                "source_revision": revision,
                "confirm_artifacts": False,
                "regenerate_scene_ids": ["E1S2"],
            },
        )

    assert second.status_code == 200, second.text
    body = second.json()
    assert body["ai_authoring"] is True
    assert body["regenerated_scenes"] == 1
    assert body["scenes"] == first["scenes"]
    script = manager.load_script("demo", "episode_1.json")
    assert len(script["scenes"]) == first["scenes"]
    rewritten = next(scene for scene in script["scenes"] if scene["scene_id"] == "E1S2")
    untouched = next(scene for scene in script["scenes"] if scene["scene_id"] == "E1S1")
    assert rewritten["image_prompt"].startswith("【镜头1｜特写】仰拍机位，缓拉。")
    assert rewritten["duration_seconds"] == 13
    assert untouched["image_prompt"].startswith("【镜头1｜中景】平视机位，缓推。")
    assert untouched["duration_seconds"] == 12
    # 未选中的分镜连 source_text 都不动：已生成的媒体与手动编辑因此不会丢。
    assert bool(rewritten["source_text"])
    assert bool(untouched["source_text"])
    options = script["short_drama_meta"]["ai_storyboard_options"]
    assert options["mode"] == "smart"
    assert options["regenerated_scene_ids"] == ["E1S2"]
    # 重写请求里只带了所选分镜的 beats 与旧提示词。
    assert "待重写的分镜" in model.requests[1].prompt
    assert "E1S2" in model.requests[1].prompt
    assert "E1S1" not in model.requests[1].prompt


def test_reimport_regenerate_rejects_unknown_scene_ids(monkeypatch, tmp_path: Path) -> None:
    """要重生成的分镜不存在时报 422，不静默扩大范围。"""
    client, manager = _client(monkeypatch, tmp_path, assets_ready=True)
    monkeypatch.setattr(TextGenerator, "create", FakeTextGenerator(_plan_payload(MARKDOWN)).create)

    with client:
        revision = _save_source(client, manager)
        client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": revision, "confirm_artifacts": False},
        )
        refused = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={
                "source_revision": revision,
                "confirm_artifacts": False,
                "regenerate_scene_ids": ["E1S99"],
            },
        )

    assert refused.status_code == 422
    # detail 会把分镜 ID 渲染成「未命名集 · S99」，所以只断言集内 ID。
    assert "S99" in refused.json()["detail"]


def test_get_source_reports_assets_missing_description(monkeypatch, tmp_path: Path) -> None:
    """GET 源稿回带缺描述资产清单：分镜入口据此置灰并提示先补齐。"""
    client, manager = _client(monkeypatch, tmp_path)
    manager.update_project(
        "demo",
        lambda project: project.setdefault("characters", {}).update({"林夏": {"description": ""}}),
    )

    with client:
        _save_source(client, manager)
        body = client.get("/api/v1/projects/demo/episodes/1/short-drama-source").json()

    assert body["exists"] is True
    missing = {(item["type"], item["name"]) for item in body["assets_missing_description"]}
    assert ("character", "林夏") in missing
    assert ("character", "周明") in missing
