"""视频提示词的前提、冻结输入与只写派生字段契约。"""

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from lib.backends.text_generator import TextGenerator
from lib.project.project_manager import ProjectManager
from lib.script.grid.shot_grid_manager import ShotGridManager
from lib.script.grid.video_prompt_targets import video_prompt_basis_for
from lib.script.script_generator import PromptAuthoringTargetError, ScriptGenerator
from lib.script.script_models import PENDING_AUTHORING_FIELD
from lib.script.script_revision import content_fingerprint
from lib.script.short_drama_source import replace_short_drama_source
from tests.fakes import FakeTextGenerator
from tests.integration.lib.script.storyboard_support import confirmed_grid, factory_prompt_project, read_factory_script


def response(*ids: str, **video_fields: Any) -> str:
    prompt = {"action": "K0推门，K1至K3走到窗边。", "camera_motion": "Push In", "ambiance_audio": "风声"}
    prompt.update(video_fields)
    return json.dumps(
        {"scenes": [{"scene_id": entry_id, "video_prompt": prompt} for entry_id in ids]}, ensure_ascii=False
    )


class DuringGeneration(FakeTextGenerator):
    def __init__(self, text: str, change: Callable[[], None]) -> None:
        super().__init__(text)
        self.change = change

    async def generate(self, request: Any, project_name: str | None = None, *, require_complete: bool = False) -> Any:
        result = await super().generate(request, project_name, require_complete=require_complete)
        self.change()
        return result


async def test_only_ready_shots_are_written_and_basis_matches_model_input(tmp_path: Path) -> None:
    directory = factory_prompt_project(tmp_path, factory=True)
    grid = confirmed_grid(directory, "E1S01")
    before = read_factory_script(directory)
    source_bytes = (directory / "source/short_drama/episodes/episode_01.md").read_bytes()
    fake = FakeTextGenerator(response("E1S01"))
    generator = ScriptGenerator(directory, generator=cast(TextGenerator, fake))
    written: list[str] = []
    await generator.generate(1, fields=["video_prompt"], rewritten_entry_ids=written)
    after = read_factory_script(directory)
    assert written == ["E1S01"]
    assert after["scenes"][1] == before["scenes"][1]
    expected = copy.deepcopy(before["scenes"][0])
    expected["video_prompt"] = json.loads(response("E1S01"))["scenes"][0]["video_prompt"]
    expected["generated_assets"] = {"video_prompt_basis": after["scenes"][0]["generated_assets"]["video_prompt_basis"]}
    assert after["scenes"][0] == expected
    basis = after["scenes"][0]["generated_assets"]["video_prompt_basis"]
    assert basis == video_prompt_basis_for(grid, written_at=basis["written_at"])
    assert "K3（中景）：E1S01 关键帧3的动作姿态" in fake.requests[0].prompt
    assert "### E1S02" not in fake.requests[0].prompt
    assert (directory / "source/short_drama/episodes/episode_01.md").read_bytes() == source_bytes


@pytest.mark.parametrize(
    "failure", ["unconfirmed", "image_prompt", "missing_image", "source", "anchor", "visual_state"]
)
async def test_explicit_targets_refuse_missing_prerequisites_before_model(tmp_path: Path, failure: str) -> None:
    directory = factory_prompt_project(tmp_path, factory=True)
    grid = confirmed_grid(directory, "E1S01")
    path = directory / "scripts/episode_1.json"
    if failure == "unconfirmed":
        grid.status = "completed"
        ShotGridManager(directory).save(grid)
    elif failure == "missing_image":
        (directory / grid.cell_paths[0]).unlink()
    elif failure == "source":
        (directory / "source/short_drama/episodes/episode_01.md").unlink()
    elif failure == "visual_state":
        path.unlink()
    else:
        current = read_factory_script(directory)
        if failure == "anchor":
            current["scenes"][0]["source_text"] = "原稿中不存在的剧情。"
        else:
            current["scenes"][0]["image_prompt"] = None
        path.write_text(json.dumps(current), encoding="utf-8")
    before = path.read_bytes() if path.exists() else None
    fake = FakeTextGenerator(response("E1S01"))
    generator = ScriptGenerator(directory, generator=cast(TextGenerator, fake))
    with pytest.raises(PromptAuthoringTargetError):
        generator.prompt_overwrite(1, fields=["video_prompt"], entry_ids=["E1S01"])
    with pytest.raises(PromptAuthoringTargetError):
        await generator.build_prompt(1, fields=["video_prompt"], entry_ids=["E1S01"])
    with pytest.raises(PromptAuthoringTargetError):
        await generator.generate(1, fields=["video_prompt"], entry_ids=["E1S01"])
    assert fake.requests == []
    assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.parametrize(
    "video_fields",
    [
        {"action": " "},
        {"ambiance_audio": "K4风声"},
        {"camera_motion": "K9 Tracking Shot"},
        {"action": "K0起手\nAvoid: blur"},
        {"ambiance_audio": "<Voice_Profiles>角色</Voice_Profiles>"},
    ],
)
async def test_all_body_fields_are_validated_before_saving(tmp_path: Path, video_fields: dict[str, str]) -> None:
    directory = factory_prompt_project(tmp_path)
    confirmed_grid(directory, "E1S01")
    path = directory / "scripts/episode_1.json"
    before = path.read_bytes()
    fake = FakeTextGenerator(response("E1S01", **video_fields))
    generator = ScriptGenerator(directory, generator=cast(TextGenerator, fake))
    with pytest.raises(ValueError, match=r"关键帧|正文|empty|不能为空"):
        await generator.generate(1, fields=["video_prompt"], entry_ids=["E1S01"])
    assert path.read_bytes() == before


@pytest.mark.parametrize("changed", ["cells", "layout", "confirmation", "source", "image", "image_replaced"])
async def test_input_changes_during_generation_refuse_the_whole_write(tmp_path: Path, changed: str) -> None:
    directory = factory_prompt_project(tmp_path, factory=True)
    grid = confirmed_grid(directory, "E1S01")
    path = directory / "scripts/episode_1.json"
    before = path.read_bytes()

    def change() -> None:
        if changed == "source":
            replace_short_drama_source(ProjectManager.for_project_dir(directory), directory.name, 1, "# 修改后的原稿\n")
        elif changed == "image":
            (directory / grid.cell_paths[0]).unlink()
        elif changed == "image_replaced":
            Image.new("RGB", (8, 8), "black").save(directory / grid.cell_paths[0])
        else:
            if changed == "cells":
                grid.cells[0].prompt = "改变后的起手"
            elif changed == "layout":
                grid.rows, grid.cols = 1, 4
            else:
                grid.status = "completed"
            ShotGridManager(directory).save(grid)

    fake = DuringGeneration(response("E1S01"), change)
    generator = ScriptGenerator(directory, generator=cast(TextGenerator, fake))
    with pytest.raises(PromptAuthoringTargetError):
        await generator.generate(1, fields=["video_prompt"], entry_ids=["E1S01"])
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("content_mode", "generation_mode"),
    [("narration", "storyboard"), ("ad", "storyboard"), ("drama", "reference_video")],
)
async def test_other_routes_cannot_author_prompts(tmp_path: Path, content_mode: str, generation_mode: str) -> None:
    directory = factory_prompt_project(tmp_path)
    metadata_path = directory / "project.json"
    metadata = json.loads(metadata_path.read_text())
    metadata.update(content_mode=content_mode, generation_mode=generation_mode)
    metadata_path.write_text(json.dumps(metadata))
    before = (directory / "scripts/episode_1.json").read_bytes()
    fake = FakeTextGenerator(response("E1S01"))
    generator = ScriptGenerator(directory, generator=cast(TextGenerator, fake))

    with pytest.raises(PromptAuthoringTargetError):
        await generator.generate(1, fields=["image_prompt"])

    assert not fake.requests
    assert (directory / "scripts/episode_1.json").read_bytes() == before


async def test_image_prompt_rewrite_preserves_factory_content_and_other_shots(tmp_path: Path) -> None:
    directory = factory_prompt_project(tmp_path)
    path = directory / "scripts/episode_1.json"
    before = read_factory_script(directory)
    raw_source = (directory / "source/short_drama/episodes/episode_01.md").read_bytes()
    image_prompt = {
        "scene": "【镜头1】男人开门。 【镜头2】男人停在窗前。",
        "composition": {"shot_type": "Medium Shot", "lighting": "日光", "ambiance": "安静"},
    }
    fake = FakeTextGenerator(json.dumps({"scenes": [{"scene_id": "E1S01", "image_prompt": image_prompt}]}))
    generator = ScriptGenerator(directory, generator=cast(TextGenerator, fake))
    written: list[str] = []

    await generator.generate(
        1,
        entry_ids=["E1S01"],
        rewrite=True,
        fields=["image_prompt"],
        overwrite_revision=content_fingerprint(path),
        rewritten_entry_ids=written,
    )

    after = read_factory_script(directory)
    expected = copy.deepcopy(before["scenes"][0])
    expected["image_prompt"] = image_prompt
    expected[PENDING_AUTHORING_FIELD] = True
    assert written == ["E1S01"]
    assert after["scenes"][0] == expected
    assert after["scenes"][1] == before["scenes"][1]
    assert after["short_drama_meta"] == before["short_drama_meta"]
    assert (directory / "source/short_drama/episodes/episode_01.md").read_bytes() == raw_source
