"""File-backed CRUD for per-shot grid plans."""

from __future__ import annotations

import json
import re
from pathlib import Path

import portalocker

from lib.infra.json_io import atomic_write_json
from lib.infra.path_safety import safe_join
from lib.script.grid.shot_models import ShotGrid

_ID_RE = re.compile(r"shot_grid_[0-9a-f]{12}")


class ShotGridManager:
    def __init__(self, project_path: Path):
        self._dir = Path(project_path) / "shot_grids"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, grid_id: str) -> Path:
        if _ID_RE.fullmatch(grid_id) is None:
            raise ValueError(f"非法镜头宫格 ID: {grid_id!r}")
        return safe_join(self._dir, f"{grid_id}.json")

    def save(self, grid: ShotGrid) -> None:
        path = self._path(grid.id)
        with portalocker.Lock(path.with_name(f".{path.name}.lock"), flags=portalocker.LOCK_EX):
            atomic_write_json(path, grid.to_dict())

    def get(self, grid_id: str) -> ShotGrid | None:
        path = self._path(grid_id)
        if not path.exists():
            return None
        # 写入以原子替换提交；读取完整快照不再加锁，提交回调也能读取当前宫格身份。
        return ShotGrid.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def update_formal(self, grid_id: str, mutate, *, on_commit=None) -> ShotGrid | None:
        path = self._path(grid_id)
        if not path.exists():
            return None
        with portalocker.Lock(path.with_name(f".{path.name}.lock"), flags=portalocker.LOCK_EX):
            previous = json.loads(path.read_text(encoding="utf-8"))
            grid = ShotGrid.from_dict(previous)
            mutate(grid)
            atomic_write_json(path, grid.to_dict())
            try:
                if on_commit is not None:
                    on_commit()
            except BaseException:
                atomic_write_json(path, previous)
                raise
            return grid

    def find(self, *, script_file: str, shot_id: str) -> ShotGrid | None:
        for path in self._dir.glob("shot_grid_*.json"):
            try:
                grid = ShotGrid.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if grid.script_file == script_file and grid.shot_id == shot_id:
                return grid
        return None

    def list_all(self) -> list[ShotGrid]:
        """目录下全部宫格记录；单条读坏只跳过它，不因一条坏记录拖垮整行判定。"""

        grids: list[ShotGrid] = []
        for path in sorted(self._dir.glob("shot_grid_*.json")):
            try:
                grids.append(ShotGrid.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
        return grids
