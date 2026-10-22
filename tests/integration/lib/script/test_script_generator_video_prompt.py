"""视频提示词的前提、冻结输入与只写派生字段契约。"""

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from lib.artifacts.artifact_activation import activate_artifact_target_state
from lib.backends.text_generator import TextGenerator
from lib.project.project_manager import ProjectManager
from lib.project.project_schema import CURRENT_PROJECT_SCHEMA_VERSION
from lib.script.grid.shot_grid_manager import ShotGridManager
from lib.script.grid.video_prompt_targets import video_prompt_basis_for
from lib.script.script_generator import PromptAuthoringTargetError, ScriptGenerator
from lib.script.short_drama_source import replace_short_drama_source
from tests.fakes import FakeTextGenerator
from tests.integration.lib.script.storyboard_support import confirmed_grid


def project(tmp_path: Path, *, factory: bool = False) -> Path:
    directory = tmp_path / "projects" / "demo"
    directory.mkdir(parents=True)
    payload = {
        "schema_version": CURRENT_PROJECT_SCHEMA_VERSION,
        "title": "视频测试",
        "content_mode": "drama",
        "generation_mode": "storyboard",
        "characters": {},
        "scenes": {},
        "props": {},
        "episodes": [{"episode": 1, "script_file": "scripts/episode_1.json"}],
    }
    (directory / "project.json").write_text(json.dumps(payload), encoding="utf-8")
    scripts = directory / "scripts"
    scripts.mkdir()
    script: dict[str, Any] = {
        "episode": 1,
        "title": "视频测试",
        "content_mode": "drama",
        "scenes": [
            {
                "scene_id": f"E1S{index:02d}",
                "duration_seconds": 12,
                "segment_break": False,
                "characters_in_scene": [],
                "scenes": [],
                "props": [],
                "utterances": [],
                "source_text": "男人推门，走到窗边。",
                "scene_description": "推门进屋",
                "image_prompt": f"【镜头1】男人推门。 【镜头2】走到窗边。分镜{index}",
                "video_prompt": None,
            }
            for index in (1, 2)
        ],
    }
    source = directory / "source"
    source.mkdir()
    (source / "episode_1.txt").write_text("男人推门，走到窗边。", encoding="utf-8")
    if factory:
        saved = replace_short_drama_source(
            ProjectManager.for_project_dir(directory), directory.name, 1, "# 第1集\n男人推门，走到窗边。\n"
        )
        script["short_drama_meta"] = {"source_revision": saved.revision}
    (scripts / "episode_1.json").write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    activate_artifact_target_state(directory, bump_schema=False)
    return directory


def response(*ids: str, **video_fields: Any) -> str:
    prompt = {"action": "K0推门，K1至K3走到窗边。", "camera_motion": "Push In", "ambiance_audio": "风声"}
    prompt.update(video_fields)
    return json.dumps(
        {"scenes": [{"scene_id": entry_id, "video_prompt": prompt} for entry_id in ids]}, ensure_ascii=False
    )


def script(directory: Path) -> dict[str, Any]:
    return json.loads((directory / "scripts" / "episode_1.json").read_text(encoding="utf-8"))


class DuringGeneration(FakeTextGenerator):
    def __init__(self, text: str, change: Callable[[], None]) -> None:
        super().__init__(text)
        self.change = change

    async def generate(self, request: Any, project_name: str | None = None, *, require_complete: bool = False) -> Any:
        result = await super().generate(request, project_name, require_complete=require_complete)
        self.change()
        return result


async def test_only_ready_shots_are_written_and_basis_matches_model_input(tmp_path: Path) -> None:
    directory = project(tmp_path, factory=True)
    grid = confirmed_grid(directory, "E1S01")
    before = script(directory)
    source_bytes = (directory / "source/short_drama/episodes/episode_01.md").read_bytes()
    fake = FakeTextGenerator(response("E1S01"))
    generator = ScriptGenerator(directory, generator=cast(TextGenerator, fake))
    written: list[str] = []
    await generator.generate(1, fields=["video_prompt"], rewritten_entry_ids=written)
    after = script(directory)
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
    directory = project(tmp_path, factory=True)
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
        current = script(directory)
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
    directory = project(tmp_path)
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
    directory = project(tmp_path, factory=True)
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
