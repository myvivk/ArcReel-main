"""Storyboard video text, ordered audio and durable frame input projection."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from lib.infra.content_digest import sha256_file
from lib.infra.path_safety import safe_join
from lib.project.asset_types import ASSET_SPECS, asset_name_comparison_key
from lib.prompts.prompt_utils import utterances_to_dialogue
from lib.prompts.storyboard_video_projection import project_storyboard_prompt
from lib.script.grid.keyframe_parser import shot_scene_text
from lib.script.grid.shot_models import ShotGrid
from lib.script.storyboard_sequence import (
    StoryboardVideoAssetImage,
    StoryboardVideoSlots,
    find_storyboard_item,
    resolve_storyboard_image_ref,
)
from lib.speech.character_voice import character_voice_binding
from lib.speech.reference_audio import resolve_reference_audio_paths
from lib.speech.voice_settings import VoiceRenderSettings


def resolve_storyboard_video_prompt(item: Mapping[str, Any], spec: object = None) -> object:
    """交给 ``render_storyboard_video_prompt`` 的文本输入。

    - 通用口径：分镜提示词字符串，逐字渲染；
    - 目标模型有规范：分镜提示词投影成 ``action`` / ``shot_cameras``，让方言按自己的
      镜头分段、时间码与逐镜运镜渲染，而不是把「【镜头N｜景别】0-3秒」原文直接塞给它。
    """

    scene = shot_scene_text(item)
    if not scene.strip():
        raise ValueError("分镜缺少 image_prompt（分镜提示词）")
    if spec is None or getattr(spec, "is_generic", True):
        return scene
    return project_storyboard_prompt(item, duration_seconds=item.get("duration_seconds"))


def storyboard_asset_links(project: Mapping[str, Any], item: Mapping[str, Any], project_name: str) -> str:
    """Link referenced identities to their entries in the project's asset table."""
    links: list[str] = []
    for spec in ASSET_SPECS.values():
        bucket = project.get(spec.bucket_key)
        if not isinstance(bucket, Mapping):
            continue
        names = dict.fromkeys(
            name for field in spec.reference_list_fields for name in (item.get(field) or []) if isinstance(name, str)
        )
        for name in names:
            entry = bucket.get(name)
            if not isinstance(entry, Mapping):
                continue
            pointer = name.replace("~", "~0").replace("/", "~1")
            link = f"/api/v1/projects/{quote(project_name, safe='')}#/{spec.bucket_key}/{quote(pointer, safe='')}"
            label = name.replace("[", r"\[").replace("]", r"\]")
            description = str(entry.get("description") or "").strip()
            links.append(f"- [{label}]({link})" + (f": {description}" if description else ""))
    return "Asset references:\n" + "\n".join(links) if links else ""


def storyboard_video_asset_trim_warning(
    dropped_assets: Sequence[tuple[str, str, Path]],
    *,
    limit: int,
    frame_count: int,
) -> dict[str, Any] | None:
    """资产图因参考图上限被裁时返回 ``{key, params}`` warning；未裁返回 ``None``。

    与任务 ``result.warnings``、提示词预览的 ``RenderedPrompt.warnings`` 同形，读侧按
    ``storyboard_video_assets_trimmed`` 渲染；K 帧不参与裁剪，``frame_count`` 只用于把
    “上限里有多少是 K 帧”说清楚。
    """

    if not dropped_assets:
        return None
    return {
        "key": "storyboard_video_assets_trimmed",
        "params": {
            "count": len(dropped_assets),
            "names": "、".join(name for _kind, name, _path in dropped_assets),
            "limit": limit,
            "frame_count": frame_count,
        },
    }


def storyboard_asset_image_evidence(
    asset_images: Sequence[StoryboardVideoAssetImage],
) -> list[dict[str, object]]:
    """Hash the ordered asset images actually attached before the K frames."""

    return [
        {
            "role": "reference_image",
            "sha256": sha256_file(path),
            "resource_id": f"{kind}:{name}",
            "kind": "asset_sheet",
            "keyframe_index": None,
        }
        for kind, name, path in asset_images
    ]


def storyboard_frame_evidence(
    slots: StoryboardVideoSlots,
    *,
    project_path: Path | None = None,
    items: Sequence[Any] = (),
    id_field: str = "scene_id",
) -> list[dict[str, object]]:
    """Hash every ordered source, including the video from which continuity is extracted."""
    evidence: list[dict[str, object]] = []
    for role, frame in slots.frames:
        entry: dict[str, object] = {
            "role": role,
            "sha256": sha256_file(frame.path),
            "resource_id": frame.resource_id,
            "kind": frame.kind,
            "keyframe_index": frame.keyframe_index,
        }
        source = find_storyboard_item(items, id_field, frame.resource_id)
        if project_path is not None and source is not None and frame.kind == "grid_cell":
            assets = source[0].get("generated_assets")
            binding = assets.get("shot_grid") if isinstance(assets, Mapping) else None
            if isinstance(binding, Mapping) and binding.get("grid_id"):
                composite = resolve_storyboard_image_ref(project_path, binding.get("composite_path"))
                if composite is None:
                    raise ValueError("confirmed keyframe has no contact sheet")
                rows, cols = binding.get("rows"), binding.get("cols")
                if type(rows) is not int or type(cols) is not int or rows < 1 or cols < 1:
                    # 旧宫格绑定只写了 ``layout``（如 ``2x2``），没有 rows/cols；按布局反推，
                    # 避免存量项目一生成视频就报「invalid grid dimensions」。
                    layout = binding.get("layout")
                    match = re.fullmatch(r"(\d+)\s*[x×]\s*(\d+)", layout.strip()) if isinstance(layout, str) else None
                    if match is None:
                        raise ValueError("confirmed keyframe has invalid grid dimensions")
                    rows, cols = int(match.group(1)), int(match.group(2))
                entry["grid"] = {
                    "id": binding["grid_id"],
                    "rows": rows,
                    "cols": cols,
                    "composite_sha256": sha256_file(composite),
                }
        evidence.append(entry)
    return evidence


@dataclass(frozen=True, slots=True)
class StoryboardAudioInputs:
    speakers: tuple[str, ...] = ()
    paths: tuple[Path, ...] = ()
    targets: tuple[int, ...] | None = None


def resolve_storyboard_audio_inputs(
    *,
    project: dict[str, Any],
    project_path: Path,
    item: Mapping[str, Any],
    slots: StoryboardVideoSlots,
    voice: VoiceRenderSettings,
) -> StoryboardAudioInputs:
    """Keep utterance first-appearance order and bind only declared grid speakers."""
    if voice.is_silent or character_voice_binding(project) != "reference_audio":
        return StoryboardAudioInputs()
    audio = resolve_reference_audio_paths(project, project_path)
    speakers = tuple(
        dict.fromkeys(
            asset_name_comparison_key(entry["speaker"])
            for entry in utterances_to_dialogue(item.get("utterances"))
            if asset_name_comparison_key(entry["speaker"]) in audio
        )
    )
    if not speakers:
        return StoryboardAudioInputs()
    targets: tuple[int, ...] | None = None
    if voice.requires_reference_image:
        assets = item.get("generated_assets")
        grid_binding = assets.get("shot_grid") if isinstance(assets, Mapping) else None
        grid_id = grid_binding.get("grid_id") if isinstance(grid_binding, Mapping) else None
        grid = None
        if isinstance(grid_id, str) and re.fullmatch(r"shot_grid_[0-9a-f]{12}", grid_id):
            grid_path = safe_join(project_path, f"shot_grids/{grid_id}.json")
            if grid_path.is_file():
                grid = ShotGrid.from_dict(json.loads(grid_path.read_text(encoding="utf-8")))
        paired: list[int] = []
        for speaker in speakers:
            target = next(
                (
                    index
                    for index, frame in enumerate(slots.references)
                    if grid is not None
                    and frame.keyframe_index is not None
                    and frame.keyframe_index < len(grid.cells)
                    and f"@[{speaker}]" in grid.cells[frame.keyframe_index].prompt
                ),
                None,
            )
            if target is None:
                raise ValueError(f"reference audio speaker has no declared keyframe target: {speaker}")
            paired.append(target)
        targets = tuple(paired)
    return StoryboardAudioInputs(speakers, tuple(audio[speaker] for speaker in speakers), targets)
