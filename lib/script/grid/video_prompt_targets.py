"""视频提示词的目标集合、宫格指纹与时效（D-18 / D-20）。

视频提示词正文只存正式脚本（D-12），这里不落任何文本，只回答三个结构问题：

- 哪些分镜「宫格已确认、视频提示词还空着」——整集批量补缺的目标；
- 哪些分镜「宫格还没确认」——不进本批，只在行上提示报数；
- 哪些分镜「提示词是按旧宫格写的」——标 stale，不阻断视频生成，重写只单个走详情。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lib.script.grid.shot_models import ShotGrid

#: 宫格指纹长度：与 scene 指纹同一口径，只留 12 位十六进制。
CELLS_REVISION_LENGTH = 12

_BASIS_FIELD = "video_prompt_basis"


def grid_cells_revision(grid: ShotGrid) -> str:
    """宫格逐格留证的指纹：``序号|景别|正文`` 逐行拼接后取 SHA-256 前 12 位。"""

    payload = "\n".join(f"{cell.index}|{cell.continuity_notes}|{cell.prompt}" for cell in grid.cells)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:CELLS_REVISION_LENGTH]


def video_prompt_text(item: Mapping[str, Any]) -> str:
    """条目上的视频提示词正文：结构化取 ``action``，文本形态取原串（D-16）。"""

    value = item.get("video_prompt")
    if isinstance(value, Mapping):
        action = value.get("action", "")
        return action.strip() if isinstance(action, str) else ""
    return value.strip() if isinstance(value, str) else ""


def video_prompt_basis(item: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """写这条视频提示词时所用宫格的指纹；缺失表示手工 / 存量，一律按 current 读。"""

    assets = item.get("generated_assets")
    if not isinstance(assets, Mapping):
        return None
    basis = assets.get(_BASIS_FIELD)
    return basis if isinstance(basis, Mapping) else None


def video_prompt_basis_for(grid: ShotGrid, *, written_at: str | None = None) -> dict[str, Any]:
    """写回时记下的依据：哪一次宫格、几格、逐格指纹是什么（D-18）。"""

    return {
        "grid_id": grid.id,
        "layout": f"{grid.rows}x{grid.cols}",
        "cells_revision": grid_cells_revision(grid),
        "written_at": written_at or datetime.now(UTC).isoformat(),
    }


def is_video_prompt_stale(item: Mapping[str, Any], grid: ShotGrid | None) -> bool:
    """提示词是否按「不是当前这张宫格」写的。

    - ``basis`` 缺失：手工 / 存量，**不算 stale**（不误报）；
    - 宫格缺失或未确认：同样不判 stale——那属于「还没到写这一步」，不是「文本过期」。
    """

    basis = video_prompt_basis(item)
    if basis is None or grid is None:
        return False
    if str(basis.get("grid_id") or "") != grid.id:
        return True
    return str(basis.get("cells_revision") or "") != grid_cells_revision(grid)


@dataclass(frozen=True, slots=True)
class VideoPromptTargets:
    """本集视频提示词的三种状态，按剧本顺序。"""

    #: 本集全部分镜，按剧本顺序。
    unit_ids: tuple[str, ...]
    #: 宫格已确认且视频提示词为空：整集批量补缺的目标。
    targets: tuple[str, ...]
    #: 宫格还没确认：不进本批，行上按它报数。
    awaiting_grid: tuple[str, ...]
    #: 提示词按旧宫格写的：标 stale，不阻断视频生成，重写只单个走详情。
    stale: tuple[str, ...]


def plan_video_prompt_targets(
    items: Sequence[Mapping[str, Any]],
    *,
    id_field: str,
    grids: Mapping[str, ShotGrid],
) -> VideoPromptTargets:
    """纯结构判定：不读文件、不落盘、不碰请求期的计费。

    Args:
        items: 本集分镜条目，按剧本顺序。
        id_field: 分镜 id 字段名（drama 是 ``scene_id``，narration 是 ``segment_id``）。
        grids: 本集已建出的宫格记录，按分镜 id 索引。
    """

    unit_ids: list[str] = []
    targets: list[str] = []
    awaiting: list[str] = []
    stale: list[str] = []
    for item in items:
        raw_id = item.get(id_field)
        if not raw_id:
            continue
        unit_id = str(raw_id)
        unit_ids.append(unit_id)
        grid = grids.get(unit_id)
        if grid is None or grid.status != "confirmed":
            awaiting.append(unit_id)
            continue
        if not video_prompt_text(item):
            targets.append(unit_id)
        elif is_video_prompt_stale(item, grid):
            stale.append(unit_id)
    return VideoPromptTargets(
        unit_ids=tuple(unit_ids),
        targets=tuple(targets),
        awaiting_grid=tuple(awaiting),
        stale=tuple(stale),
    )


__all__ = [
    "CELLS_REVISION_LENGTH",
    "VideoPromptTargets",
    "grid_cells_revision",
    "is_video_prompt_stale",
    "plan_video_prompt_targets",
    "video_prompt_basis",
    "video_prompt_basis_for",
    "video_prompt_text",
]
