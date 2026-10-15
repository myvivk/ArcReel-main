"""Per-shot multi-frame grid planning API."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel, Field

from lib.generation.generation_queue import get_generation_queue
from lib.infra.path_safety import safe_join
from lib.project.project_manager import get_project_manager
from lib.script.grid.shot_grid_manager import ShotGridManager
from lib.script.grid.shot_models import ShotGrid, ShotGridCell
from lib.script.storyboard_sequence import get_storyboard_items
from server.auth import CurrentUser

router = APIRouter(prefix="/projects/{project_name}/shot-grids", tags=["分镜镜头宫格"])


class ShotGridCellInput(BaseModel):
    index: int = Field(ge=0)
    prompt: str = Field(default="", max_length=4000)


class SaveShotGridRequest(BaseModel):
    script_file: str
    episode: int = Field(ge=1)
    shot_id: str = Field(min_length=1, max_length=200)
    rows: int = Field(default=3, ge=3, le=3)
    cols: int = Field(default=3, ge=3, le=3)
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
def get_shot_grid_image(project_name: str, grid_id: str):
    project_path = get_project_manager().get_project_path(project_name)
    grid = ShotGridManager(project_path).get(grid_id)
    if grid is None or not grid.composite_path:
        raise HTTPException(status_code=404, detail="联合图不存在")
    image_path = safe_join(project_path, grid.composite_path)
    if not image_path.is_file():
        raise HTTPException(status_code=404, detail="联合图文件不存在")
    return FileResponse(image_path, media_type="image/png")


@router.put("")
def save_shot_grid(project_name: str, req: SaveShotGridRequest):
    manager = ShotGridManager(get_project_manager().get_project_path(project_name))
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
        raise HTTPException(status_code=409, detail="镜头宫格正在处理中")

    if (req.rows, req.cols) != (3, 3):
        raise HTTPException(status_code=400, detail="单分镜宫格必须使用 3×3 布局")
    if len(req.cells) != 9:
        raise HTTPException(status_code=400, detail="3×3 单分镜宫格必须提供 9 个镜头格")
    by_index = {cell.index: cell for cell in req.cells}
    if len(by_index) != 9 or set(by_index) != set(range(9)):
        raise HTTPException(status_code=400, detail="镜头格索引必须完整覆盖 0 到 8")
    grid.rows, grid.cols = req.rows, req.cols
    grid.cells = [
        ShotGridCell(index=index, prompt=by_index[index].prompt if index in by_index else "")
        for index in range(req.rows * req.cols)
    ]
    grid.status = "draft"
    grid.error_message = None
    grid.updated_at = datetime.now(UTC).isoformat()
    manager.save(grid)
    return grid.to_dict()


@router.post("/generate")
async def generate_shot_grid(project_name: str, script_file: str, shot_id: str, user: CurrentUser):
    project_manager = get_project_manager()
    project_path = project_manager.get_project_path(project_name)
    manager = ShotGridManager(project_path)
    grid = manager.find(script_file=script_file, shot_id=shot_id)
    if grid is None:
        raise HTTPException(status_code=404, detail="请先保存镜头规划")
    if not any(cell.prompt.strip() for cell in grid.cells):
        raise HTTPException(status_code=400, detail="至少填写一个镜头描述")
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
    grid.status = "generating"
    grid.task_id = task["task_id"]
    grid.error_message = None
    grid.updated_at = datetime.now(UTC).isoformat()
    manager.save(grid)
    return {"task_id": task["task_id"], "status": task["status"]}


@router.get("/tasks/{task_id}")
async def get_shot_grid_task(task_id: str):
    task = await get_generation_queue().get_task(task_id)
    if task is None or task.get("task_type") != "shot_grid":
        raise HTTPException(status_code=404, detail="镜头宫格任务不存在")
    return task


@router.post("/{grid_id}/confirm")
def confirm_shot_grid(project_name: str, grid_id: str):
    project_manager = get_project_manager()
    project_path = project_manager.get_project_path(project_name)
    manager = ShotGridManager(project_path)
    grid = manager.get(grid_id)
    if grid is None:
        raise HTTPException(status_code=404, detail="镜头宫格不存在")
    if grid.status != "completed" or not grid.composite_path:
        raise HTTPException(status_code=409, detail="联合图尚未生成完成")

    composite = safe_join(project_path, grid.composite_path)
    try:
        with Image.open(composite) as image:
            image.load()
            width, height = image.size
            if width < grid.cols or height < grid.rows:
                raise ValueError("联合图尺寸不足，无法切分")
            cells_dir = safe_join(project_path, f"shot_grids/{grid.id}/cells")
            cells_dir.mkdir(parents=True, exist_ok=True)
            paths = []
            for cell in grid.cells:
                if not cell.prompt.strip():
                    paths.append(None)
                    continue
                row, col = divmod(cell.index, grid.cols)
                left = round(col * width / grid.cols)
                top = round(row * height / grid.rows)
                right = round((col + 1) * width / grid.cols)
                bottom = round((row + 1) * height / grid.rows)
                output = cells_dir / f"{cell.index}.png"
                image.crop((left, top, right, bottom)).save(output, format="PNG")
                paths.append(output.relative_to(project_path).as_posix())
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"联合图切分失败：{exc}") from exc

    script = project_manager.load_script(project_name, grid.script_file)
    items, id_field, _, _, _ = get_storyboard_items(script)
    shot = next((item for item in items if str(item.get(id_field, "")) == grid.shot_id), None)
    if shot is None:
        raise HTTPException(status_code=404, detail="剧本中找不到对应分镜")
    assets = shot.setdefault("generated_assets", {})
    existing = assets.get("storyboard_image", {})
    if not isinstance(existing, dict):
        existing = {}
    if not paths[0] or not paths[8]:
        raise HTTPException(status_code=422, detail="第 1 格和第 9 格必须有可用图像，才能作为视频首尾参考")
    cell_paths = paths
    existing["grid_images"] = [path for path in paths if path]
    assets["storyboard_image"] = paths[0]
    assets["shot_grid"] = {
        "grid_id": grid.id,
        "layout": "3x3",
        "composite_path": grid.composite_path,
        "cell_paths": cell_paths,
        "start_cell": 0,
        "key_cell": 4,
        "end_cell": 8,
        "video_reference_mode": "start_end_cells",
        "confirmed_at": datetime.now(UTC).isoformat(),
    }
    project_manager.save_script(project_name, grid.script_file, script)

    grid.status = "confirmed"
    grid.updated_at = datetime.now(UTC).isoformat()
    for cell, path in zip(grid.cells, paths, strict=True):
        cell.confirmed = bool(path)
        cell.image_path = path
    manager.save(grid)
    return {"success": True, "grid": grid.to_dict()}
