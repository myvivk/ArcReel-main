"""提示词生成测试使用的已确认宫格及真实关键帧文件。"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image

from lib.artifacts.artifact_activation import activate_artifact_target_state
from lib.project.project_manager import ProjectManager
from lib.project.project_schema import CURRENT_PROJECT_SCHEMA_VERSION
from lib.script.grid.shot_grid_manager import ShotGridManager
from lib.script.grid.shot_models import ShotGrid
from lib.script.short_drama_source import replace_short_drama_source


def factory_prompt_project(tmp_path: Path, *, factory: bool = True, project_name: str = "demo") -> Path:
    directory = tmp_path / "projects" / project_name
    directory.mkdir(parents=True)
    payload = {
        "schema_version": CURRENT_PROJECT_SCHEMA_VERSION,
        "title": "视频测试",
        "content_mode": "drama",
        "generation_mode": "storyboard",
        "characters": {},
        "scenes": {},
        "props": {},
        "episodes": [{"episode": 1, "script_file": "scripts/episode_1.json"}],
    }
    (directory / "project.json").write_text(json.dumps(payload), encoding="utf-8")
    scripts = directory / "scripts"
    scripts.mkdir()
    script: dict[str, Any] = {
        "episode": 1,
        "title": "视频测试",
        "content_mode": "drama",
        "scenes": [
            {
                "scene_id": f"E1S{index:02d}",
                "duration_seconds": 12,
                "segment_break": False,
                "characters_in_scene": [],
                "scenes": [],
                "props": [],
                "utterances": [],
                "source_text": "男人推门，走到窗边。",
                "scene_description": "推门进屋",
                "image_prompt": f"【镜头1】男人推门。 【镜头2】走到窗边。分镜{index}",
                "video_prompt": None,
            }
            for index in (1, 2)
        ],
    }
    source = directory / "source"
    source.mkdir()
    (source / "episode_1.txt").write_text("男人推门，走到窗边。", encoding="utf-8")
    if factory:
        saved = replace_short_drama_source(
            ProjectManager.for_project_dir(directory), directory.name, 1, "# 第1集\n男人推门，走到窗边。\n"
        )
        script["short_drama_meta"] = {"source_revision": saved.revision}
    (scripts / "episode_1.json").write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    activate_artifact_target_state(directory, bump_schema=False)
    return directory


def read_factory_script(directory: Path) -> dict[str, Any]:
    return json.loads((directory / "scripts/episode_1.json").read_text(encoding="utf-8"))


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
