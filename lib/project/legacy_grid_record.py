"""Read historical joint-grid records for migration and media attribution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass
class ReferenceImage:
    """Metadata for a reference image used during grid generation."""

    path: str  # Relative path from project root (e.g. "characters/hero/sheet.png")
    name: str  # Display name
    ref_type: str  # "character" | "scene" | "prop"

    def to_dict(self) -> dict:
        return {"path": self.path, "name": self.name, "ref_type": self.ref_type}

    @classmethod
    def from_dict(cls, data: dict) -> ReferenceImage:
        return cls(
            path=data["path"],
            name=data["name"],
            ref_type=data.get("ref_type", "character"),
        )


@dataclass
class FrameCell:
    """Represents a single cell in a grid frame chain."""

    index: int
    row: int
    col: int
    frame_type: Literal["first", "transition", "placeholder"]
    prev_scene_id: str | None = None
    next_scene_id: str | None = None
    image_path: str | None = None

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "row": self.row,
            "col": self.col,
            "frame_type": self.frame_type,
            "prev_scene_id": self.prev_scene_id,
            "next_scene_id": self.next_scene_id,
            "image_path": self.image_path,
        }

    @classmethod
    def from_dict(cls, data: dict) -> FrameCell:
        return cls(
            index=data["index"],
            row=data["row"],
            col=data["col"],
            frame_type=data["frame_type"],
            prev_scene_id=data.get("prev_scene_id"),
            next_scene_id=data.get("next_scene_id"),
            image_path=data.get("image_path"),
        )


@dataclass
class LegacyGridRecord:
    """Represents a grid image generation job."""

    id: str
    episode: int
    script_file: str
    scene_ids: list[str]
    grid_image_path: str | None
    rows: int
    cols: int
    cell_count: int
    frame_chain: list[FrameCell]
    # 联合图生命周期：completed 仅表示联合图就绪，是否已切分落格由 split_at 表达
    status: Literal["pending", "generating", "completed", "failed"]
    prompt: str | None
    provider: str
    model: str
    grid_size: str
    created_at: str
    error_message: str | None = None
    reference_images: list[ReferenceImage] | None = None
    # 最近一次按当前联合图切分落格的时间；联合图内容变更（重新生成/上传/版本还原）时清空
    split_at: str | None = None
    # 产出当前联合图时的单格目标比例。项目 aspect_ratio 可随时改，而切分与产出已解耦、
    # 可以隔很久才执行，读当时的项目设置会把历史联合图按新比例中心裁切（横版按竖版切会
    # 丢掉大半画面宽度），故随联合图一起冻结在记录上。存量记录为 None，切分时回退到项目设置。
    video_aspect_ratio: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "episode": self.episode,
            "script_file": self.script_file,
            "scene_ids": self.scene_ids,
            "grid_image_path": self.grid_image_path,
            "rows": self.rows,
            "cols": self.cols,
            "cell_count": self.cell_count,
            "frame_chain": [c.to_dict() for c in self.frame_chain],
            "status": self.status,
            "prompt": self.prompt,
            "provider": self.provider,
            "model": self.model,
            "grid_size": self.grid_size,
            "created_at": self.created_at,
            "error_message": self.error_message,
            "reference_images": [r.to_dict() for r in self.reference_images] if self.reference_images else None,
            "split_at": self.split_at,
            "video_aspect_ratio": self.video_aspect_ratio,
        }

    @classmethod
    def from_dict(cls, data: dict) -> LegacyGridRecord:
        # 生成与切分曾是同一任务，中间态记录可能残留 status="splitting"；
        # 该态下联合图已落盘，按当前模型等价于「联合图就绪、未落格」。
        raw_status = data["status"]
        status = "completed" if raw_status == "splitting" else raw_status
        # 旧记录没有 split_at 字段。旧流程下 status="completed" 只在切格落盘之后才写入，
        # 因此这类记录等价于「已切分」，用 created_at 充当落格时间；若一律读成未切分，
        # 前端会提示待切分，用户照做就会用旧联合图覆盖之后单独重生成过的分镜图。
        # 显式为 null 的新记录、以及 status="splitting"（联合图已就绪但未落格）保持未切分。
        split_at = data["created_at"] if "split_at" not in data and raw_status == "completed" else data.get("split_at")
        return cls(
            id=data["id"],
            episode=data["episode"],
            script_file=data["script_file"],
            scene_ids=data["scene_ids"],
            grid_image_path=data.get("grid_image_path"),
            rows=data["rows"],
            cols=data["cols"],
            cell_count=data["cell_count"],
            frame_chain=[FrameCell.from_dict(c) for c in data.get("frame_chain", [])],
            status=status,
            prompt=data.get("prompt"),
            provider=data["provider"],
            model=data["model"],
            grid_size=data["grid_size"],
            created_at=data["created_at"],
            error_message=data.get("error_message"),
            reference_images=[ReferenceImage.from_dict(r) for r in data["reference_images"]]
            if data.get("reference_images")
            else None,
            split_at=split_at,
            video_aspect_ratio=data.get("video_aspect_ratio"),
        )
