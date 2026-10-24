"""单分镜参考素材宫格的 HTTP 面：保存规格与逐格文本、入队、布局校验。

回归点：``PUT /shot-grids`` 的请求体必须带 ``layout`` 派生属性（与 ``POST /generate``
共用同一族规格模型），否则处理函数在 ``_assert_layout`` 处抛 ``AttributeError`` → 500；
``POST /generate`` 入队时引用的是查询参数 ``script_file``，不能残留已删除的 ``req``。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

from lib.artifacts.artifact_manifest import (
    ArtifactKey,
    ArtifactManifest,
    ArtifactManifestEntry,
    ProjectArtifactManifestAdapter,
)
from lib.project.project_manager import ProjectManager
from lib.script.grid.keyframe_parser import scene_revision_of
from lib.script.grid.shot_grid_manager import ShotGridManager
from lib.script.grid.shot_models import ShotGrid
from server.error_handlers import register_error_handlers
from server.routers import shot_grids
from tests.auth_deps import AUTH_DEPENDENCIES, override_auth

PROJECT = "proj-e75f39a3"
BASE = f"/api/v1/projects/{PROJECT}/shot-grids"
SCRIPT = "episode_1.json"
SHOT = "E1S01"

SCENE = (
    "【镜头1｜全景】传统中式厅堂的室内夜景，一只白瓷酒盏刚刚滚落。\n"
    "【镜头2｜近景】碎片散落在青砖的砖缝之间。\n"
    "【镜头3｜特写】藕色织锦襦裙的衣襟近特写。\n"
    "【镜头4｜特写】指尖轻触一片尖锐瓷片。"
)

PROMPT = (
    "第1格（全景）：传统中式厅堂的室内夜景，一只白瓷酒盏刚刚滚落。\n"
    "第2格（近景）：碎片散落在青砖的砖缝之间。\n"
    "第3格（特写）：藕色织锦襦裙的衣襟近特写。\n"
    "第4格（特写）：指尖轻触一片尖锐瓷片。"
)

CELL_TEXTS = [
    "传统中式厅堂的室内夜景，一只白瓷酒盏刚刚滚落。",
    "碎片散落在青砖的砖缝之间。",
    "藕色织锦襦裙的衣襟近特写。",
    "指尖轻触一片尖锐瓷片。",
]


class _RecordingQueue:
    """只记录入队参数并立刻应答的队列替身：不碰数据库、不真正跑任务。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def enqueue_task(self, **kwargs: object) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"task_id": "task-1", "status": "queued"}


@pytest.fixture
def grid_projects(tmp_path: Path) -> ProjectManager:
    manager = ProjectManager(tmp_path / "projects")
    manager.create_project(PROJECT)
    manager.create_project_metadata(PROJECT, "宫格测试", content_mode="drama")
    scripts = manager.get_project_path(PROJECT) / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    script = {
        "episode": 1,
        "title": "第一集",
        "content_mode": "drama",
        "scenes": [
            {
                "scene_id": SHOT,
                "image_prompt": {"opening": "", "scene": SCENE},
                "video_prompt": {"action": ""},
                "duration_seconds": 8,
                "generated_assets": {},
            }
        ],
    }
    (scripts / SCRIPT).write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    manager.add_episode(PROJECT, 1, "第一集", f"scripts/{SCRIPT}")
    return manager


@pytest.fixture
def grid_queue() -> _RecordingQueue:
    return _RecordingQueue()


@pytest.fixture
async def grid_client(
    grid_projects: ProjectManager, grid_queue: _RecordingQueue, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(shot_grids, "get_project_manager", lambda: grid_projects)
    monkeypatch.setattr(shot_grids, "get_generation_queue", lambda: grid_queue)
    app = FastAPI()
    register_error_handlers(app)
    override_auth(app)
    app.include_router(shot_grids.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http


def _payload(**overrides: object) -> dict:
    payload: dict = {
        "script_file": SCRIPT,
        "episode": 1,
        "shot_id": SHOT,
        "rows": 2,
        "cols": 2,
        "prompt": PROMPT,
    }
    payload.update(overrides)
    return payload


def _mark_completed(manager: ShotGridManager, grid_id: str) -> str:
    """把记录置为「宫格图已出、尚未确认」，composite_path 指到一张不存在的图也够用。"""

    composite = f"shot_grids/{grid_id}.png"

    def mutate(grid: ShotGrid) -> None:
        grid.status = "completed"
        grid.composite_path = composite

    manager.update_formal(grid_id, mutate)
    return composite


async def test_put_saves_layout_prompt_and_cells(grid_client: httpx.AsyncClient) -> None:
    response = await grid_client.put(BASE, json=_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["prompt"] == PROMPT
    assert body["status"] == "draft"
    assert [cell["prompt"] for cell in body["cells"]] == CELL_TEXTS
    assert [cell["continuity_notes"] for cell in body["cells"]] == ["全景", "近景", "特写", "特写"]

    # 原样再保存一次（prompt 不传）：幂等，不把已存的逐格文本或状态清掉。
    again = await grid_client.put(BASE, json=_payload(prompt=None))
    assert again.status_code == 200
    assert again.json()["prompt"] == PROMPT
    assert again.json()["status"] == "draft"


async def test_identical_save_keeps_the_generated_grid(
    grid_client: httpx.AsyncClient, grid_projects: ProjectManager
) -> None:
    saved = (await grid_client.put(BASE, json=_payload())).json()
    composite = _mark_completed(ShotGridManager(grid_projects.get_project_path(PROJECT)), saved["id"])

    again = await grid_client.put(BASE, json=_payload())

    assert again.status_code == 200
    body = again.json()
    assert body["status"] == "completed"
    assert body["composite_path"] == composite
    assert body["prompt"] == PROMPT


async def test_same_layout_save_keeps_generated_image_and_actual_cell_evidence(
    grid_client: httpx.AsyncClient, grid_projects: ProjectManager
) -> None:
    saved = (await grid_client.put(BASE, json=_payload())).json()
    composite = _mark_completed(ShotGridManager(grid_projects.get_project_path(PROJECT)), saved["id"])
    edited = PROMPT.replace("指尖轻触一片尖锐瓷片。", "手掌离开瓷片。")

    response = await grid_client.put(BASE, json=_payload(prompt=edited))

    assert response.status_code == 200
    body = response.json()
    assert body["prompt"] == edited
    assert body["status"] == "completed"
    assert body["composite_path"] == composite
    # 规格仍是已提交的那一档：草稿不碰切格口径
    assert (body["rows"], body["cols"]) == (2, 2)
    # 留证仍是上一次出图的那一份
    assert [cell["prompt"] for cell in body["cells"]] == CELL_TEXTS


@pytest.mark.parametrize("prompt", ["一格", PROMPT + "\n第5格：多余画面"])
async def test_manual_cell_count_mismatch_is_rejected_without_record(
    grid_client: httpx.AsyncClient, grid_projects: ProjectManager, prompt: str
) -> None:
    response = await grid_client.put(BASE, json=_payload(prompt=prompt))
    assert response.status_code == 422
    assert not ShotGridManager(grid_projects.get_project_path(PROJECT)).list_all()


async def test_empty_prompt_is_saved_and_generated_from_scene(
    grid_client: httpx.AsyncClient, grid_projects: ProjectManager
) -> None:
    saved = await grid_client.put(BASE, json=_payload(prompt=""))
    assert saved.status_code == 200
    response = await grid_client.post(f"{BASE}/generate", params={"script_file": SCRIPT, "shot_id": SHOT})
    assert response.status_code == 200
    grid = ShotGridManager(grid_projects.get_project_path(PROJECT)).get(saved.json()["id"])
    assert grid is not None
    assert [cell.prompt for cell in grid.cells] == CELL_TEXTS


async def test_put_rejects_unsupported_layout(grid_client: httpx.AsyncClient) -> None:
    response = await grid_client.put(BASE, json=_payload(rows=3, cols=2))

    assert response.status_code == 400
    assert "2×2" in response.json()["detail"]


async def test_generate_enqueues_the_saved_grid(grid_client: httpx.AsyncClient, grid_queue: _RecordingQueue) -> None:
    saved = await grid_client.put(BASE, json=_payload())

    response = await grid_client.post(f"{BASE}/generate", params={"script_file": SCRIPT, "shot_id": SHOT})

    assert response.status_code == 200
    assert response.json() == {"task_id": "task-1", "status": "queued"}
    assert grid_queue.calls == [
        {
            "project_name": PROJECT,
            "task_type": "shot_grid",
            "media_type": "image",
            "resource_id": saved.json()["id"],
            "payload": {"shot_grid_id": saved.json()["id"], "script_file": SCRIPT},
            "script_file": SCRIPT,
            "source": "webui",
            "user_id": "default",
        }
    ]
    refreshed = await grid_client.get(BASE, params={"script_file": SCRIPT, "shot_id": SHOT})
    assert refreshed.json()["status"] == "generating"


async def test_cancelled_queued_task_releases_the_grid_for_retry(
    grid_client: httpx.AsyncClient,
    grid_projects: ProjectManager,
    grid_queue: _RecordingQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = (await grid_client.put(BASE, json=_payload())).json()
    await grid_client.post(f"{BASE}/generate", params={"script_file": SCRIPT, "shot_id": SHOT})
    monkeypatch.setattr(
        grid_queue,
        "get_task",
        AsyncMock(
            return_value={
                "task_id": "task-1",
                "task_type": "shot_grid",
                "project_name": PROJECT,
                "resource_id": saved["id"],
                "status": "cancelled",
            }
        ),
        raising=False,
    )
    response = await grid_client.get(f"{BASE}/tasks/task-1")
    assert response.status_code == 200
    record = ShotGridManager(grid_projects.get_project_path(PROJECT)).get(saved["id"])
    assert record is not None
    assert record.status == "failed"
    retry = await grid_client.put(BASE, json=_payload(rows=3, cols=3, prompt=""))
    assert retry.status_code == 200
    assert retry.json()["status"] == "draft"


def _write_composite(project_path: Path, grid_id: str, width: int, height: int) -> str:
    """把一张真实尺寸的宫格联合图落到记录所指路径，供 confirm 切分。"""

    rel = f"shot_grids/{grid_id}.png"
    path = project_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), (12, 24, 36)).save(path, format="PNG")
    return rel


@pytest.mark.parametrize(
    ("aspect", "size"),
    [("16:9", (1280, 720)), ("9:16", (720, 1280))],
)
async def test_confirm_splits_cells_at_the_frozen_orientation(
    grid_client: httpx.AsyncClient,
    grid_projects: ProjectManager,
    aspect: str,
    size: tuple[int, int],
) -> None:
    """确认切分按冻结的画布比例落格：16:9 与 9:16 都要切出来，单格比例与之一致。

    单格比例对齐后才好当分镜图 / 视频首尾帧用；逐格描述是本次出图的留证，
    确认切分不改变它。
    """

    saved = (await grid_client.put(BASE, json=_payload())).json()
    project_path = grid_projects.get_project_path(PROJECT)
    composite = _write_composite(project_path, saved["id"], *size)
    manager = ShotGridManager(project_path)

    def set_ready(grid: ShotGrid) -> None:
        grid.status = "completed"
        grid.composite_path = composite
        grid.video_aspect_ratio = aspect

    manager.update_formal(saved["id"], set_ready)

    response = await grid_client.post(f"{BASE}/{saved['id']}/confirm")

    assert response.status_code == 200
    cells = response.json()["grid"]["cells"]
    assert [cell["image_path"] for cell in cells] == [f"storyboards/{saved['id']}/{index}.png" for index in range(4)]
    # 逐格描述（本次出图的逐格留证）在确认切分后仍在
    assert [cell["prompt"] for cell in cells] == CELL_TEXTS

    expected = 16 / 9 if aspect == "16:9" else 9 / 16
    for cell in cells:
        with Image.open(project_path / cell["image_path"]) as image:
            width, height = image.size
        assert abs(width / height - expected) < 0.02
    body = response.json()["grid"]
    assert body["cell_paths"] == [cell["image_path"] for cell in cells]
    binding = grid_projects.load_script(PROJECT, SCRIPT)["scenes"][0]["generated_assets"]["shot_grid"]
    assert set(binding) == {"grid_id", "layout", "composite_path", "cell_paths", "confirmed_at"}

    unconfirmed = await grid_client.post(f"{BASE}/{saved['id']}/unconfirm")
    assert unconfirmed.status_code == 200
    assert unconfirmed.json()["grid"]["status"] == "completed"
    assert not any(cell["confirmed"] for cell in unconfirmed.json()["grid"]["cells"])
    assert not grid_projects.load_script(PROJECT, SCRIPT)["scenes"][0]["generated_assets"].get("shot_grid")
    reconfirmed = await grid_client.post(f"{BASE}/{saved['id']}/confirm")
    assert reconfirmed.status_code == 200
    assert reconfirmed.json()["grid"]["status"] == "confirmed"
    locked = await grid_client.put(BASE, json=_payload(rows=3, cols=3, prompt=""))
    assert locked.status_code == 409
    await grid_client.post(f"{BASE}/{saved['id']}/unconfirm")
    resized = await grid_client.put(BASE, json=_payload(rows=3, cols=3, prompt=""))
    assert resized.status_code == 200
    assert resized.json()["status"] == "draft"


async def test_confirmation_rolls_back_cells_grid_and_script_when_registration_fails(
    grid_client: httpx.AsyncClient, grid_projects: ProjectManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = (await grid_client.put(BASE, json=_payload())).json()
    project_path = grid_projects.get_project_path(PROJECT)
    _write_composite(project_path, saved["id"], 640, 360)
    _mark_completed(ShotGridManager(project_path), saved["id"])
    grid_file = project_path / "shot_grids" / f"{saved['id']}.json"
    script_file = project_path / "scripts" / SCRIPT
    before = (grid_file.read_bytes(), script_file.read_bytes())
    monkeypatch.setattr(shot_grids, "register_current_resource_artifact", lambda *args, **kwargs: False)
    response = await grid_client.post(f"{BASE}/{saved['id']}/confirm")
    assert response.status_code == 422
    assert (grid_file.read_bytes(), script_file.read_bytes()) == before
    assert not list((project_path / "storyboards" / saved["id"]).glob("*.png"))


@pytest.mark.parametrize("count", [3, 4, 5])
async def test_author_returns_exact_draft_without_writing_or_truncating(
    grid_client: httpx.AsyncClient, grid_projects: ProjectManager, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    from lib.backends.text_generator import TextGenerator

    generator = SimpleNamespace(
        generate=AsyncMock(
            return_value=SimpleNamespace(
                text=json.dumps(
                    {"frames": [{"shot_size": "全景", "description": f"画面{index}"} for index in range(count)]}
                )
            )
        )
    )
    monkeypatch.setattr(TextGenerator, "create", AsyncMock(return_value=generator))
    response = await grid_client.post(f"{BASE}/author-prompt", json=_payload())
    assert response.status_code == (200 if count == 4 else 502)
    assert not ShotGridManager(grid_projects.get_project_path(PROJECT)).list_all()


def _save_generated_grid(project_path: Path, grid_id: str, scene: str) -> None:
    """直接落一条「出图完成、尚未确认」的记录，并写上图与出图时的文本指纹。

    不经 PUT 造记录：这里只验证 GET 的过期判定，保存口径由 PUT 自己的用例负责。产物清单按出图
    提交的真实口径登记：``shot_grids`` 资源登记成这一镜的分镜图键，路径指向合成图本身。
    """

    composite = _write_composite(project_path, grid_id, 1280, 720)
    ShotGridManager(project_path).save(
        ShotGrid(
            id=grid_id,
            episode=1,
            script_file=SCRIPT,
            shot_id=SHOT,
            rows=2,
            cols=2,
            prompt=PROMPT,
            status="completed",
            composite_path=composite,
            scene_revision=scene_revision_of(scene),
            # 未确认的合成图不该被拿去比规范期望，写不写基准都不影响判定。
            image_basis={"kind": "grid", "kind_version": 1, "digest": "sha256-v1:" + "0" * 64},
        )
    )
    ArtifactManifest(ProjectArtifactManifestAdapter(project_path)).register_entry_transactionally(
        ArtifactKey.episode_storyboard(1, SHOT),
        ArtifactManifestEntry(artifact_path=composite, basis_digest="sha256-v1:" + "1" * 64),
    )


async def test_completed_grid_is_not_stale(grid_client: httpx.AsyncClient, grid_projects: ProjectManager) -> None:
    """刚出完图、尚未确认的宫格不算过期。

    回归点：未确认的合成图没有对应的规范期望，拿它跟分镜图的期望比对只会得到「不等」，
    表现为「刚生成完就过期」，连带确认入口一起锁死。
    """

    _save_generated_grid(grid_projects.get_project_path(PROJECT), "shot_grid_0000000000f1", SCENE)

    response = await grid_client.get(BASE, params={"script_file": SCRIPT, "shot_id": SHOT})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["stale"] is False


async def test_scene_change_marks_the_grid_stale(grid_client: httpx.AsyncClient, grid_projects: ProjectManager) -> None:
    """出图后镜头文本改了才判过期：这是 stale 唯一要回答的问题。"""

    project_path = grid_projects.get_project_path(PROJECT)
    _save_generated_grid(project_path, "shot_grid_0000000000e2", SCENE)

    edited = SCENE.replace("白瓷酒盏刚刚滚落", "青铜酒盏刚刚滚落")
    script_path = project_path / "scripts" / SCRIPT
    script = json.loads(script_path.read_text(encoding="utf-8"))
    script["scenes"][0]["image_prompt"]["scene"] = edited
    script_path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")

    response = await grid_client.get(BASE, params={"script_file": SCRIPT, "shot_id": SHOT})

    assert response.status_code == 200
    assert response.json()["stale"] is True
