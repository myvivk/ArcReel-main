"""Read and delete historical joint-grid records."""

import json
import logging
import re
from contextlib import contextmanager
from pathlib import Path

import portalocker

from lib.artifacts.formal_write import formal_write_transaction, project_metadata_lock
from lib.infra.path_safety import safe_join
from lib.project.legacy_grid_record import LegacyGridRecord
from lib.project.project_activity import record_project_activity

logger = logging.getLogger(__name__)

# 与 lib/script/grid/models.py::LegacyGridRecord.create 的生成格式一致
_GRID_ID_RE = re.compile(r"grid_[0-9a-f]{12}")


class LegacyGridStore:
    """File-based CRUD for LegacyGridRecord records, stored in {project}/grids/."""

    def __init__(self, project_path: Path):
        self._project_dir = Path(project_path)
        self._dir = self._project_dir / "grids"

    def _path(self, grid_id: str, suffix: str = ".json") -> Path:
        """grids/ 下的记录路径。grid_id 来自 URL 路径参数，先卡格式白名单再过越界校验。

        用 ``fullmatch`` 而非 ``match()`` + ``$``：``$`` 会匹配字符串末尾换行符之前的
        位置，``grid_xxxxxxxxxxxx\\n`` 这类带尾随换行的输入能骗过 ``match()``，让换行符
        混入最终文件名。
        """
        if _GRID_ID_RE.fullmatch(grid_id) is None:
            raise ValueError(f"非法宫格 ID: {grid_id!r}")
        return safe_join(self._dir, f"{grid_id}{suffix}")

    def image_path(self, grid_id: str) -> Path:
        """grids/ 下的联合图路径。调用方一律经此取路径，不自行拼接，
        否则 ID 白名单与越界校验会被绕过。"""
        return self._path(grid_id, ".png")

    def get(self, grid_id: str) -> LegacyGridRecord | None:
        """Read and return a LegacyGridRecord by id, or None if not found."""
        path = self._path(grid_id)
        if not path.exists():
            return None
        with self._record_lock(path):
            return self._get_unlocked(path)

    @staticmethod
    def _get_unlocked(path: Path) -> LegacyGridRecord | None:
        if not path.exists():
            return None
        return LegacyGridRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))

    @staticmethod
    @contextmanager
    def _record_lock(path: Path):
        lock_path = path.parent / f".{path.name}.lock"
        lock_path.touch(exist_ok=True)
        with portalocker.Lock(lock_path, flags=portalocker.LOCK_EX):
            yield

    def delete(self, grid_id: str) -> bool:
        """Delete one grid record, image, and active typed claim atomically."""
        path = self._path(grid_id)
        if not path.exists():
            return False
        with project_metadata_lock(self._project_dir), self._record_lock(path):
            if not path.exists():
                return False
            image_file = self.image_path(grid_id)
            try:
                grid = self._get_unlocked(path)
            except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError):
                # Invalid legacy records were deletable before Manifest activation.
                # They cannot carry a provable typed claim, so preserve that cleanup
                # behavior while valid records take the guarded claim path below.
                grid = None
            with formal_write_transaction(path, image_file):
                image_file.unlink(missing_ok=True)
                path.unlink()
                if grid is not None:
                    from lib.artifacts.artifact_activation import register_artifact_entries_atomically
                    from lib.artifacts.artifact_manifest import ArtifactKey

                    register_artifact_entries_atomically(
                        self._project_dir,
                        {ArtifactKey.episode_grid(grid.episode, grid.id): None},
                    )
        record_project_activity(self._project_dir)
        return True

    def list_all(self) -> list[LegacyGridRecord]:
        """Return all grids sorted by created_at ascending."""
        grids = []
        for p in self._dir.glob("grid_*.json"):
            try:
                grids.append(LegacyGridRecord.from_dict(json.loads(p.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Skipping invalid grid file %s: %s", p.name, e)
        return sorted(grids, key=lambda g: g.created_at)
