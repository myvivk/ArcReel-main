"""参考素材 Agent 工具（``configure_shot_grid`` / ``generate_shot_grids`` / ``author_shot_grid_prompt``）。

覆盖三条边界：配置只落盘不入队、生成保留已配置的规格与逐格文本、已确认的宫格不进批量重做。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lib.script.grid.shot_grid_manager import ShotGridManager
from lib.script.grid.shot_models import ShotGrid
from tests.integration.server.agent_tool_support import (
    ToolHarness,
    prepare_factory_script,
    problem_of,
    read_generation_result,
    run_declared_tool,
)

SCRIPT = "episode_1.json"
SHOT = "E1S01"

#: 四格可解析的镜头序列（scene 回退路径与 AI 写格都以它为输入）。
SCENE = (
    "【镜头1｜全景】村口黄昏，土路向远处延伸。\n"
    "【镜头2｜中景】张三站在土路上，抬头看天。\n"
    "【镜头3｜近景】张三的手攥紧衣角。\n"
    "【镜头4｜特写】风吹动衣角。"
)

CUSTOM_PROMPT = (
    "第1格（全景）：村口黄昏，土路尽头升起炊烟。\n"
    "第2格（中景）：张三回头望向村口。\n"
    "第3格（近景）：张三握紧了手里的信。\n"
    "第4格（特写）：信纸边角被风吹起。"
)


@pytest.fixture
def grid_factory_ctx(fake_ctx: ToolHarness) -> ToolHarness:
    """工厂派生状态 + 一镜四格的镜头序列（`fake_ctx` 的旁白骨架换成可解析的 scene）。"""

    prepare_factory_script(fake_ctx)
    scene = fake_ctx.pm.script_payload["scenes"][0]
    scene["image_prompt"] = {"opening": "", "scene": SCENE}
    scene["generated_assets"] = {}
    return fake_ctx


def _grid(ctx: ToolHarness, shot_id: str = SHOT) -> ShotGrid | None:
    return ShotGridManager(ctx.project_path).find(script_file=SCRIPT, shot_id=shot_id)


async def _succeed_every_spec(*, specs, **_batch_kwargs):
    from lib.generation.generation_queue_client import BatchTaskResult

    return [
        BatchTaskResult(
            resource_id=spec.resource_id,
            task_id="t1",
            status="succeeded",
            result={"file_path": f"shot_grids/{spec.resource_id}.png"},
        )
        for spec in specs
    ], []


async def test_configure_saves_layout_and_cells_without_queueing(grid_factory_ctx: ToolHarness) -> None:
    async def unreachable_batch(**_batch_kwargs):
        raise AssertionError("配置不该入队")

    out = await run_declared_tool(
        "configure_shot_grid",
        grid_factory_ctx,
        {"script": SCRIPT, "shot_id": SHOT, "rows": 2, "cols": 2, "prompt": CUSTOM_PROMPT},
        batch_waiter=unreachable_batch,
    )

    assert out.problem is None
    assert out.value is not None
    assert out.value["status"] == "draft"
    assert (out.value["rows"], out.value["cols"], out.value["cells"]) == (2, 2, 4)
    grid = _grid(grid_factory_ctx)
    assert grid is not None
    assert grid.prompt == CUSTOM_PROMPT
    assert [cell.prompt for cell in grid.cells] == [line.split("：", 1)[1] for line in CUSTOM_PROMPT.splitlines()]


async def test_configure_rejects_a_cell_count_mismatch(grid_factory_ctx: ToolHarness) -> None:
    out = await run_declared_tool(
        "configure_shot_grid",
        grid_factory_ctx,
        {"script": SCRIPT, "shot_id": SHOT, "rows": 2, "cols": 3, "prompt": CUSTOM_PROMPT},
    )

    assert problem_of(out).code == "shot_grid_cells_invalid"
    assert _grid(grid_factory_ctx) is None


async def test_generate_honors_the_configured_layout_and_prompt(grid_factory_ctx: ToolHarness) -> None:
    await run_declared_tool(
        "configure_shot_grid",
        grid_factory_ctx,
        {"script": SCRIPT, "shot_id": SHOT, "rows": 2, "cols": 2, "prompt": CUSTOM_PROMPT},
    )
    captured: list[dict[str, object]] = []

    async def fake_batch(*, specs, **batch_kwargs):
        captured.extend(specs)
        return await _succeed_every_spec(specs=specs, **batch_kwargs)

    out = await run_declared_tool("generate_shot_grids", grid_factory_ctx, {"script": SCRIPT}, batch_waiter=fake_batch)

    result = read_generation_result(out)
    assert result.succeeded == [SHOT]
    grid = _grid(grid_factory_ctx)
    assert grid is not None
    # 批量入口会写成 2×2 + 清空 prompt；Agent 入口必须保留单镜配置。
    assert (grid.rows, grid.cols, grid.prompt) == (2, 2, CUSTOM_PROMPT)
    assert len(captured) == 1


async def test_generate_falls_back_to_two_by_two_and_scene_when_unconfigured(grid_factory_ctx: ToolHarness) -> None:
    async def fake_batch(*, specs, **batch_kwargs):
        return await _succeed_every_spec(specs=specs, **batch_kwargs)

    out = await run_declared_tool("generate_shot_grids", grid_factory_ctx, {"script": SCRIPT}, batch_waiter=fake_batch)

    assert read_generation_result(out).succeeded == [SHOT]
    grid = _grid(grid_factory_ctx)
    assert grid is not None
    assert (grid.rows, grid.cols, grid.prompt) == (2, 2, "")
    assert [cell.prompt for cell in grid.cells]


async def test_generate_reports_an_in_flight_grid_as_skipped(grid_factory_ctx: ToolHarness) -> None:
    await run_declared_tool(
        "configure_shot_grid",
        grid_factory_ctx,
        {"script": SCRIPT, "shot_id": SHOT, "rows": 2, "cols": 2, "prompt": CUSTOM_PROMPT},
    )
    grid = _grid(grid_factory_ctx)
    assert grid is not None
    ShotGridManager(grid_factory_ctx.project_path).update_formal(
        grid.id, lambda current: setattr(current, "status", "generating")
    )

    async def unreachable_batch(**_batch_kwargs):
        raise AssertionError("已在生成中的宫格不该再入队")

    out = await run_declared_tool(
        "generate_shot_grids", grid_factory_ctx, {"script": SCRIPT}, batch_waiter=unreachable_batch
    )

    result = read_generation_result(out)
    assert [item.unit_id for item in result.skipped] == [SHOT]
    assert result.requested == []


async def test_generate_rejects_a_confirmed_grid_before_queueing(grid_factory_ctx: ToolHarness) -> None:
    await run_declared_tool(
        "configure_shot_grid",
        grid_factory_ctx,
        {"script": SCRIPT, "shot_id": SHOT, "rows": 2, "cols": 2, "prompt": CUSTOM_PROMPT},
    )
    grid = _grid(grid_factory_ctx)
    assert grid is not None
    ShotGridManager(grid_factory_ctx.project_path).update_formal(
        grid.id, lambda current: setattr(current, "status", "confirmed")
    )

    async def unreachable_batch(**_batch_kwargs):
        raise AssertionError("已确认的宫格不该入队")

    out = await run_declared_tool(
        "generate_shot_grids",
        grid_factory_ctx,
        {"script": SCRIPT, "shot_ids": [SHOT]},
        batch_waiter=unreachable_batch,
    )

    assert problem_of(out).code == "shot_grid_confirmed"
    assert problem_of(out).params == {"unit_ids": [SHOT]}


async def test_author_returns_formatted_cells_without_writing(
    grid_factory_ctx: ToolHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lib.backends.text_generator import TextGenerator

    generator = SimpleNamespace(
        generate=AsyncMock(
            return_value=SimpleNamespace(
                text=json.dumps(
                    {
                        "frames": [
                            {"shot_size": "全景", "description": "村口黄昏。"},
                            {"shot_size": "中景", "description": "张三抬头。"},
                            {"shot_size": "近景", "description": "张三攥手。"},
                            {"shot_size": "特写", "description": "衣角被风吹动。"},
                            {"shot_size": "近景", "description": "张三转身。"},
                            {"shot_size": "全景", "description": "炊烟升起。"},
                        ]
                    },
                    ensure_ascii=False,
                )
            )
        )
    )
    monkeypatch.setattr(TextGenerator, "create", AsyncMock(return_value=generator))

    out = await run_declared_tool(
        "author_shot_grid_prompt",
        grid_factory_ctx,
        {"script": SCRIPT, "shot_id": SHOT, "rows": 2, "cols": 3},
    )

    assert out.problem is None
    assert out.value is not None
    lines = out.value["prompt"].splitlines()
    assert len(lines) == 6
    assert lines[0].startswith("第1格（全景）：")
    assert _grid(grid_factory_ctx) is None
