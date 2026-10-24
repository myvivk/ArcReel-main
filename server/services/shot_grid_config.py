"""参考素材（单分镜宫格）的配置与逐格文本生成：HTTP 路由与 Agent 工具共用同一份判定。

落盘口径（ADR 0099 D-4 / D-5）不变：配置只在调用方明确保存时写入；AI 生成的逐格文本由调用方
决定是否落盘——路由回填弹窗输入框，Agent 拿回执后再决定配置哪一镜。

失败以 ``lib.infra.api_errors`` 的领域异常抛出，错误码就是 i18n key：路由由 app 级 handler
翻成 HTTP 状态与本地化文案，Agent 工具按 ``DEFAULT_LOCALE`` 转述给模型。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from lib.infra.api_errors import (
    BadGatewayError,
    BadRequestError,
    ConflictError,
    NotFoundError,
    UnprocessableError,
)
from lib.project.project_manager import ProjectManager
from lib.script.factory_episode import require_factory_script
from lib.script.grid.keyframe_parser import (
    ShotFrame,
    format_grid_prompt,
    parse_grid_prompt_cells,
    parse_scene_shots,
    scene_revision_of,
    shot_scene_text,
)
from lib.script.grid.shot_grid_manager import ShotGridManager
from lib.script.grid.shot_models import ShotGrid
from lib.script.grid.shot_prompt_builder import shot_grid_cells
from lib.script.storyboard_sequence import get_storyboard_items

logger = logging.getLogger(__name__)

#: 开放给用户的宫格规格（ADR 0099 D-1）：2×2（默认）/ 2×3 / 3×3。
SUPPORTED_LAYOUTS = {(2, 2), (2, 3), (3, 3)}

_GENERATING_STATUSES = {"generating", "confirming"}

_AUTHORED_SYSTEM_PROMPT = """你是分镜画师的提示词编辑助理。用户给你一个分镜的画面描述（镜头序列）和这一镜要画的格数，你为每一格写一段自包含的关键帧画面描述。只输出结构化结果，不写任何解释。"""

_AUTHORED_USER_TEMPLATE = """【分镜画面】
{scene}

【出场资产】（只写名字，不要描述外观，外观由参考图承担）
{assets}

【项目画风】
{style}

【任务】
把上面这个分镜拆成 {count} 个关键帧，一格一个，按画面里的时间顺序排列。
要求：
- 恰好 {count} 个画面，一个不多一个不少
- 景别从「远景、全景、中景、近景、特写」中选，同一分镜内要有明显变化
- 每段是自包含的关键帧画面：主体姿态、表情、服装、环境、光线、氛围，用叙述句
- 资产直接写名字，不要写 @[] 之类的引用记号
- 不得增删事件，所有格必须在同一时空里连续发生
"""

#: 引用字段名 → 资产类型标签，喂给 AI 时只给名字与类型。
_REFERENCE_TYPE_LABELS = {"product": "商品", "character": "角色", "scene": "场景", "prop": "道具"}


class _AuthoredFrame(BaseModel):
    """AI 产出的一格：景别与自包含的关键帧画面描述。"""

    shot_size: str = Field(default="", max_length=40)
    description: str = Field(min_length=1, max_length=4000)


class _AuthoredGridPrompt(BaseModel):
    frames: list[_AuthoredFrame] = Field(min_length=1)


def assert_layout(rows: int, cols: int) -> None:
    """宫格规格只接受 2×2 / 2×3 / 3×3（ADR 0099 D-1）。"""

    if (rows, cols) not in SUPPORTED_LAYOUTS:
        raise BadRequestError("shot_grid_layout_invalid")


def _storyboard_shot(projects: ProjectManager, project_name: str, *, script_file: str, episode: int, shot_id: str):
    """这一集这一镜的剧本条目；不是工厂派生状态或找不到分镜时拒绝。"""

    script = projects.load_script(project_name, script_file)
    require_factory_script(
        projects.get_project_path(project_name), projects.load_project(project_name), script, episode
    )
    items, id_field, char_field, scene_field, prop_field = get_storyboard_items(script)
    shot = next((item for item in items if str(item.get(id_field, "")) == shot_id), None)
    if script.get("episode") != episode or shot is None:
        raise NotFoundError("shot_grid_shot_missing")
    return shot, (id_field, char_field, scene_field, prop_field)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def save_grid_config(
    projects: ProjectManager,
    project_name: str,
    *,
    script_file: str,
    episode: int,
    shot_id: str,
    rows: int,
    cols: int,
    prompt: str | None,
) -> ShotGrid:
    """保存宫格规格与逐格镜头文本，在入队前落盘（ADR 0099 D-4）。

    同规格保存保留已有结果和实际出图留证；改规格才重置。手写格数必须精确匹配。
    """

    shot, _ = _storyboard_shot(projects, project_name, script_file=script_file, episode=episode, shot_id=shot_id)
    scene_revision = scene_revision_of(shot_scene_text(shot))
    assert_layout(rows, cols)
    manager = ShotGridManager(projects.get_project_path(project_name))
    grid = manager.find(script_file=script_file, shot_id=shot_id)
    if grid is None:
        # 首次保存：记录按本次规格新建，逐格文本与留证照常落盘——新建分支不能吞掉 prompt，
        # 否则弹窗里写好的逐格文本在「确定」这一步被丢掉，出图只能回退去解析 scene。
        grid = ShotGrid.create(episode=episode, script_file=script_file, shot_id=shot_id, rows=rows, cols=cols)
        saved_prompt = prompt or ""
    else:
        saved_prompt = prompt if prompt is not None else grid.prompt
        frames = parse_grid_prompt_cells(saved_prompt)
        if saved_prompt.strip() and len(frames) != rows * cols:
            raise UnprocessableError("shot_grid_cells_invalid", count=rows * cols)
        if (grid.rows, grid.cols) == (rows, cols) and saved_prompt == grid.prompt:
            return grid
        if grid.status == "confirmed" and (grid.rows, grid.cols) != (rows, cols):
            raise ConflictError("shot_grid_spec_locked")
        if grid.status in _GENERATING_STATUSES:
            raise ConflictError("shot_grid_busy")
        if (grid.rows, grid.cols) == (rows, cols):
            grid.prompt = saved_prompt
            grid.updated_at = _now()
            manager.save(grid)
            return grid

    frames = parse_grid_prompt_cells(saved_prompt)
    if saved_prompt.strip() and len(frames) != rows * cols:
        raise UnprocessableError("shot_grid_cells_invalid", count=rows * cols)
    grid.rows, grid.cols = rows, cols
    grid.prompt = saved_prompt
    grid.scene_revision = scene_revision
    # 逐格文本是唯一输入：服务端解析并回填留证（正文进 prompt、景别进 continuity_notes）。
    grid.cells = shot_grid_cells(frames, grid.rows, grid.cols) if frames else []
    grid.status = "draft"
    grid.composite_path = None
    grid.task_id = None
    grid.image_basis = None
    grid.error_message = None
    grid.confirmed_at = None
    grid.updated_at = _now()
    manager.save(grid)
    return grid


def _shot_asset_lines(shot: dict[str, Any], char_field: str | None, scene_field: str, prop_field: str) -> str:
    """这一分镜登记的资产名单（名字 + 类型），按字段序。"""

    lines: list[str] = []
    for field_name, asset_type in (
        ("products_in_shot", "product"),
        (char_field, "character"),
        (scene_field, "scene"),
        (prop_field, "prop"),
    ):
        if not field_name:
            continue
        names = shot.get(field_name)
        if not isinstance(names, list):
            continue
        lines.extend(
            f"- {name.strip()}（{_REFERENCE_TYPE_LABELS[asset_type]}）"
            for name in names
            if isinstance(name, str) and name.strip()
        )
    return "\n".join(lines) if lines else "- （这一分镜没有登记资产）"


def _serialize_authored_frames(frames: list[_AuthoredFrame], count: int) -> str:
    """模型必须提供恰好指定数量的完整关键帧，不截断、不补写。"""

    if len(frames) != count or any(not frame.description.strip() for frame in frames):
        raise ValueError(f"模型需返回 {count} 个非空关键帧")
    return format_grid_prompt(
        [
            ShotFrame(number=index + 1, shot_size=frame.shot_size, text=frame.description)
            for index, frame in enumerate(frames)
        ]
    )


async def author_grid_cells(
    projects: ProjectManager,
    project_name: str,
    *,
    script_file: str,
    episode: int,
    shot_id: str,
    rows: int,
    cols: int,
    user_id: str,
) -> str:
    """生成逐格镜头文本，不落盘、不入队（ADR 0099 D-5）。

    输入是当前 ``image_prompt.scene`` + 该分镜登记资产（只给名字与类型）+ 项目画风 + 格数；
    输出是纯逐格镜头文本。落盘只发生在调用方明确保存时——AI 生成了但用户取消，不会留下半截状态。

    ``TextOutputTruncatedError`` 原样上抛：HTTP 侧统一翻成 author_failed，Agent 入口按
    ``text_output_truncated`` 报出模型与出路。
    """

    from lib.backends.providers import CallPurpose
    from lib.backends.text_backends.base import (
        TextGenerationRequest,
        TextTaskType,
    )
    from lib.backends.text_generator import TextGenerator

    shot, (_, char_field, scene_field, prop_field) = _storyboard_shot(
        projects, project_name, script_file=script_file, episode=episode, shot_id=shot_id
    )
    assert_layout(rows, cols)
    scene_text = shot_scene_text(shot)
    if not scene_text.strip():
        raise BadRequestError("shot_grid_scene_missing")
    try:
        parse_scene_shots(scene_text)
    except ValueError as exc:
        raise BadRequestError("shot_grid_scene_not_sequence") from exc

    project = await asyncio.to_thread(projects.load_project, project_name)
    count = rows * cols
    generator = await TextGenerator.create(
        TextTaskType.SCRIPT, project_name, purpose=CallPurpose.SCRIPT_GENERATION, user_id=user_id
    )
    try:
        result = await generator.generate(
            TextGenerationRequest(
                prompt=_AUTHORED_USER_TEMPLATE.format(
                    scene=scene_text.strip(),
                    assets=_shot_asset_lines(shot, char_field, scene_field, prop_field),
                    style=(project.get("style") or "").strip() or "（未设置）",
                    count=count,
                ),
                system_prompt=_AUTHORED_SYSTEM_PROMPT,
                response_schema=_AuthoredGridPrompt,
            ),
            project_name=project_name,
        )
        authored = _AuthoredGridPrompt.model_validate_json(result.text)
        return _serialize_authored_frames(authored.frames, count)
    except (ValidationError, ValueError) as exc:
        logger.warning("shot grid author-prompt rejected the model output: %s", exc)
        raise BadGatewayError("shot_grid_author_failed") from exc
    except Exception as exc:
        logger.exception("shot grid author-prompt failed")
        raise BadGatewayError("shot_grid_author_failed") from exc


__all__ = [
    "SUPPORTED_LAYOUTS",
    "assert_layout",
    "author_grid_cells",
    "save_grid_config",
]
