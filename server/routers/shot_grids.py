"""Per-shot multi-frame grid planning API."""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from lib.artifacts.artifact_activation import register_current_resource_artifact
from lib.artifacts.artifact_manifest import ArtifactBasisDescriptor
from lib.artifacts.formal_write import formal_write_transaction
from lib.generation.generation_queue import get_generation_queue
from lib.infra.path_safety import safe_join
from lib.project.project_manager import get_project_manager
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
from server.auth import CurrentUser
from server.i18n import Translator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_name}/shot-grids", tags=["分镜镜头宫格"])

#: 弹窗开放的宫格规格（ADR 0098 D7 / 0099 D-1）。
SUPPORTED_LAYOUTS = {(2, 2), (2, 3), (3, 3)}


class SaveShotGridRequest(BaseModel):
    script_file: str
    episode: int = Field(ge=1)
    shot_id: str = Field(min_length=1, max_length=200)
    rows: int = Field(default=2, ge=2, le=3)
    cols: int = Field(default=2, ge=2, le=3)
    #: 本次出图的逐格镜头文本；``None`` 表示保持原值，``""`` 表示明确清空（ADR 0099 D-4）。
    prompt: str | None = Field(default=None, max_length=12000)


class AuthorShotGridPromptRequest(BaseModel):
    script_file: str
    episode: int = Field(ge=1)
    shot_id: str = Field(min_length=1, max_length=200)
    rows: int = Field(default=2, ge=2, le=3)
    cols: int = Field(default=2, ge=2, le=3)


class _AuthoredFrame(BaseModel):
    """AI 产出的一格：景别与自包含的关键帧画面描述。"""

    model_config = ConfigDict(extra="forbid")

    shot_size: str = Field(default="", max_length=40)
    description: str = Field(min_length=1, max_length=4000)


class _AuthoredGridPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frames: list[_AuthoredFrame] = Field(min_length=1)


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


def _shot_asset_lines(item: dict, char_field: str | None) -> str:
    """这一分镜登记的资产名单（名字 + 类型），按字段序。"""

    lines: list[str] = []
    for field_name, asset_type in (
        ("products_in_shot", "product"),
        (char_field, "character"),
        ("scenes_in_shot", "scene"),
        ("props_in_shot", "prop"),
    ):
        if not field_name:
            continue
        names = item.get(field_name)
        if not isinstance(names, list):
            continue
        lines.extend(
            f"- {name.strip()}（{_REFERENCE_TYPE_LABELS[asset_type]}）"
            for name in names
            if isinstance(name, str) and name.strip()
        )
    return "\n".join(lines) if lines else "- （这一分镜没有登记资产）"


def _serialize_authored_frames(frames: list[_AuthoredFrame], count: int) -> str:
    """结构化产出规整为弹窗输入框的逐格文本；超出格数的尾格丢弃，不足留给延续格。"""

    return format_grid_prompt(
        [
            ShotFrame(number=index + 1, shot_size=frame.shot_size, text=frame.description)
            for index, frame in enumerate(frames[:count])
        ]
    )


@router.get("")
def get_shot_grid(project_name: str, script_file: str, shot_id: str, request: Request):
    grid = ShotGridManager(get_project_manager().get_project_path(project_name)).find(
        script_file=script_file, shot_id=shot_id
    )
    if grid is None:
        return None
    result = grid.to_dict()
    if grid.composite_path:
        result["composite_url"] = (
            f"{request.scope.get('root_path', '')}/api/v1/projects/{project_name}/shot-grids/{grid.id}/image"
        )
    return result


@router.get("/{grid_id}/image")
def get_shot_grid_image(project_name: str, grid_id: str, t: Translator):
    project_path = get_project_manager().get_project_path(project_name)
    grid = ShotGridManager(project_path).get(grid_id)
    if grid is None or not grid.composite_path:
        raise HTTPException(status_code=404, detail=t("shot_grid_missing"))
    image_path = safe_join(project_path, grid.composite_path)
    if not image_path.is_file():
        raise HTTPException(status_code=404, detail=t("shot_grid_missing"))
    return FileResponse(image_path, media_type="image/png")


@router.put("")
async def save_shot_grid(project_name: str, req: SaveShotGridRequest, t: Translator):
    """保存宫格规格与逐格镜头文本，在「确定」入队前落盘（ADR 0099 D-4）。

    幂等：规格、逐格文本与解析出的留证完全相同（含打开即取消式的原样保存）时不动任何生成
    状态，已出的宫格图不会被「看一眼」清掉。仅逐格文本变化才重置为待生成；已确认的宫格禁止
    改规格，改提示词后重新走一遍生成 + 确认（R7）。
    """

    project_manager = get_project_manager()
    script = project_manager.load_script(project_name, req.script_file)
    items, id_field, _, _, _ = get_storyboard_items(script)
    shot = next((item for item in items if str(item.get(id_field, "")) == req.shot_id), None)
    if script.get("episode") != req.episode or shot is None:
        raise HTTPException(status_code=404, detail=t("shot_grid_shot_missing"))
    scene_revision = scene_revision_of(shot_scene_text(shot))
    manager = ShotGridManager(project_manager.get_project_path(project_name))
    grid = manager.find(script_file=req.script_file, shot_id=req.shot_id)
    if grid is not None and grid.status in {"generating", "confirming"}:
        task = await get_generation_queue().get_task(grid.task_id) if grid.task_id else None
        if grid.status == "confirming" or not task or task["status"] not in {"failed", "cancelled"}:
            raise HTTPException(status_code=409, detail=t("shot_grid_busy"))

    if (req.rows, req.cols) not in SUPPORTED_LAYOUTS:
        raise HTTPException(status_code=400, detail=t("shot_grid_layout_invalid"))
    if grid is not None and grid.status == "confirmed" and (grid.rows, grid.cols) != (req.rows, req.cols):
        raise HTTPException(status_code=409, detail=t("shot_grid_spec_locked"))

    # 逐格文本是唯一输入：服务端解析并回填留证（正文进 prompt、景别进 continuity_notes）。
    prompt = req.prompt if req.prompt is not None else (grid.prompt if grid is not None else "")
    frames = parse_grid_prompt_cells(prompt)
    cells = shot_grid_cells(frames, req.rows, req.cols) if frames else []

    if grid is None:
        grid = ShotGrid.create(
            episode=req.episode,
            script_file=req.script_file,
            shot_id=req.shot_id,
            rows=req.rows,
            cols=req.cols,
        )
    elif (
        grid.rows == req.rows
        and grid.cols == req.cols
        and grid.prompt == prompt
        and [(cell.prompt, cell.continuity_notes) for cell in grid.cells]
        == [(cell.prompt, cell.continuity_notes) for cell in cells]
    ):
        # 完全相同：只刷新 scene 指纹（「分镜提示词已变更」的提示据此消失），不碰生成状态。
        if grid.scene_revision != scene_revision:
            grid.scene_revision = scene_revision
            grid.updated_at = datetime.now(UTC).isoformat()
            manager.save(grid)
        return grid.to_dict()

    grid.rows, grid.cols, grid.cells = req.rows, req.cols, cells
    grid.prompt = prompt
    grid.scene_revision = scene_revision
    grid.status = "draft"
    grid.composite_path = None
    grid.task_id = None
    grid.image_basis = None
    grid.error_message = None
    grid.updated_at = datetime.now(UTC).isoformat()
    manager.save(grid)
    return grid.to_dict()


@router.post("/author-prompt")
async def author_shot_grid_prompt(
    project_name: str, req: AuthorShotGridPromptRequest, user: CurrentUser, t: Translator
) -> dict[str, str]:
    """同步生成逐格镜头文本，不落盘、不入队（ADR 0099 D-5）。

    输入是当前 ``image_prompt.scene`` + 该分镜登记资产（只给名字与类型）+ 项目画风 + 格数；
    输出是纯逐格镜头文本，由前端回填弹窗输入框。落盘只发生在用户点「确定」时——AI 生成了但
    用户取消，不会留下半截状态。
    """

    from pydantic import ValidationError

    from lib.backends.providers import CallPurpose
    from lib.backends.text_backends.base import (
        TextGenerationRequest,
        TextOutputTruncatedError,
        TextTaskType,
    )
    from lib.backends.text_generator import TextGenerator

    project_manager = get_project_manager()
    script = project_manager.load_script(project_name, req.script_file)
    items, id_field, char_field, _, _ = get_storyboard_items(script)
    shot = next((item for item in items if str(item.get(id_field, "")) == req.shot_id), None)
    if script.get("episode") != req.episode or shot is None:
        raise HTTPException(status_code=404, detail=t("shot_grid_shot_missing"))
    if (req.rows, req.cols) not in SUPPORTED_LAYOUTS:
        raise HTTPException(status_code=400, detail=t("shot_grid_layout_invalid"))
    scene_text = shot_scene_text(shot)
    if not scene_text.strip():
        raise HTTPException(status_code=400, detail=t("shot_grid_scene_missing"))
    try:
        parse_scene_shots(scene_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=t("shot_grid_scene_not_sequence")) from exc

    project = await asyncio.to_thread(project_manager.load_project, project_name)
    count = req.rows * req.cols
    generator = await TextGenerator.create(
        TextTaskType.SCRIPT, project_name, purpose=CallPurpose.SCRIPT_GENERATION, user_id=user.id
    )
    try:
        result = await generator.generate(
            TextGenerationRequest(
                prompt=_AUTHORED_USER_TEMPLATE.format(
                    scene=scene_text.strip(),
                    assets=_shot_asset_lines(shot, char_field),
                    style=(project.get("style") or "").strip() or "（未设置）",
                    count=count,
                ),
                system_prompt=_AUTHORED_SYSTEM_PROMPT,
                response_schema=_AuthoredGridPrompt,
                max_output_tokens=4000,
            ),
            project_name=project_name,
        )
        authored = _AuthoredGridPrompt.model_validate_json(result.text)
    except (TextOutputTruncatedError, ValidationError, ValueError) as exc:
        logger.warning("shot grid author-prompt rejected the model output: %s", exc)
        raise HTTPException(status_code=502, detail=t("shot_grid_author_failed")) from exc
    except Exception as exc:
        logger.exception("shot grid author-prompt failed")
        raise HTTPException(status_code=502, detail=t("shot_grid_author_failed")) from exc
    return {"prompt": _serialize_authored_frames(authored.frames, count)}


@router.post("/generate")
async def generate_shot_grid(project_name: str, script_file: str, shot_id: str, user: CurrentUser, t: Translator):
    project_manager = get_project_manager()
    project_path = project_manager.get_project_path(project_name)
    manager = ShotGridManager(project_path)
    grid = manager.find(script_file=script_file, shot_id=shot_id)
    if grid is None:
        raise HTTPException(status_code=404, detail=t("shot_grid_missing"))
    # 守卫只看 ShotGrid.prompt：逐格留证由服务端在出图时解析回填（ADR 0099 D-7）。
    if not parse_grid_prompt_cells(grid.prompt):
        raise HTTPException(status_code=400, detail=t("shot_grid_prompt_empty"))
    if grid.status == "generating":
        return {"task_id": grid.task_id, "status": grid.status}

    queue = get_generation_queue()
    task = await queue.enqueue_task(
        project_name=project_name,
        task_type="shot_grid",
        media_type="image",
        resource_id=grid.id,
        payload={"shot_grid_id": grid.id, "script_file": script_file},
        script_file=script_file,
        source="webui",
        user_id=user.id,
    )

    def mark_submitted(current):
        if current.status in {"draft", "failed", "generating"}:
            current.status = "generating"
            current.task_id = task["task_id"]
            current.error_message = None
            current.updated_at = datetime.now(UTC).isoformat()

    manager.update_formal(grid.id, mark_submitted)
    return {"task_id": task["task_id"], "status": task["status"]}


@router.get("/tasks/{task_id}")
async def get_shot_grid_task(project_name: str, task_id: str, t: Translator):
    task = await get_generation_queue().get_task(task_id)
    if task is None or task.get("task_type") != "shot_grid" or task.get("project_name") != project_name:
        raise HTTPException(status_code=404, detail=t("shot_grid_missing"))
    return task


@router.post("/{grid_id}/confirm")
def confirm_shot_grid(project_name: str, grid_id: str, request: Request, t: Translator):
    project_manager = get_project_manager()
    project_path = project_manager.get_project_path(project_name)
    manager = ShotGridManager(project_path)
    grid = manager.get(grid_id)
    if grid is None:
        raise HTTPException(status_code=404, detail=t("shot_grid_missing"))
    if grid.status == "confirmed":
        return {"success": True, "grid": get_shot_grid(project_name, grid.script_file, grid.shot_id, request)}
    if grid.status != "completed" or not grid.composite_path:
        raise HTTPException(status_code=409, detail=t("shot_grid_not_ready"))

    composite = safe_join(project_path, grid.composite_path)
    count = grid.rows * grid.cols
    if len(grid.cells) != count or [cell.index for cell in grid.cells] != list(range(count)):
        raise HTTPException(status_code=422, detail=t("shot_grid_cells_invalid", count=count))
    try:
        with Image.open(composite) as image:
            image.load()
            width, height = image.size
            if width < grid.cols or height < grid.rows:
                raise ValueError("contact sheet dimensions are smaller than its layout")
            cells_dir = safe_join(project_path, f"storyboards/{grid.id}")
            cells_dir.mkdir(parents=True, exist_ok=True)
            paths = []
            for cell in grid.cells:
                row, col = divmod(cell.index, grid.cols)
                left = round(col * width / grid.cols)
                top = round(row * height / grid.rows)
                right = round((col + 1) * width / grid.cols)
                bottom = round((row + 1) * height / grid.rows)
                output = cells_dir / f"{cell.index}.png"
                image.crop((left, top, right, bottom)).save(output, format="PNG")
                paths.append(output.relative_to(project_path).as_posix())
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=t("shot_grid_split_failed")) from exc

    # 正式分镜绑定整张宫格图；视频首尾帧与参考图从同一图切出。
    storyboard = cells_dir / "contact.png"
    composite_rel = storyboard.relative_to(project_path).as_posix()
    basis = ArtifactBasisDescriptor.from_dict(grid.image_basis) if grid.image_basis else None

    def register(_script_path):
        if not register_current_resource_artifact(
            project_path,
            resource_type="storyboards",
            resource_id=grid.shot_id,
            script_file=grid.script_file,
            artifact_path=composite_rel,
            basis=basis,
        ):
            raise HTTPException(status_code=422, detail=t("shot_grid_register_failed"))
        grid.status = "confirmed"
        # 登记在案的那一份就是复合路径：确认后整图以 storyboards/ 下的副本充当分镜图，
        # 记录随之改指同一路径，「参考素材」行的时效比对才能对上清单里登记的那一件。
        grid.composite_path = composite_rel
        grid.updated_at = datetime.now(UTC).isoformat()
        for cell, path in zip(grid.cells, paths, strict=True):
            cell.confirmed = True
            cell.image_path = path
        manager.save(grid)

    with formal_write_transaction(storyboard):
        shutil.copyfile(composite, storyboard)
        with project_manager.locked_script(
            project_name, grid.script_file, validate=False, on_commit=register
        ) as script:
            items, id_field, _, _, _ = get_storyboard_items(script)
            shot = next((item for item in items if str(item.get(id_field, "")) == grid.shot_id), None)
            if shot is None:
                raise HTTPException(status_code=404, detail=t("shot_grid_shot_missing"))
            assets = shot.setdefault("generated_assets", {})
            assets["storyboard_image"] = composite_rel
            assets["shot_grid"] = {
                "grid_id": grid.id,
                "layout": f"{grid.rows}x{grid.cols}",
                "composite_path": composite_rel,
                "cell_paths": paths,
                "start_cell": 0,
                "key_cell": count // 2,
                "end_cell": count - 1,
                "video_reference_mode": "start_end_cells",
                "confirmed_at": datetime.now(UTC).isoformat(),
            }
            project_manager.update_scene_status(shot)
    return {"success": True, "grid": get_shot_grid(project_name, grid.script_file, grid.shot_id, request)}
