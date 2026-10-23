import json
from pathlib import Path

import pytest

from lib.artifacts.visual_artifact_provenance import build_storyboard_video_artifact_visual_basis
from lib.prompts.prompt_utils import render_storyboard_video_prompt
from lib.script.grid.shot_models import ShotGrid
from lib.script.storyboard_sequence import resolve_storyboard_video_slots
from lib.script.storyboard_video_request import (
    resolve_storyboard_audio_inputs,
    storyboard_asset_links,
    storyboard_frame_evidence,
)
from lib.speech.voice_settings import VoiceRenderSettings


@pytest.mark.parametrize("count", [1, 4, 9])
@pytest.mark.parametrize("previous", [False, True])
def test_declaration_matches_request_slot_numbers(tmp_path, count, previous):
    (tmp_path / "storyboards").mkdir()
    paths = [f"storyboards/K{index}.png" for index in range(count)]
    for path in paths:
        (tmp_path / path).write_bytes(path.encode())
    item = {"scene_id": "current", "generated_assets": {"shot_grid": {"cell_paths": paths}}}
    items = [item]
    if previous:
        (tmp_path / "end_frames").mkdir()
        (tmp_path / "end_frames/scene_prior.png").write_bytes(b"snapshot")
        items.insert(0, {"scene_id": "prior", "end_frame_image": "end_frames/scene_prior.png"})
    slots = resolve_storyboard_video_slots(
        project_path=tmp_path,
        items=items,
        id_field="scene_id",
        resource_id="current",
    )
    text = render_storyboard_video_prompt(
        {"action": "Move from K0"},
        item,
        content_mode="drama",
        voice_characters=None,
        slots=slots,
    )
    assert ("previous shot" in text) == previous
    assert ("The reference images" in text) == bool(slots.references)
    if slots.references:
        first, last = slots.references[0].keyframe_index, slots.references[-1].keyframe_index
        assert f"K{first}–K{last}" in text
    assert (f"K{count - 1} is supplied as the last frame" in text) == (slots.end is not None)
    assert (
        render_storyboard_video_prompt(
            text,
            item,
            content_mode="drama",
            voice_characters=None,
            slots=slots,
        )
        == text
    )


def test_full_frame_evidence_detects_middle_cell_and_previous_video_changes(tmp_path):
    (tmp_path / "storyboards").mkdir()
    (tmp_path / "videos").mkdir()
    paths = [f"storyboards/K{index}.png" for index in range(4)]
    for path in paths:
        (tmp_path / path).write_bytes(path.encode())
    (tmp_path / "videos/prior.mp4").write_bytes(b"selected-video")
    item = {"scene_id": "current", "generated_assets": {"shot_grid": {"cell_paths": paths}}}
    slots = resolve_storyboard_video_slots(
        project_path=tmp_path,
        items=[
            {"scene_id": "prior", "generated_assets": {"video_clip": "videos/prior.mp4"}},
            item,
        ],
        id_field="scene_id",
        resource_id="current",
    )

    def basis():
        return build_storyboard_video_artifact_visual_basis(
            resource_id="current",
            visual_prompt={"action": "move"},
            storyboard_image=slots.start.path,
            end_frame_image=slots.end.path,
            aspect_ratio="16:9",
            frame_evidence=storyboard_frame_evidence(slots),
        )

    initial = basis()
    (tmp_path / paths[1]).write_bytes(b"changed-middle")
    middle_changed = basis()
    assert middle_changed.digest != initial.digest
    (tmp_path / "videos/prior.mp4").write_bytes(b"new-selected-video")
    assert basis().digest != middle_changed.digest
    assert initial.kind_version == 2


def test_audio_order_and_text_use_utterance_first_appearance(tmp_path: Path):
    audio_dir = tmp_path / "characters/refs_audio"
    audio_dir.mkdir(parents=True)
    for name in ("Alice", "Bob"):
        (audio_dir / f"{name}.wav").write_bytes(name.encode())
    (tmp_path / "storyboards").mkdir()
    (tmp_path / "storyboards/opening.png").write_bytes(b"opening")
    project = {
        "character_voice_binding": "reference_audio",
        "characters": {
            name: {"reference_audio": f"characters/refs_audio/{name}.wav", "description": f"{name} identity"}
            for name in ("Alice", "Bob")
        },
    }
    item = {
        "scene_id": "current",
        "characters_in_scene": ["Alice", "Bob"],
        "utterances": [
            {"kind": "voiceover", "speaker": None, "text": "narration"},
            *({"kind": "dialogue", "speaker": speaker, "text": "hello"} for speaker in ("Bob", "Alice", "Bob")),
        ],
        "generated_assets": {"storyboard_image": "storyboards/opening.png"},
    }
    slots = resolve_storyboard_video_slots(
        project_path=tmp_path,
        items=[item],
        id_field="scene_id",
        resource_id="current",
    )
    audio = resolve_storyboard_audio_inputs(
        project=project,
        project_path=tmp_path,
        item=item,
        slots=slots,
        voice=VoiceRenderSettings(voice_consistency="native", max_reference_audio=2),
    )
    assert audio.speakers == ("Bob", "Alice")
    assert [path.read_bytes() for path in audio.paths] == [b"Bob", b"Alice"]
    text = render_storyboard_video_prompt(
        {"action": "talk"},
        item,
        content_mode="drama",
        voice_characters=project["characters"],
        slots=slots,
        audio_speakers=audio.speakers,
        asset_links=storyboard_asset_links(project, item, "demo"),
    )
    assert "Bob: use the voice in reference audio 1" in text
    assert "Alice: use the voice in reference audio 2" in text
    assert "[Alice](/api/v1/projects/demo#/characters/Alice): Alice identity" in text
    assert "narration" not in text
    project["character_voice_binding"] = "prompt"
    assert not resolve_storyboard_audio_inputs(
        project=project,
        project_path=tmp_path,
        item=item,
        slots=slots,
        voice=VoiceRenderSettings(),
    ).paths


def test_per_image_audio_requires_a_real_declared_grid_target(tmp_path):
    (tmp_path / "characters/refs_audio").mkdir(parents=True)
    (tmp_path / "characters/refs_audio/Alice.wav").write_bytes(b"voice")
    (tmp_path / "storyboards").mkdir()
    (tmp_path / "storyboards/opening.png").write_bytes(b"opening")
    project = {
        "character_voice_binding": "reference_audio",
        "characters": {"Alice": {"reference_audio": "characters/refs_audio/Alice.wav"}},
    }
    item = {
        "scene_id": "current",
        "utterances": [{"kind": "dialogue", "speaker": "Alice", "text": "hello"}],
        "generated_assets": {"storyboard_image": "storyboards/opening.png"},
    }
    slots = resolve_storyboard_video_slots(
        project_path=tmp_path,
        items=[item],
        id_field="scene_id",
        resource_id="current",
    )
    with pytest.raises(ValueError, match="no declared keyframe target"):
        resolve_storyboard_audio_inputs(
            project=project,
            project_path=tmp_path,
            item=item,
            slots=slots,
            voice=VoiceRenderSettings(voice_consistency="native", requires_reference_image=True),
        )
    assert not (tmp_path / "shot_grids").exists()


def test_per_image_audio_targets_the_actual_reference_order(tmp_path):
    (tmp_path / "characters/refs_audio").mkdir(parents=True)
    (tmp_path / "characters/refs_audio/Alice.wav").write_bytes(b"voice")
    (tmp_path / "storyboards").mkdir()
    paths = [f"storyboards/K{index}.png" for index in range(4)]
    for path in paths:
        (tmp_path / path).write_bytes(path.encode())
    grid = ShotGrid.create(episode=1, script_file="episode_1.json", shot_id="current", rows=2, cols=2)
    grid.cells[2].prompt = "@[Alice] turns to speak"
    (tmp_path / "shot_grids").mkdir()
    (tmp_path / f"shot_grids/{grid.id}.json").write_text(json.dumps(grid.to_dict()), encoding="utf-8")
    project = {
        "character_voice_binding": "reference_audio",
        "characters": {"Alice": {"reference_audio": "characters/refs_audio/Alice.wav"}},
    }
    item = {
        "scene_id": "current",
        "utterances": [{"kind": "dialogue", "speaker": "Alice", "text": "hello"}],
        "generated_assets": {"shot_grid": {"grid_id": grid.id, "cell_paths": paths}},
    }
    slots = resolve_storyboard_video_slots(
        project_path=tmp_path, items=[item], id_field="scene_id", resource_id="current"
    )
    audio = resolve_storyboard_audio_inputs(
        project=project,
        project_path=tmp_path,
        item=item,
        slots=slots,
        voice=VoiceRenderSettings(voice_consistency="native", requires_reference_image=True),
    )
    assert audio.targets == (1,)
    assert slots.references[audio.targets[0]].keyframe_index == 2
    assert audio.paths[0].read_bytes() == b"voice"


def test_frame_evidence_preserves_the_complete_contact_sheet_relationship(tmp_path):
    (tmp_path / "storyboards").mkdir()
    paths = [f"storyboards/K{index}.png" for index in range(4)]
    for path in paths:
        (tmp_path / path).write_bytes(path.encode())
    contact = tmp_path / "storyboards/contact.png"
    contact.write_bytes(b"contact")
    item = {
        "scene_id": "current",
        "generated_assets": {
            "shot_grid": {
                "grid_id": "shot_grid_000000000000",
                "rows": 2,
                "cols": 2,
                "composite_path": "storyboards/contact.png",
                "cell_paths": paths,
            }
        },
    }
    slots = resolve_storyboard_video_slots(
        project_path=tmp_path, items=[item], id_field="scene_id", resource_id="current"
    )
    evidence = storyboard_frame_evidence(slots, project_path=tmp_path, items=[item])
    assert [frame["keyframe_index"] for frame in evidence] == [0, 1, 2, 3]
    for frame in evidence:
        grid = frame["grid"]
        assert isinstance(grid, dict)
        assert grid["id"] == "shot_grid_000000000000"
        assert (grid["rows"], grid["cols"]) == (2, 2)
    contact.write_bytes(b"new-contact")
    assert storyboard_frame_evidence(slots, project_path=tmp_path, items=[item]) != evidence
