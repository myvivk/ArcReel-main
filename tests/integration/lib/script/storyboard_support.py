"""提示词生成测试使用的已确认宫格及真实关键帧文件。"""

from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from lib.script.grid.shot_grid_manager import ShotGridManager
from lib.script.grid.shot_models import ShotGrid


def confirmed_grid(project_path: Path, shot_id: str, *, rows: int = 2, cols: int = 2) -> ShotGrid:
    grid = ShotGrid.create(episode=1, script_file="episode_1.json", shot_id=shot_id, rows=rows, cols=cols)
    directory = project_path / "storyboards"
    directory.mkdir(exist_ok=True)
    grid.status = "confirmed"
    grid.confirmed_at = datetime.now(UTC).isoformat()
    grid.composite_path = f"storyboards/{shot_id}-grid.png"
    Image.new("RGB", (cols * 8, rows * 8), "white").save(project_path / grid.composite_path)
    for cell in grid.cells:
        cell.prompt = f"{shot_id} 关键帧{cell.index}的动作姿态"
        cell.continuity_notes = "中景"
        cell.confirmed = True
        cell.image_path = f"storyboards/{shot_id}-K{cell.index}.png"
        Image.new("RGB", (8, 8), "white").save(project_path / cell.image_path)
    ShotGridManager(project_path).save(grid)
    return grid
