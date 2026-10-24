"""参考素材（单分镜宫格）的 Agent 工具：配置单镜规格与逐格文本、生成 K 帧宫格。

两条写入口分工与 Web 一致：``configure_shot_grid`` 只落配置（规格 + 逐格镜头文本，不付费、不入队），
``generate_shot_grids`` 按已配置的记录入队出图。确认仍是人的一步——K 帧是否可用是视觉判断，
工具不替用户点确认；已确认的宫格也不进批量重做（ADR 0099 R7）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

from lib.backends.text_backends.base import TextOutputTruncatedError
from lib.generation.generation_result import (
    GenerationResultBuilder,
    normalize_requested_ids,
    record_batch_outcomes,
)
from lib.i18n import DEFAULT_LOCALE
from lib.i18n import _ as translate
from lib.infra.api_errors import ApiError
from lib.infra.async_thread import run_sync_transaction
from lib.infra.schema_guards import is_int
from lib.script.grid.shot_grid_manager import ShotGridManager
from lib.script.grid.shot_models import ShotGrid
from server.media_tools.context import (
    GenerationToolValue,
    RequestedIds,
    ScriptFilename,
    generation_batch_submission_outcome,
    generation_result_outcome,
    tool_error,
    tool_problem,
)
from server.services.admission.storyboard_batch import (
    SHOT_GRID_BATCH_OPERATION,
    ShotGridBatchPlan,
    load_shot_grid_batch_plan_for_script,
    shot_grid_task_specs,
    write_shot_grid_records,
)
from server.services.shot_grid_config import author_grid_cells, save_grid_config
from server.tool_runtime import (
    CallerContext,
    ProjectScope,
    Services,
    ToolOutcome,
    ToolRequest,
    submit_media_generation,
    truncation_problem,
)

logger = logging.getLogger(__name__)

_AUTHOR_OPERATION = "author_shot_grid_prompt"
_CONFIGURE_OPERATION = "configure_shot_grid"

_EPISODE_FROM_SCRIPT = re.compile(r"episode_(\d+)\.json")


class AuthorShotGridPromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    script: ScriptFilename = Field(description="剧本纯文件名（不含目录），如 episode_1.json")
    shot_id: str = Field(min_length=1, max_length=200, description="分镜 ID，如 E1S01")
    rows: int = Field(default=2, ge=2, le=3, description="宫格行数：2×2 填 2，2×3 填 2，3×3 填 3")
    cols: int = Field(default=2, ge=2, le=3, description="宫格列数：2×2 填 2，2×3 填 3，3×3 填 3")


class ConfigureShotGridRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    script: ScriptFilename = Field(description="剧本纯文件名（不含目录），如 episode_1.json")
    shot_id: str = Field(min_length=1, max_length=200, description="分镜 ID，如 E1S01")
    rows: int = Field(default=2, ge=2, le=3, description="宫格行数：2×2 填 2，2×3 填 2，3×3 填 3")
    cols: int = Field(default=2, ge=2, le=3, description="宫格列数：2×2 填 2，2×3 填 3，3×3 填 3")
    prompt: str = Field(
        default="",
        max_length=12000,
        description=(
            "本次出图的逐格镜头文本，按顺序每格一行，格式「第1格（景别）：画面描述」；"
            "格数须与 rows×cols 一致。留空表示出图时从 image_prompt.scene 现解析镜头序列。"
        ),
    )


class GenerateShotGridsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    script: ScriptFilename = Field(description="剧本纯文件名（不含目录），如 episode_1.json")
    shot_ids: RequestedIds | SkipJsonSchema[None] = Field(
        default=None,
        description=(
            "要出图的分镜 ID 列表；省略则只选还没有可用参考素材的分镜。"
            "已确认参考素材的分镜不能走这里重做，须先在分镜详情取消确认。"
        ),
    )


def script_episode(script: dict[str, Any], script_file: str) -> int:
    """剧本身份的集号：以字段为准，缺字段时按 ``episode_N.json`` 兜底。"""

    if is_int(script.get("episode"), minimum=1):
        return int(script["episode"])
    match = _EPISODE_FROM_SCRIPT.fullmatch(script_file)
    return int(match.group(1)) if match else 1


def _api_problem(exc: ApiError) -> ToolOutcome[Any]:
    """领域异常按默认语言转述给 Agent，问题码沿用异常的 i18n key。"""

    return tool_problem(
        translate(exc.key, DEFAULT_LOCALE, **exc.params),
        code=exc.key,
        params=dict(exc.params) or None,
    )


async def author_shot_grid_prompt(
    request: ToolRequest[AuthorShotGridPromptRequest],
    scope: ProjectScope,
    caller: CallerContext,
    services: Services,
) -> ToolOutcome[dict[str, Any]]:
    """生成逐格镜头文本，不落盘、不入队；由调用方决定是否写进配置。"""

    value = request.value
    try:
        script = services.projects.load_script(scope.project_name, value.script)
        prompt = await author_grid_cells(
            services.projects,
            scope.project_name,
            script_file=value.script,
            episode=script_episode(script, value.script),
            shot_id=value.shot_id,
            rows=value.rows,
            cols=value.cols,
            user_id=caller.user_id,
        )
    except TextOutputTruncatedError as exc:
        return ToolOutcome(problem=truncation_problem(exc))
    except ApiError as exc:
        return _api_problem(exc)
    except Exception as exc:
        logger.exception("%s 失败", _AUTHOR_OPERATION)
        return tool_error(_AUTHOR_OPERATION, exc)
    return ToolOutcome(
        value={
            "shot_id": value.shot_id,
            "rows": value.rows,
            "cols": value.cols,
            "cells": value.rows * value.cols,
            "prompt": prompt,
        }
    )


async def configure_shot_grid(
    request: ToolRequest[ConfigureShotGridRequest],
    scope: ProjectScope,
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[dict[str, Any]]:
    """保存单镜宫格规格与逐格镜头文本，不付费也不入队。"""

    value = request.value
    try:
        script = services.projects.load_script(scope.project_name, value.script)
        grid = await run_sync_transaction(
            save_grid_config,
            services.projects,
            scope.project_name,
            script_file=value.script,
            episode=script_episode(script, value.script),
            shot_id=value.shot_id,
            rows=value.rows,
            cols=value.cols,
            prompt=value.prompt,
        )
    except ApiError as exc:
        return _api_problem(exc)
    except Exception as exc:
        logger.exception("%s 失败", _CONFIGURE_OPERATION)
        return tool_error(_CONFIGURE_OPERATION, exc)
    return ToolOutcome(
        value={
            "shot_id": grid.shot_id,
            "grid_id": grid.id,
            "rows": grid.rows,
            "cols": grid.cols,
            "cells": len(grid.cells),
            "status": grid.status,
            "prompt_configured": bool(grid.prompt.strip()),
        }
    )


def _confirmed_requested(project_path: Path, script_file: str, shot_ids: list[str] | None) -> list[str]:
    """点名重做里已经确认过参考素材的分镜：整批准入前拒绝，指引先取消确认（R7）。"""

    if not shot_ids:
        return []
    wanted = set(shot_ids)
    return [
        grid.shot_id
        for grid in ShotGridManager(project_path).list_all()
        if grid.script_file == script_file and grid.status == "confirmed" and grid.shot_id in wanted
    ]


def _result_builder(plan: ShotGridBatchPlan, prepared: Mapping[str, ShotGrid]) -> GenerationResultBuilder:
    """结果集以规划为底；已在生成中的目标记为跳过——这次提交不会为它们新建任务。"""

    builder = GenerationResultBuilder.from_preflight(plan.preflight)
    for unit_id in plan.target_ids:
        if unit_id in prepared or unit_id not in plan.generating:
            continue
        builder.skip_unit(unit_id)
    return builder


async def generate_shot_grids(
    request: ToolRequest[GenerateShotGridsRequest],
    scope: ProjectScope,
    caller: CallerContext,
    services: Services,
) -> ToolOutcome[GenerationToolValue]:
    """按已配置的记录为缺失参考素材的分镜入队出图；确认仍留给用户。"""

    try:
        script_file = request.value.script
        shot_ids = normalize_requested_ids(request.value.shot_ids, field="shot_ids")
        loaded, plan = await load_shot_grid_batch_plan_for_script(
            services.projects,
            services.queue,
            project_name=scope.project_name,
            script_file=script_file,
            user_id=caller.user_id,
            requested_ids=shot_ids,
            honor_configured=True,
        )
        confirmed = _confirmed_requested(loaded.project_path, script_file, shot_ids)
        if confirmed:
            return tool_problem(
                "分镜 " + "、".join(confirmed) + " 的参考素材已经确认；重做需要先在分镜详情取消确认，"
                "已确认的宫格不会由批量入口重做。",
                code="shot_grid_confirmed",
                params={"unit_ids": confirmed},
            )
        prepared = await run_sync_transaction(
            write_shot_grid_records, plan, project_path=loaded.project_path, honor_configured=True
        )
        specs = shot_grid_task_specs(plan, prepared, source=caller.source)
        submitted = await submit_media_generation(
            scope=scope,
            caller=caller,
            services=services,
            operation=SHOT_GRID_BATCH_OPERATION,
            preflight=plan.preflight,
            pending_ids=[unit_id for unit_id in plan.target_ids if unit_id in prepared],
            specs=specs,
            states=plan.states,
        )
        if submitted.successes is None or submitted.failures is None:
            return generation_batch_submission_outcome(submitted.batch)
        builder = _result_builder(plan, prepared)
        # 队列按宫格记录寻址、结果集按分镜 ID 陈述：两者在这里对齐，Agent 不必知道 grid_id。
        unit_id_by_grid_id = {grid.id: unit_id for unit_id, grid in prepared.items()}
        record_batch_outcomes(
            builder,
            successes=submitted.successes,
            failures=submitted.failures,
            states=plan.states,
            unit_id_of=lambda resource_id: unit_id_by_grid_id.get(resource_id, resource_id),
            # 参考素材的可用性以确认后的切图为准；这里不替用户报产物时效。
            resolver=None,
        )
        return generation_result_outcome(builder.build(), batch_id=submitted.batch.batch_id)
    except ApiError as exc:
        return _api_problem(exc)
    except Exception as exc:
        logger.exception("%s 失败", SHOT_GRID_BATCH_OPERATION)
        return tool_error(SHOT_GRID_BATCH_OPERATION, exc)


__all__ = [
    "AuthorShotGridPromptRequest",
    "ConfigureShotGridRequest",
    "GenerateShotGridsRequest",
    "author_shot_grid_prompt",
    "configure_shot_grid",
    "generate_shot_grids",
    "script_episode",
]
