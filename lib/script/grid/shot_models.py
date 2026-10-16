"""Per-shot multi-frame grid planning records."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class ShotGridCell:
    index: int
    prompt: str = ""
    time_ratio: float = 0.0
    continuity_notes: str = ""
    image_path: str | None = None
    confirmed: bool = False

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "prompt": self.prompt,
            "time_ratio": self.time_ratio,
            "continuity_notes": self.continuity_notes,
            "image_path": self.image_path,
            "confirmed": self.confirmed,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ShotGridCell:
        return cls(
            index=int(data["index"]),
            prompt=str(data.get("prompt", "")),
            time_ratio=float(data.get("time_ratio", 0.0)),
            continuity_notes=str(data.get("continuity_notes", "")),
            image_path=data.get("image_path"),
            confirmed=bool(data.get("confirmed", False)),
        )


@dataclass
class ShotGrid:
    id: str
    episode: int
    script_file: str
    shot_id: str
    rows: int
    cols: int
    cells: list[ShotGridCell] = field(default_factory=list)
    composite_path: str | None = None
    status: str = "draft"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    error_message: str | None = None
    task_id: str | None = None
    image_basis: dict[str, object] | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "episode": self.episode,
            "script_file": self.script_file,
            "shot_id": self.shot_id,
            "rows": self.rows,
            "cols": self.cols,
            "cells": [cell.to_dict() for cell in self.cells],
            "composite_path": self.composite_path,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error_message": self.error_message,
            "task_id": self.task_id,
            "image_basis": self.image_basis,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ShotGrid:
        return cls(
            id=str(data["id"]),
            episode=int(data["episode"]),
            script_file=str(data["script_file"]),
            shot_id=str(data["shot_id"]),
            rows=int(data.get("rows", 3)),
            cols=int(data.get("cols", 3)),
            cells=[ShotGridCell.from_dict(cell) for cell in data.get("cells", [])],
            composite_path=data.get("composite_path"),
            status=str(data.get("status", "draft")),
            created_at=str(data.get("created_at", datetime.now(UTC).isoformat())),
            updated_at=str(data.get("updated_at", datetime.now(UTC).isoformat())),
            error_message=data.get("error_message"),
            task_id=data.get("task_id"),
            image_basis=data.get("image_basis"),
        )

    @classmethod
    def create(cls, *, episode: int, script_file: str, shot_id: str, rows: int, cols: int) -> ShotGrid:
        now = datetime.now(UTC).isoformat()
        return cls(
            id=f"shot_grid_{uuid.uuid4().hex[:12]}",
            episode=episode,
            script_file=script_file,
            shot_id=shot_id,
            rows=rows,
            cols=cols,
            cells=[ShotGridCell(index=index) for index in range(rows * cols)],
            created_at=now,
            updated_at=now,
        )
