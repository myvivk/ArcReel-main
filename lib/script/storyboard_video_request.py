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
from lib.script.grid.shot_models import ShotGrid
from lib.script.storyboard_sequence import StoryboardVideoSlots, find_storyboard_item, resolve_storyboard_image_ref
from lib.speech.character_voice import character_voice_binding
from lib.speech.reference_audio import resolve_reference_audio_paths
from lib.speech.voice_settings import VoiceRenderSettings


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
                    raise ValueError("confirmed keyframe has invalid grid dimensions")
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
