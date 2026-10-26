"""Per-shot multi-frame grid planning API.

单分镜出图优先使用 ``ShotGrid.prompt``：弹窗仅在「确定」时落盘，
POST /generate 与任务层都用它；``image_prompt.scene`` 仅作「prompt 为空」时的回退（整集批量
走这条，不预存 prompt）。AI 生成提示词的同步端点 ``/author-prompt`` 把 scene + 资产 + 画风
喂给模型，产出纯逐格文本回填弹窗输入框；前端在 AI 返回与关闭弹窗时把文本存成草稿
（``PUT /draft``，D-5），「确定」才保存规格并入队。

保存与逐格文本生成与 Agent 工具共用 :mod:`server.services.shot_grid_config` 的同一份判定；
领域异常由 app 级 handler 翻成 HTTP 状态与本地化文案。
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel, Field

from lib.artifacts.artifact_activation import (
    active_artifact_currency_resolver,
    forget_current_resource_artifact,
    register_current_resource_artifact,
)
from lib.artifacts.artifact_manifest import ArtifactBasisDescriptor, ArtifactKey, ArtifactStatus
from lib.artifacts.formal_write import formal_write_transaction
from lib.backends.text_backends.base import TextOutputTruncatedError
from lib.generation.generation_queue import get_generation_queue
from lib.infra.api_errors import BadGatewayError
from lib.infra.async_thread import run_sync_transaction
from lib.infra.path_safety import safe_join
from lib.project.project_manager import get_project_manager
from lib.script.factory_episode import require_factory_script
from lib.script.grid.keyframe_parser import (
    parse_grid_prompt_cells,
    parse_scene_shots,
    scene_revision_of,
    shot_scene_text,
)
from lib.script.grid.shot_grid_manager import ShotGridManager
from lib.script.grid.shot_models import ShotGrid
from lib.script.grid.shot_prompt_builder import cell_duplication_problem, shot_grid_cells
from lib.script.grid.splitter import split_grid_image
from lib.script.storyboard_sequence import get_storyboard_items
from server.auth import CurrentUser
from server.i18n import Translator
from server.services.shot_grid_config import (
    author_grid_cells,
    save_grid_config,
    save_grid_draft,
)
from server.services.tasks.formal_image_commit import get_aspect_ratio

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_name}/shot-grids", tags=["分镜镜头宫格"])

_GENERATING_STATUSES = {"generating", "confirming"}


class _GridSpecRequest(BaseModel):
    """PUT / POST /generate / author-prompt 共用的寻址 + 规格，没有一个文本字段。"""

    script_file: str = Field(min_length=1)
    episode: int = Field(ge=1)
    shot_id: str = Field(min_length=1, max_length=200)
    rows: int = Field(default=2, ge=2, le=3)
    cols: int = Field(default=2, ge=2, le=3)

    @property
    def layout(self) -> tuple[int, int]:
        return self.rows, self.cols


class SaveShotGridRequest(_GridSpecRequest):
    #: 本次出图的逐格镜头文本；``None`` 表示保持原值，``""`` 表示明确清空（ADR 0099 D-4）。
    prompt: str | None = Field(default=None, max_length=12000)


class SaveShotGridDraftRequest(_GridSpecRequest):
    """弹窗里还没点「确定」的逐格文本：只保存成草稿，不触发一次生成（ADR 0099 D-5）。"""

    prompt: str = Field(default="", max_length=12000)


class AuthorShotGridPromptRequest(_GridSpecRequest):
    pass


@router.post("/author-prompt")
async def author_shot_grid_prompt(
    project_name: str, req: AuthorShotGridPromptRequest, user: CurrentUser
) -> dict[str, str]:
    """同步生成逐格镜头文本，不落盘、不入队（ADR 0099 D-5）。

    输入是当前 ``image_prompt.scene`` + 该分镜登记资产（只给名字与类型）+ 项目画风 + 格数；
    输出是纯逐格镜头文本，由前端回填弹窗输入框并立即存成草稿（D-5）；「确定」才保存规格并入队。
    """

    try:
        prompt = await author_grid_cells(
            get_project_manager(),
            project_name,
            script_file=req.script_file,
            episode=req.episode,
            shot_id=req.shot_id,
            rows=req.rows,
            cols=req.cols,
            user_id=user.id,
        )
    except TextOutputTruncatedError as exc:
        # 弹窗只回填文本，没有重试通道；截断与其它模型失败同样按 502 报出。
        raise BadGatewayError("shot_grid_author_failed") from exc
    return {"prompt": prompt}


@router.get("")
def get_shot_grid(project_name: str, script_file: str, shot_id: str, request: Request):
    grid = ShotGridManager(get_project_manager().get_project_path(project_name)).find(
        script_file=script_file, shot_id=shot_id
    )
    if grid is None:
        return None
    result = grid.to_dict()
    project_manager = get_project_manager()
    script = project_manager.load_script(project_name, script_file)
    items, id_field, _, _, _ = get_storyboard_items(script)
    shot = next((item for item in items if str(item.get(id_field, "")) == shot_id), None)
    result["stale"] = shot is None or (
        grid.scene_revision is not None and grid.scene_revision != scene_revision_of(shot_scene_text(shot))
    )
    # 「过期」只回答一件事：这份上图还是不是按当前的输入出的。
    #
    # 产物比对只在宫格已确认时才做：确认会把合成图登记成这一镜的分镜图（``storyboards/<grid>/contact.png``），
    # 那时拿它去比分镜图的规范期望才有意义。未确认的合成图没有对应的规范期望——拿 ``shot_grids/<grid>.png``
    # 去比，只会得到「路径不同 / 基准不同」，把刚出完图的宫格一律判成过期，连带确认入口一起锁死。
    if grid.status == "confirmed" and grid.image_basis:
        project = project_manager.load_project(project_name)
        resolver = active_artifact_currency_resolver(project_manager.get_project_path(project_name), project)
        comparison = resolver.compare(
            ArtifactKey.episode_storyboard(grid.episode, grid.shot_id),
            artifact_path=f"storyboards/{grid.id}/contact.png",
        )
        result["stale"] = result["stale"] or comparison.status in {ArtifactStatus.STALE, ArtifactStatus.BLOCKED}
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


@router.put("/draft")
def save_shot_grid_draft(project_name: str, req: SaveShotGridDraftRequest):
    """把弹窗里未提交的逐格文本存成草稿，AI 生成返回与关闭弹窗时调用（ADR 0099 D-5）。

    只写 ``prompt``：``status`` / ``composite_path`` / ``task_id`` / ``image_basis`` / ``cells``
    与 ``scene_revision`` 全部保持原样，已出的宫格图不会被「看一眼再关闭」清掉；「确定并生成」
    才把规格与文本作为一次提交（``PUT`` + ``POST /generate``）。
    """

    grid = save_grid_draft(
        get_project_manager(),
        project_name,
        script_file=req.script_file,
        episode=req.episode,
        shot_id=req.shot_id,
        rows=req.rows,
        cols=req.cols,
        prompt=req.prompt,
    )
    return grid.to_dict()


@router.put("")
def save_shot_grid(project_name: str, req: SaveShotGridRequest):
    """保存宫格规格与逐格镜头文本，在「确定」入队前落盘（ADR 0099 D-4）。

    同规格保存保留已有结果和实际出图留证；改规格才重置。手写格数必须精确匹配。
    """

    grid = save_grid_config(
        get_project_manager(),
        project_name,
        script_file=req.script_file,
        episode=req.episode,
        shot_id=req.shot_id,
        rows=req.rows,
        cols=req.cols,
        prompt=req.prompt,
    )
    return grid.to_dict()


@router.post("/generate")
async def generate_shot_grid(project_name: str, script_file: str, shot_id: str, user: CurrentUser, t: Translator):
    """保存过的宫格按 ``ShotGrid.prompt`` 出图；prompt 为空时回退到 ``image_prompt.scene`` 现解析
    （整集批量走这条，不预存 prompt）。不预存任何草稿（D-1 / D-3 / D-7）。"""

    project_manager = get_project_manager()
    project_path = project_manager.get_project_path(project_name)
    manager = ShotGridManager(project_path)
    grid = manager.find(script_file=script_file, shot_id=shot_id)
    if grid is None:
        raise HTTPException(status_code=404, detail=t("shot_grid_missing"))
    script = project_manager.load_script(project_name, script_file)
    require_factory_script(project_path, project_manager.load_project(project_name), script, grid.episode)
    if grid.status == "generating":
        return {"task_id": grid.task_id, "status": grid.status}

    # 逐格文本：用户填的 prompt 优先，否则现解析 scene（批量同路径）。
    frames = parse_grid_prompt_cells(grid.prompt) if grid.prompt else []
    if grid.prompt.strip() and len(frames) != grid.rows * grid.cols:
        raise HTTPException(status_code=422, detail=t("shot_grid_cells_invalid", count=grid.rows * grid.cols))
    if not frames:
        script = project_manager.load_script(project_name, script_file)
        items, id_field, _, _, _ = get_storyboard_items(script)
        shot = next((item for item in items if str(item.get(id_field, "")) == shot_id), None)
        if shot is None:
            raise HTTPException(status_code=404, detail=t("shot_grid_shot_missing"))
        if not shot_scene_text(shot).strip():
            raise HTTPException(status_code=400, detail=t("shot_grid_scene_missing"))
        try:
            frames = list(parse_scene_shots(shot_scene_text(shot)).shots)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=t("shot_grid_scene_not_sequence")) from exc
        # 镜头少于格数时，宫格是帧位（首帧/延续帧/尾帧），不是一镜一格：让模型按帧位写逐格文本，
        # 而不是把末镜重复填空。失败才回退到机械对齐，保证出图不因文本调用失败而卡死。
        if len(frames) < grid.rows * grid.cols:
            try:
                authored = await author_grid_cells(
                    project_manager,
                    project_name,
                    script_file=script_file,
                    episode=grid.episode,
                    shot_id=shot_id,
                    rows=grid.rows,
                    cols=grid.cols,
                    user_id=user.id,
                )
                authored_frames = parse_grid_prompt_cells(authored)
                if len(authored_frames) == grid.rows * grid.cols:
                    frames = authored_frames
            except Exception as exc:
                logger.warning("grid frame authoring failed for %s: %s", shot_id, exc)
            if len(frames) < grid.rows * grid.cols:
                raise HTTPException(
                    status_code=422,
                    detail="宫格逐格文本生成失败：镜头数不足，无法按帧位补齐（可重试或改用更小格数）",
                )
            problem = cell_duplication_problem(frames)
            if problem:
                raise HTTPException(status_code=422, detail=f"宫格逐格文本不合格：{problem}（可重试）")
    grid.cells = shot_grid_cells(frames, grid.rows, grid.cols)
    grid.composite_path = None
    grid.image_basis = None
    grid.error_message = None
    grid.confirmed_at = None
    grid.status = "draft"
    grid.updated_at = datetime.now(UTC).isoformat()
    manager.save(grid)

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

    def mark_submitted(current: ShotGrid) -> None:
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
    if task.get("status") in {"failed", "cancelled"}:
        # 排队期间取消的任务不会启动 worker；轮询消费终态时补齐宫格状态，允许原稿不变地重试。
        manager = ShotGridManager(get_project_manager().get_project_path(project_name))

        def reconcile(current: ShotGrid):
            if current.task_id == task_id and current.status == "generating":
                current.status = "failed"
                current.error_message = task.get("error_message") or t("shot_grid_generation_failed")
                current.updated_at = datetime.now(UTC).isoformat()

        await run_sync_transaction(manager.update_formal, str(task["resource_id"]), reconcile)
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
    # 不再拿 GET 的 stale 当确认门槛：它表达的是「输入相对出图时变了」，其中提示词这一条由下方切分事务
    # 里的 scene_revision 复核兜住；资产变化不该阻止用户确认一张已经出好的图。

    composite = safe_join(project_path, grid.composite_path)
    count = grid.rows * grid.cols
    if len(grid.cells) != count or [cell.index for cell in grid.cells] != list(range(count)):
        raise HTTPException(status_code=422, detail=t("shot_grid_cells_invalid", count=count))
    # 单格对齐的比例：优先用生图时冻结的画布比例（16:9 / 9:16），存量记录回退到项目当前比例。
    video_aspect_ratio = grid.video_aspect_ratio or get_aspect_ratio(
        project_manager.load_project(project_name), "storyboards"
    )
    cells_dir = safe_join(project_path, f"storyboards/{grid.id}")
    storyboard = cells_dir / "contact.png"
    composite_rel = storyboard.relative_to(project_path).as_posix()
    outputs = [cells_dir / f"{index}.png" for index in range(count)]
    paths = [output.relative_to(project_path).as_posix() for output in outputs]
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

    def bind():
        with formal_write_transaction(storyboard, *outputs):
            try:
                with Image.open(composite) as image:
                    image.load()
                    if image.width < grid.cols or image.height < grid.rows:
                        raise ValueError("contact sheet dimensions are smaller than its layout")
                    cells_dir.mkdir(parents=True, exist_ok=True)
                    split_cells = split_grid_image(image, grid.rows, grid.cols, video_aspect_ratio, edge_margin=0.0)
                    for cell_image, output in zip(split_cells, outputs, strict=True):
                        cell_image.save(output, format="PNG")
                    if composite != storyboard:
                        shutil.copyfile(composite, storyboard)
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=t("shot_grid_split_failed")) from exc
            with project_manager.locked_script(
                project_name, grid.script_file, validate=False, on_commit=register
            ) as script:
                items, id_field, _, _, _ = get_storyboard_items(script)
                shot = next((item for item in items if str(item.get(id_field, "")) == grid.shot_id), None)
                if shot is None:
                    raise HTTPException(status_code=404, detail=t("shot_grid_shot_missing"))
                if grid.scene_revision and grid.scene_revision != scene_revision_of(shot_scene_text(shot)):
                    raise HTTPException(status_code=409, detail=t("shot_grid_not_ready"))
                assets = shot.setdefault("generated_assets", {})
                assets["storyboard_image"] = composite_rel
                assets["shot_grid"] = {
                    "grid_id": grid.id,
                    "layout": f"{grid.rows}x{grid.cols}",
                    "rows": grid.rows,
                    "cols": grid.cols,
                    "composite_path": composite_rel,
                    "cell_paths": paths,
                    "confirmed_at": grid.confirmed_at,
                }
                project_manager.update_scene_status(shot)

    def mark_confirmed(current: ShotGrid):
        if current.to_dict() != grid.to_dict():
            raise HTTPException(status_code=409, detail=t("shot_grid_busy"))
        current.status = "confirmed"
        current.composite_path = composite_rel
        current.confirmed_at = datetime.now(UTC).isoformat()
        current.updated_at = current.confirmed_at
        grid.confirmed_at = current.confirmed_at
        for cell, path in zip(current.cells, paths, strict=True):
            cell.confirmed = True
            cell.image_path = path

    manager.update_formal(grid_id, mark_confirmed, on_commit=bind)
    return {"success": True, "grid": get_shot_grid(project_name, grid.script_file, grid.shot_id, request)}


@router.post("/{grid_id}/unconfirm")
def unconfirm_shot_grid(project_name: str, grid_id: str, request: Request, t: Translator):
    """解除确认和当前分镜绑定，保留图片供查看、再次确认或重做。"""
    project_manager = get_project_manager()
    project_path = project_manager.get_project_path(project_name)
    manager = ShotGridManager(project_path)
    grid = manager.get(grid_id)
    if grid is None:
        raise HTTPException(status_code=404, detail=t("shot_grid_missing"))
    if grid.status in _GENERATING_STATUSES:
        raise HTTPException(status_code=409, detail=t("shot_grid_busy"))
    if grid.status != "confirmed":
        return {"success": True, "grid": get_shot_grid(project_name, grid.script_file, grid.shot_id, request)}

    def mark_unconfirmed(current: ShotGrid):
        if current.to_dict() != grid.to_dict():
            raise HTTPException(status_code=409, detail=t("shot_grid_busy"))
        current.status = "completed"
        current.confirmed_at = None
        current.updated_at = datetime.now(UTC).isoformat()
        for cell in current.cells:
            cell.confirmed = False

    def unbind():
        removed_binding: list[bool] = []

        def forget(_script_path):
            if removed_binding:
                forget_current_resource_artifact(
                    project_path, resource_type="storyboards", resource_id=grid.shot_id, script_file=grid.script_file
                )

        with project_manager.locked_script(project_name, grid.script_file, validate=False, on_commit=forget) as script:
            items, id_field, _, _, _ = get_storyboard_items(script)
            shot = next((item for item in items if str(item.get(id_field, "")) == grid.shot_id), None)
            if shot is None:
                raise HTTPException(status_code=404, detail=t("shot_grid_shot_missing"))
            assets = shot.setdefault("generated_assets", {})
            binding = assets.get("shot_grid", {})
            if binding.get("grid_id") and binding.get("grid_id") != grid_id:
                raise HTTPException(status_code=409, detail=t("shot_grid_busy"))
            if binding.get("grid_id") == grid_id:
                assets.pop("shot_grid", None)
                if assets.get("storyboard_image") == grid.composite_path:
                    assets.pop("storyboard_image", None)
                    removed_binding.append(True)
            project_manager.update_scene_status(shot)

    manager.update_formal(grid_id, mark_unconfirmed, on_commit=unbind)
    return {"success": True, "grid": get_shot_grid(project_name, grid.script_file, grid.shot_id, request)}
