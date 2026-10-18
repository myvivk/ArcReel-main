"""Per-shot multi-frame grid planning API.

宫格没有"草稿文本"这一类状态：逐格镜头文本的真相源是 ``image_prompt.scene``（D-1 / D-3），
PUT 只存规格，POST /generate 现解析 scene 后直接入队。因此也不存在"打开看一眼再取消就把
已出的图清掉"的路径，更没有 AI 生成提示词的同步端点（D-5）。
"""

from __future__ import annotations

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
from lib.project.project_manager import ProjectManager, get_project_manager
from lib.script.grid.keyframe_parser import (
    SceneShots,
    parse_scene_shots,
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

#: 开放给用户的宫格规格（D-1）：2×2（默认）/ 2×3 / 3×3。
SUPPORTED_LAYOUTS = {(2, 2), (2, 3), (3, 3)}

_GENERATING_STATUSES = {"generating", "confirming"}


class ShotGridSpecRequest(BaseModel):
    """PUT 与 POST /generate 共用：寻址 + 规格，没有一个文本字段。"""

    model_config = ConfigDict(extra="forbid")

    script_file: str = Field(min_length=1)
    episode: int = Field(ge=1)
    shot_id: str = Field(min_length=1, max_length=200)
    rows: int = Field(default=2, ge=2, le=3)
    cols: int = Field(default=2, ge=2, le=3)

    @property
    def layout(self) -> tuple[int, int]:
        return self.rows, self.cols


def _load_shot(project_name: str, req: ShotGridSpecRequest, t: Translator) -> tuple[ProjectManager, dict]:
    """按 (集, 分镜) 取剧本条目；寻址不对一律 404，不把「写错 id」拖到入队之后。"""

    project_manager = get_project_manager()
    script = project_manager.load_script(project_name, req.script_file)
    items, id_field, _, _, _ = get_storyboard_items(script)
    shot = next((item for item in items if str(item.get(id_field, "")) == req.shot_id), None)
    if script.get("episode") != req.episode or shot is None:
        raise HTTPException(status_code=404, detail=t("shot_grid_shot_missing"))
    return project_manager, shot


def _assert_layout(req: ShotGridSpecRequest, t: Translator) -> None:
    if req.layout not in SUPPORTED_LAYOUTS:
        raise HTTPException(status_code=400, detail=t("shot_grid_layout_invalid"))


def _scene_shots(shot: dict, t: Translator) -> SceneShots:
    """逐格文本的唯一来源：现解析 ``image_prompt.scene``（D-7 的守卫与拼装共用）。"""

    scene = shot_scene_text(shot)
    if not scene.strip():
        raise HTTPException(status_code=400, detail=t("shot_grid_scene_missing"))
    try:
        return parse_scene_shots(scene)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=t("shot_grid_scene_not_sequence")) from exc


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
def save_shot_grid(project_name: str, req: ShotGridSpecRequest, t: Translator):
    """只存规格（D-4）：规格没变就一个字段都不动，变了才重置生成状态。

    旧实现无条件把 ``status`` 打回 ``draft`` 并清 ``composite_path``，改一次规格就等于把
    已出的图连整图一起清掉。这里把"重置"收敛到"规格真的变了"这一种情形。已确认的宫格锁
    规格：要换规格得先取消确认（R7）。
    """

    project_manager, _ = _load_shot(project_name, req, t)
    _assert_layout(req, t)
    manager = ShotGridManager(project_manager.get_project_path(project_name))
    grid = manager.find(script_file=req.script_file, shot_id=req.shot_id)
    if grid is None:
        grid = ShotGrid.create(
            episode=req.episode,
            script_file=req.script_file,
            shot_id=req.shot_id,
            rows=req.rows,
            cols=req.cols,
        )
        manager.save(grid)
        return grid.to_dict()
    if (grid.rows, grid.cols) == req.layout:
        # 规格相同：幂等返回，不碰 status / composite_path / cells / 任务。
        return grid.to_dict()

    if grid.status == "confirmed":
        raise HTTPException(status_code=409, detail=t("shot_grid_spec_locked"))
    if grid.status in _GENERATING_STATUSES:
        raise HTTPException(status_code=409, detail=t("shot_grid_busy"))

    grid.rows, grid.cols = req.layout
    # 旧格的行列与新规格对不上，合并只会产出自相矛盾的图：整体重解析（D-11）。
    grid.cells = []
    grid.status = "draft"
    grid.composite_path = None
    grid.task_id = None
    grid.image_basis = None
    grid.error_message = None
    grid.updated_at = datetime.now(UTC).isoformat()
    manager.save(grid)
    return grid.to_dict()


@router.post("/generate")
async def generate_shot_grid(project_name: str, req: ShotGridSpecRequest, user: CurrentUser, t: Translator):
    """按规格与 ``image_prompt.scene`` 直接入队，不预存任何草稿（D-1 / D-3 / D-7）。"""

    project_manager, shot = _load_shot(project_name, req, t)
    _assert_layout(req, t)
    parsed = _scene_shots(shot, t)
    project_path = project_manager.get_project_path(project_name)
    manager = ShotGridManager(project_path)
    grid = manager.find(script_file=req.script_file, shot_id=req.shot_id)
    if grid is not None and grid.status == "generating":
        return {"task_id": grid.task_id, "status": grid.status}
    if grid is not None and grid.status == "confirmed" and (grid.rows, grid.cols) != req.layout:
        raise HTTPException(status_code=409, detail=t("shot_grid_spec_locked"))

    if grid is None:
        grid = ShotGrid.create(
            episode=req.episode,
            script_file=req.script_file,
            shot_id=req.shot_id,
            rows=req.rows,
            cols=req.cols,
        )
    grid.rows, grid.cols = req.layout
    # 本次出图实际使用的逐格文本回填留证（正文进 prompt、景别进 continuity_notes）。
    grid.cells = shot_grid_cells(parsed.shots, grid.rows, grid.cols)
    grid.composite_path = None
    grid.image_basis = None
    grid.error_message = None
    grid.status = "draft"
    grid.updated_at = datetime.now(UTC).isoformat()
    manager.save(grid)

    queue = get_generation_queue()
    task = await queue.enqueue_task(
        project_name=project_name,
        task_type="shot_grid",
        media_type="image",
        resource_id=grid.id,
        payload={"shot_grid_id": grid.id, "script_file": req.script_file},
        script_file=req.script_file,
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
