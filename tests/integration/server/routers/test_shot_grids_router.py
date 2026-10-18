"""单分镜参考素材宫格的 HTTP 面：保存规格与逐格文本、入队、布局校验。

回归点：``PUT /shot-grids`` 的请求体必须带 ``layout`` 派生属性（与 ``POST /generate``
共用同一族规格模型），否则处理函数在 ``_assert_layout`` 处抛 ``AttributeError`` → 500；
``POST /generate`` 入队时引用的是查询参数 ``script_file``，不能残留已删除的 ``req``。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from lib.project.project_manager import ProjectManager
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


async def test_draft_save_keeps_the_generated_grid(
    grid_client: httpx.AsyncClient, grid_projects: ProjectManager
) -> None:
    """草稿只写逐格文本：关闭弹窗不会把已出的宫格图或生成状态清掉。"""

    saved = (await grid_client.put(BASE, json=_payload())).json()
    composite = _mark_completed(ShotGridManager(grid_projects.get_project_path(PROJECT)), saved["id"])
    edited = f"{PROMPT}\n第5格（远景）：街角的雨。"

    response = await grid_client.put(f"{BASE}/draft", json=_payload(prompt=edited))

    assert response.status_code == 200
    body = response.json()
    assert body["prompt"] == edited
    assert body["status"] == "completed"
    assert body["composite_path"] == composite
    # 规格仍是已提交的那一档：草稿不碰切格口径
    assert (body["rows"], body["cols"]) == (2, 2)
    # 留证仍是上一次出图的那一份
    assert [cell["prompt"] for cell in body["cells"]] == CELL_TEXTS


async def test_draft_save_creates_a_record_with_the_dialog_layout(grid_client: httpx.AsyncClient) -> None:
    """没有记录时草稿也留得下：按弹窗当前规格新建，状态仍是待生成。"""

    response = await grid_client.put(f"{BASE}/draft", json=_payload(rows=3, cols=3))

    assert response.status_code == 200
    body = response.json()
    assert body["prompt"] == PROMPT
    assert body["status"] == "draft"
    assert (body["rows"], body["cols"]) == (3, 3)
    assert body["composite_path"] is None


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
