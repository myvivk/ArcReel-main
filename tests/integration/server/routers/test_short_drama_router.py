from __future__ import annotations

import base64
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lib.project.project_manager import ProjectManager
from server.auth import CurrentUserInfo, get_current_user
from server.error_handlers import register_error_handlers
from server.routers import short_drama
from tests.auth_deps import AUTH_DEPENDENCIES


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
            json={"source_revision": saved["revision"], "confirm_artifacts": False},
        )

    assert imported.status_code == 200
    assert imported.json()["scenes"] >= 1
    script = manager.load_script("demo", "episode_1.json")
    assert script["short_drama_meta"]["source_path"] == "source/short_drama/episodes/episode_01.md"
    assert script["short_drama_meta"]["source_revision"] == saved["revision"]
    assert all(
        12 <= scene["duration_seconds"] <= 15 for scene in script["scenes"]
    ), [scene["duration_seconds"] for scene in script["scenes"]]
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
    markdown = (
        MARKDOWN.replace("（女主｜齐肩短发，米色风衣，右腕有旧疤）", "（女主）")
        .replace("（前夫｜寸头，深灰西装，领口松开）", "（前夫）")
    )

    with client:
        saved = client.put(
            "/api/v1/projects/demo/episodes/1/short-drama-source",
            json={"markdown": markdown, "expected_revision": None},
        ).json()
        imported = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": saved["revision"], "confirm_artifacts": False},
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
            json={"source_revision": imported["source_revision"], "confirm_artifacts": False},
        )
        confirmed = client.post(
            "/api/v1/projects/demo/episodes/1/short-drama-source/reimport",
            json={"source_revision": imported["source_revision"], "confirm_artifacts": True},
        )

    assert refused.status_code == 409
    assert confirmed.status_code == 200
