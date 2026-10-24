"""参考素材单镜配置的落盘口径（ADR 0099 D-4 / D-5）：规格、逐格文本、确认锁定与逐格文本生成。

路由与 Agent 工具共用这一份实现，因此行为在服务层直接回归，不经 HTTP。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lib.infra.api_errors import BadRequestError, ConflictError, NotFoundError, UnprocessableError
from lib.project.project_manager import ProjectManager
from lib.script.grid.shot_grid_manager import ShotGridManager
from lib.script.grid.shot_models import ShotGrid
from lib.script.short_drama_source import replace_short_drama_source
from server.services.shot_grid_config import author_grid_cells, save_grid_config

PROJECT = "demo"
SCRIPT = "episode_1.json"
SHOT = "E1S01"

SCENE = "村口黄昏。【镜头1｜全景】土路向远处延伸。【镜头2｜中景】张三抬头看天。【镜头3｜近景】张三攥紧衣角。"

FOUR_CELLS = "\n".join(f"第{index}格（中景）：画面{index}" for index in range(1, 5))


@pytest.fixture
def factory_projects(tmp_path: Path) -> ProjectManager:
    manager = ProjectManager(tmp_path / "projects")
    manager.create_project(PROJECT)
    manager.create_project_metadata(PROJECT, "Demo", content_mode="drama")
    source = replace_short_drama_source(manager, PROJECT, 1, "# 第1集\n黄昏时分，风吹过村口。\n")
    script = {
        "episode": 1,
        "title": "第一集",
        "content_mode": "drama",
        "short_drama_meta": {"source_revision": source.revision},
        "scenes": [
            {
                "scene_id": SHOT,
                "source_text": "黄昏时分，风吹过村口。",
                "image_prompt": {"scene": SCENE},
                "video_prompt": {"action": ""},
                "duration_seconds": 8,
                "generated_assets": {},
            }
        ],
    }
    scripts = manager.get_project_path(PROJECT) / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / SCRIPT).write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    manager.add_episode(PROJECT, 1, "第一集", f"scripts/{SCRIPT}")
    return manager


def _save(projects: ProjectManager, *, rows: int = 2, cols: int = 2, prompt: str = FOUR_CELLS) -> ShotGrid:
    return save_grid_config(
        projects,
        PROJECT,
        script_file=SCRIPT,
        episode=1,
        shot_id=SHOT,
        rows=rows,
        cols=cols,
        prompt=prompt,
    )


def _grid(projects: ProjectManager) -> ShotGrid:
    grid = ShotGridManager(projects.get_project_path(PROJECT)).find(script_file=SCRIPT, shot_id=SHOT)
    assert grid is not None
    return grid


def test_first_save_creates_a_draft_with_parsed_cells(factory_projects: ProjectManager) -> None:
    grid = _save(factory_projects)

    assert grid.status == "draft"
    assert (grid.rows, grid.cols) == (2, 2)
    assert grid.prompt == FOUR_CELLS
    assert [cell.prompt for cell in grid.cells] == [f"画面{index}" for index in range(1, 5)]
    assert grid.scene_revision
    assert _grid(factory_projects).id == grid.id


def test_same_layout_and_prompt_keeps_the_generated_grid(factory_projects: ProjectManager) -> None:
    saved = _save(factory_projects)
    manager = ShotGridManager(factory_projects.get_project_path(PROJECT))

    def mark_completed(current: ShotGrid) -> None:
        current.status = "completed"
        current.composite_path = f"shot_grids/{current.id}.png"

    manager.update_formal(saved.id, mark_completed)

    again = _save(factory_projects)
    assert again.status == "completed"
    assert again.composite_path == f"shot_grids/{saved.id}.png"


def test_changing_the_layout_resets_the_generated_grid(factory_projects: ProjectManager) -> None:
    saved = _save(factory_projects)
    manager = ShotGridManager(factory_projects.get_project_path(PROJECT))

    def mark_completed(current: ShotGrid) -> None:
        current.status = "completed"
        current.composite_path = f"shot_grids/{current.id}.png"

    manager.update_formal(saved.id, mark_completed)
    prompt = "\n".join(f"第{index}格（中景）：画面{index}" for index in range(1, 7))

    resized = _save(factory_projects, rows=2, cols=3, prompt=prompt)

    assert (resized.rows, resized.cols) == (2, 3)
    assert resized.status == "draft"
    assert resized.composite_path is None
    assert len(resized.cells) == 6


def test_confirmed_grid_locks_the_layout(factory_projects: ProjectManager) -> None:
    saved = _save(factory_projects)
    ShotGridManager(factory_projects.get_project_path(PROJECT)).update_formal(
        saved.id, lambda current: setattr(current, "status", "confirmed")
    )

    with pytest.raises(ConflictError) as error:
        _save(factory_projects, rows=3, cols=3, prompt="")

    assert error.value.key == "shot_grid_spec_locked"


def test_generating_grid_rejects_a_save(factory_projects: ProjectManager) -> None:
    saved = _save(factory_projects)
    ShotGridManager(factory_projects.get_project_path(PROJECT)).update_formal(
        saved.id, lambda current: setattr(current, "status", "generating")
    )

    with pytest.raises(ConflictError) as error:
        _save(factory_projects, prompt="")

    assert error.value.key == "shot_grid_busy"


def test_cell_count_mismatch_is_rejected_without_a_record(factory_projects: ProjectManager) -> None:
    with pytest.raises(UnprocessableError) as error:
        _save(factory_projects, rows=2, cols=3)

    assert error.value.key == "shot_grid_cells_invalid"
    assert error.value.params == {"count": 6}
    assert ShotGridManager(factory_projects.get_project_path(PROJECT)).list_all() == []


def test_unknown_layout_is_rejected(factory_projects: ProjectManager) -> None:
    with pytest.raises(BadRequestError) as error:
        _save(factory_projects, rows=3, cols=2)

    assert error.value.key == "shot_grid_layout_invalid"


def test_unknown_shot_is_rejected(factory_projects: ProjectManager) -> None:
    with pytest.raises(NotFoundError) as error:
        save_grid_config(
            factory_projects,
            PROJECT,
            script_file=SCRIPT,
            episode=1,
            shot_id="E1S99",
            rows=2,
            cols=2,
            prompt="",
        )

    assert error.value.key == "shot_grid_shot_missing"


async def test_author_formats_exactly_the_requested_cells(
    factory_projects: ProjectManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lib.backends.text_generator import TextGenerator

    generator = SimpleNamespace(
        generate=AsyncMock(
            return_value=SimpleNamespace(
                text=json.dumps(
                    {"frames": [{"shot_size": "全景", "description": f"画面{index}"} for index in range(1, 5)]},
                    ensure_ascii=False,
                )
            )
        )
    )
    monkeypatch.setattr(TextGenerator, "create", AsyncMock(return_value=generator))

    prompt = await author_grid_cells(
        factory_projects,
        PROJECT,
        script_file=SCRIPT,
        episode=1,
        shot_id=SHOT,
        rows=2,
        cols=2,
        user_id="default",
    )

    assert prompt == "\n".join(f"第{index}格（全景）：画面{index}" for index in range(1, 5))
    assert ShotGridManager(factory_projects.get_project_path(PROJECT)).list_all() == []


async def test_author_rejects_a_scene_without_a_shot_sequence(
    factory_projects: ProjectManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lib.backends.text_generator import TextGenerator

    create = AsyncMock()
    monkeypatch.setattr(TextGenerator, "create", create)
    script = factory_projects.load_script(PROJECT, SCRIPT)
    script["scenes"][0]["image_prompt"] = {"scene": "村口黄昏。"}
    (factory_projects.get_project_path(PROJECT) / "scripts" / SCRIPT).write_text(
        json.dumps(script, ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(BadRequestError) as error:
        await author_grid_cells(
            factory_projects,
            PROJECT,
            script_file=SCRIPT,
            episode=1,
            shot_id=SHOT,
            rows=2,
            cols=2,
            user_id="default",
        )

    assert error.value.key == "shot_grid_scene_not_sequence"
    create.assert_not_awaited()
