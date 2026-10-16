"""Per-shot multi-frame grid planning API."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel, Field

from lib.artifacts.artifact_activation import register_current_resource_artifact
from lib.artifacts.artifact_manifest import ArtifactBasisDescriptor
from lib.artifacts.formal_write import formal_write_transaction
from lib.generation.generation_queue import get_generation_queue
from lib.infra.path_safety import safe_join
from lib.project.project_manager import get_project_manager
from lib.script.grid.shot_grid_manager import ShotGridManager
from lib.script.grid.shot_models import ShotGrid, ShotGridCell
from lib.script.storyboard_sequence import get_storyboard_items
from server.auth import CurrentUser
from server.i18n import Translator

router = APIRouter(prefix="/projects/{project_name}/shot-grids", tags=["分镜镜头宫格"])


class ShotGridCellInput(BaseModel):
    index: int = Field(ge=0)
    prompt: str = Field(default="", max_length=4000)


class SaveShotGridRequest(BaseModel):
    script_file: str
    episode: int = Field(ge=1)
    shot_id: str = Field(min_length=1, max_length=200)
    rows: int = Field(default=3, ge=2, le=3)
    cols: int = Field(default=3, ge=2, le=3)
    cells: list[ShotGridCellInput] = Field(default_factory=list)


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
    project_manager = get_project_manager()
    script = project_manager.load_script(project_name, req.script_file)
    items, id_field, _, _, _ = get_storyboard_items(script)
    if script.get("episode") != req.episode or not any(item.get(id_field) == req.shot_id for item in items):
        raise HTTPException(status_code=404, detail=t("shot_grid_shot_missing"))
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
    elif grid.status in {"generating", "confirming"}:
        task = await get_generation_queue().get_task(grid.task_id) if grid.task_id else None
        if grid.status == "confirming" or not task or task["status"] not in {"failed", "cancelled"}:
            raise HTTPException(status_code=409, detail=t("shot_grid_busy"))

    if (req.rows, req.cols) not in {(2, 2), (2, 3), (3, 3)}:
        raise HTTPException(status_code=400, detail=t("shot_grid_layout_invalid"))
    count = req.rows * req.cols
    if req.cells and len(req.cells) != count:
        raise HTTPException(status_code=400, detail=t("shot_grid_cells_invalid", count=count))
    by_index = {cell.index: cell for cell in req.cells}
    if req.cells and (len(by_index) != count or set(by_index) != set(range(count))):
        raise HTTPException(status_code=400, detail=t("shot_grid_cells_invalid", count=count))
    grid.rows, grid.cols = req.rows, req.cols
    grid.cells = [
        ShotGridCell(
            index=index,
            prompt=by_index[index].prompt if index in by_index else "Follow the shot description at this moment.",
            time_ratio=index / (count - 1),
        )
        for index in range(count)
    ]
    grid.status = "draft"
    grid.composite_path = None
    grid.task_id = None
    grid.image_basis = None
    grid.error_message = None
    grid.updated_at = datetime.now(UTC).isoformat()
    manager.save(grid)
    return grid.to_dict()


@router.post("/generate")
async def generate_shot_grid(project_name: str, script_file: str, shot_id: str, user: CurrentUser, t: Translator):
    project_manager = get_project_manager()
    project_path = project_manager.get_project_path(project_name)
    manager = ShotGridManager(project_path)
    grid = manager.find(script_file=script_file, shot_id=shot_id)
    if grid is None:
        raise HTTPException(status_code=404, detail=t("shot_grid_missing"))
    if not any(cell.prompt.strip() for cell in grid.cells):
        raise HTTPException(status_code=400, detail=t("shot_grid_cells_invalid", count=grid.rows * grid.cols))
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
            project_path, resource_type="storyboards", resource_id=grid.shot_id,
            script_file=grid.script_file, artifact_path=composite_rel, basis=basis,
        ):
            raise HTTPException(status_code=422, detail=t("shot_grid_register_failed"))
        grid.status = "confirmed"
        grid.updated_at = datetime.now(UTC).isoformat()
        for cell, path in zip(grid.cells, paths, strict=True):
            cell.confirmed = True
            cell.image_path = path
        manager.save(grid)

    with formal_write_transaction(storyboard):
        shutil.copyfile(composite, storyboard)
        with project_manager.locked_script(project_name, grid.script_file, validate=False, on_commit=register) as script:
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
