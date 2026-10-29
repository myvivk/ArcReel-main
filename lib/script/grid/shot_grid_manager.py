"""File-backed CRUD for per-shot grid plans."""

from __future__ import annotations

import json
import re
from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import portalocker

from lib.infra.json_io import atomic_write_json
from lib.infra.path_safety import safe_join
from lib.script.grid.shot_models import ShotGrid

_ID_RE = re.compile(r"shot_grid_[0-9a-f]{12}")


def select_shot_grids(
    grids: Iterable[ShotGrid], *, items: Sequence[Any] = (), id_field: str = "scene_id"
) -> dict[str, ShotGrid]:
    """Prefer the bound grid; otherwise choose confirmed, completed, then latest."""
    chosen: dict[str, ShotGrid] = {}
    bindings: dict[str, str] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        assets = item.get("generated_assets")
        binding = assets.get("shot_grid") if isinstance(assets, Mapping) else None
        grid_id = binding.get("grid_id") if isinstance(binding, Mapping) else None
        if isinstance(grid_id, str) and grid_id:
            bindings[str(item.get(id_field))] = grid_id
    priority = {"confirmed": 2, "completed": 1}
    for grid in grids:
        bound = bindings.get(grid.shot_id)
        if bound is not None and grid.id != bound:
            continue
        current = chosen.get(grid.shot_id)
        if current is None or (priority.get(grid.status, 0), grid.updated_at, grid.id) > (
            priority.get(current.status, 0),
            current.updated_at,
            current.id,
        ):
            chosen[grid.shot_id] = grid
    return chosen


class ShotGridManager:
    def __init__(self, project_path: Path):
        self._dir = Path(project_path) / "shot_grids"

    def _path(self, grid_id: str) -> Path:
        if _ID_RE.fullmatch(grid_id) is None:
            raise ValueError(f"非法镜头宫格 ID: {grid_id!r}")
        return safe_join(self._dir, f"{grid_id}.json")

    def save(self, grid: ShotGrid) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(grid.id)
        with portalocker.Lock(path.with_name(f".{path.name}.lock"), flags=portalocker.LOCK_EX):
            atomic_write_json(path, grid.to_dict())

    def get(self, grid_id: str) -> ShotGrid | None:
        path = self._path(grid_id)
        if not path.exists():
            return None
        # 写入以原子替换提交；读取完整快照不再加锁，提交回调也能读取当前宫格身份。
        return ShotGrid.from_dict(json.loads(path.read_text(encoding="utf-8")))

    @contextmanager
    def locked_records(self, grid_ids: Iterable[str]) -> Generator[dict[str, ShotGrid | None]]:
        """按固定顺序锁住宫格，供宫格 → 剧本 → 项目顺序的提交事务复核输入。"""
        self._dir.mkdir(parents=True, exist_ok=True)
        with ExitStack() as stack:
            ids = sorted(set(grid_ids))
            for grid_id in ids:
                path = self._path(grid_id)
                stack.enter_context(portalocker.Lock(path.with_name(f".{path.name}.lock"), flags=portalocker.LOCK_EX))
            yield {grid_id: self.get(grid_id) for grid_id in ids}

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

    def find(
        self,
        *,
        script_file: str,
        shot_id: str,
        items: Sequence[Any] = (),
        id_field: str = "scene_id",
    ) -> ShotGrid | None:
        return select_shot_grids(
            [grid for grid in self.list_all() if grid.script_file == script_file and grid.shot_id == shot_id],
            items=items,
            id_field=id_field,
        ).get(shot_id)

    def list_all(self) -> list[ShotGrid]:
        """目录下全部宫格记录；单条读坏只跳过它，不因一条坏记录拖垮整行判定。"""

        grids: list[ShotGrid] = []
        for path in sorted(self._dir.glob("shot_grid_*.json")):
            try:
                grids.append(ShotGrid.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
        return grids
