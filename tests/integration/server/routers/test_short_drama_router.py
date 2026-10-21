from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lib.backends.text_generator import TextGenerator
from lib.project.project_manager import ProjectManager
from lib.script.short_drama_import import beat_catalog, parse_short_drama_episode
from server.auth import CurrentUserInfo, get_current_user
from server.error_handlers import register_error_handlers
from server.routers import short_drama
from tests.auth_deps import AUTH_DEPENDENCIES
from tests.fakes import FakeTextGenerator


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


def test_artifact_package_import_preserves_bytes_and_does_not_project(monkeypatch, tmp_path: Path) -> None:
    client, manager = _client(monkeypatch, tmp_path)
    original = _episode_markdown("\r\n").encode("utf-8")
    payload = {
        "artifacts": [
            {
                "path": "episodes/episode_01.md",
                "content_base64": base64.b64encode(original).decode("ascii"),
            },
            {
                "path": "reports/episode_01.machine.md",
                "content_base64": base64.b64encode(b"FAIL\r\n").decode("ascii"),
            },
        ]
    }
    with client:
        response = client.post("/api/v1/projects/demo/short-drama/import-artifacts", json=payload)
        listed = client.get("/api/v1/projects/demo/short-drama/artifacts")

    assert response.status_code == 200
    assert listed.status_code == 200
    project_path = manager.get_project_path("demo")
    assert (project_path / "source/short_drama/episodes/episode_01.md").read_bytes() == original
    assert not (project_path / "characters.json").exists()
    assert {item["path"] for item in listed.json()["artifacts"]} == {
        "episodes/episode_01.md",
        "reports/episode_01.machine.md",
    }
    assert not (project_path / "scripts/episode_1.json").exists()


def test_ingested_episode_artifact_visible_as_short_drama_source(monkeypatch, tmp_path: Path) -> None:
    """复现：工厂工件包导入后，分集页经 getShortDramaSource 应能读到本集正文。"""
    client, _manager = _client(monkeypatch, tmp_path)
    original = MARKDOWN.encode("utf-8")
    payload = {
        "artifacts": [
            {
                "path": "episodes/episode_01.md",
                "content_base64": base64.b64encode(original).decode("ascii"),
            }
        ]
    }
    with client:
        import_resp = client.post("/api/v1/projects/demo/short-drama/import-artifacts", json=payload)
        source_resp = client.get("/api/v1/projects/demo/episodes/1/short-drama-source")

    assert import_resp.status_code == 200
    assert source_resp.status_code == 200
    body = source_resp.json()
    assert body["exists"] is True, body
    assert "雨夜重逢" in body["markdown"]


def test_ingest_registers_episode_as_own_source(monkeypatch, tmp_path: Path) -> None:
    """复现断点：导入后每集工厂 Markdown 须登记为该集 OWN 源文本，分集页本集正文才有内容、下游才建得上。"""
    client, manager = _client(monkeypatch, tmp_path)
    original = MARKDOWN.encode("utf-8")
    payload = {
        "artifacts": [
            {
                "path": "episodes/episode_01.md",
                "content_base64": base64.b64encode(original).decode("ascii"),
            }
        ]
    }
    with client:
        resp = client.post("/api/v1/projects/demo/short-drama/import-artifacts", json=payload)
    assert resp.status_code == 200
    project = manager.load_project("demo")
    entry = next(e for e in project["episodes"] if e.get("episode") == 1)
    assert entry.get("source_origin") == "own", entry
    source_file = manager.get_project_path("demo") / "source" / "episode_1.txt"
    assert source_file.read_text(encoding="utf-8") == original.decode("utf-8")


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
    assert legacy.json()["path"] == "source/short_drama/episodes/episode_01.md"
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
            json={"source_revision": saved["revision"], "confirm_artifacts": False, "fast_split": True},
        )

    assert imported.status_code == 200
    assert imported.json()["scenes"] >= 1
    script = manager.load_script("demo", "episode_1.json")
    assert script["short_drama_meta"]["source_path"] == "source/short_drama/episodes/episode_01.md"
    assert script["short_drama_meta"]["source_revision"] == saved["revision"]
    # 快速切分也留痕：分镜页据此提示「提示词为骨架，可用 AI 重新分镜重写」。
    assert script["short_drama_meta"]["ai_storyboard_options"]["mode"] == "fast"
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


def test_reimport_reports_assets_without_description(monkeypatch, tmp_path: Path) -> None:
    client, manager = _client(monkeypatch, tmp_path)
    manager.update_project(
        "demo",
        lambda project: project.setdefault("characters", {}).update({"周明": {"description": ""}}),
    )
    markdown = MARKDOWN.replace("（女主｜齐肩短发，米色风衣，右腕有旧疤）", "（女主）").replace(
        "（前夫｜寸头，深灰西装，领口松开）", "（前夫）"
    )

    with client:
        saved = client.put(
            "/api/v1/projects/demo/episodes/1/short-drama-source",
            json={"markdown": markdown, "expected_revision": None},
        ).json()
        imported = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": saved["revision"], "confirm_artifacts": False, "fast_split": True},
        ).json()

    missing = {(item["name"], item["reason"]) for item in imported["assets_without_description"]}
    assert ("林夏", "source_missing") in missing
    assert ("周明", "existing_blank") in missing
    assert all(item["type"] == "character" for item in imported["assets_without_description"])


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
            json={"source_revision": imported["source_revision"], "confirm_artifacts": False, "fast_split": True},
        )
        confirmed = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": imported["source_revision"], "confirm_artifacts": True, "fast_split": True},
        )

    assert refused.status_code == 409
    assert confirmed.status_code == 200


def _shot_prompt(header: str = "【镜头1】中景，平视机位，缓推。") -> str:
    return "\n".join(
        [
            header,
            "画面主体：林夏@[林夏/抓住门把手，压声屏息]。",
            "环境道具：旧公寓@[旧公寓/水泥楼道声控灯忽明忽暗]。",
            "视觉参考：风格锚@[短剧/雨夜逼仄质感]",
        ]
    )


def _plan_payload(markdown: str, *, header: str = "【镜头1】中景，平视机位，缓推。", group_size: int = 10) -> str:
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


def _save_source(client: TestClient) -> str:
    return client.put(
        "/api/v1/projects/demo/episodes/1/short-drama-source",
        json={"markdown": MARKDOWN, "expected_revision": None},
    ).json()["revision"]


def test_smart_reimport_writes_ai_shot_prompts(monkeypatch, tmp_path: Path) -> None:
    """智能切分走文本模型：每幕落单段 image_prompt，video_prompt 仍留空。"""
    client, manager = _client(monkeypatch, tmp_path)
    model = FakeTextGenerator(_plan_payload(MARKDOWN))
    monkeypatch.setattr(TextGenerator, "create", model.create)

    with client:
        revision = _save_source(client)
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
        assert scene["image_prompt"].startswith("【镜头1】中景，平视机位，缓推。")
        assert scene["video_prompt"] is None
        assert scene["pending_authoring"] is True


def test_smart_reimport_retries_then_falls_back_to_mechanical_split(monkeypatch, tmp_path: Path) -> None:
    """镜头首行没有真实景别/运镜：带原因反复返修，所有轮次仍不合格才回落机械切分。"""
    client, manager = _client(monkeypatch, tmp_path)
    broken = _plan_payload(MARKDOWN, header="【镜头1】景别，机位，运镜。")
    # 返修轮数由 _PLAN_ATTEMPTS 决定，这里给足同样失败的回复，逼它走到回落。
    model = FakeTextGenerator(*([broken] * 5))
    monkeypatch.setattr(TextGenerator, "create", model.create)

    with client:
        revision = _save_source(client)
        imported = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": revision, "confirm_artifacts": False},
        )

    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert body["ai_authoring"] is False
    assert "镜头首行缺景别或运镜" in body["planning_fallback"]
    assert len(model.requests) == 5
    assert "上一轮输出未通过校验" in model.requests[1].prompt
    script = manager.load_script("demo", "episode_1.json")
    assert script["scenes"], "回落机械切分后仍须产出分镜"
    # 回落不留白：机械路径给骨架提示词，且回落原因随剧本留痕，用户事后查得到。
    options = script["short_drama_meta"]["ai_storyboard_options"]
    assert options["mode"] == "smart_fallback"
    assert "镜头首行缺景别或运镜" in options["fallback"]
    for scene in script["scenes"]:
        assert scene["image_prompt"].startswith("【镜头1】中景，平视机位，固定。")
        assert "视觉参考：" in scene["image_prompt"]
        assert scene["pending_authoring"] is True


def test_smart_reimport_keeps_refining_until_plan_passes(monkeypatch, tmp_path: Path) -> None:
    """校验不通过就带原因返修、让 AI 继续优化，而非放宽闸门；多轮后通过即落盘 AI 提示词。"""
    client, manager = _client(monkeypatch, tmp_path)
    broken = _plan_payload(MARKDOWN, header="【镜头1】景别，机位，运镜。")
    good = _plan_payload(MARKDOWN, header="【镜头1】中景，平视机位，缓推。")
    model = FakeTextGenerator(broken, good)
    monkeypatch.setattr(TextGenerator, "create", model.create)

    with client:
        revision = _save_source(client)
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
    assert script["scenes"][0]["image_prompt"].startswith("【镜头1】中景，平视机位，缓推。")


def test_smart_reimport_reuses_cached_plan(monkeypatch, tmp_path: Path) -> None:
    """同源稿同强度再切一次：直接复用缓存，不再调模型。"""
    client, _manager = _client(monkeypatch, tmp_path)
    model = FakeTextGenerator(_plan_payload(MARKDOWN))
    monkeypatch.setattr(TextGenerator, "create", model.create)

    with client:
        revision = _save_source(client)
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
