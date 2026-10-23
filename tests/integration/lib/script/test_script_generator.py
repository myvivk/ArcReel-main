"""工厂分镜提示词编写：范围、覆盖、并发保存与正文保留。"""

import copy
import json
from pathlib import Path
from typing import cast

import pytest

from lib.backends.text_generator import TextGenerator
from lib.project.project_manager import ProjectManager, ScriptWriteConflict
from lib.script.prompt_authoring_scope import PromptOverwriteRequired
from lib.script.script_generator import PromptAuthoringTargetError, ScriptGenerator
from lib.script.script_revision import content_fingerprint
from tests.fakes import FakeTextGenerator
from tests.integration.lib.script.storyboard_support import factory_prompt_project, read_factory_script


def _response(*ids: str) -> str:
    return json.dumps(
        {
            "scenes": [
                {
                    "scene_id": scene_id,
                    "image_prompt": {
                        "scene": "【镜头1】男人推开木门。 【镜头2】男人走到窗前。",
                        "composition": {"shot_type": "Medium Shot", "lighting": "日光", "ambiance": "安静"},
                    },
                }
                for scene_id in ids
            ]
        },
        ensure_ascii=False,
    )


def _save(directory: Path, script: dict) -> None:
    ProjectManager.for_project_dir(directory).save_script(directory.name, script, "episode_1.json")


def _generator(directory: Path, fake: FakeTextGenerator) -> ScriptGenerator:
    return ScriptGenerator(directory, generator=cast(TextGenerator, fake))


async def test_default_fill_only_writes_missing_image_prompts(tmp_path: Path) -> None:
    directory = factory_prompt_project(tmp_path)
    before = read_factory_script(directory)
    before["scenes"][0].update(image_prompt=None, pending_authoring=True, note="保留备注")
    _save(directory, before)
    source = (directory / "source/short_drama/episodes/episode_01.md").read_bytes()
    fake = FakeTextGenerator(_response("E1S01"))
    written: list[str] = []

    await _generator(directory, fake).generate(1, rewritten_entry_ids=written)

    after = read_factory_script(directory)
    expected = copy.deepcopy(before)
    expected["scenes"][0]["image_prompt"] = json.loads(_response("E1S01"))["scenes"][0]["image_prompt"]
    assert {key: value for key, value in after.items() if key != "metadata"} == {
        key: value for key, value in expected.items() if key != "metadata"
    }
    assert after["metadata"]["created_at"] == before["metadata"]["created_at"]
    assert after["metadata"]["status"] == before["metadata"]["status"]
    assert after["metadata"]["generator"] == "fake-text"
    assert written == ["E1S01"]
    assert len(fake.requests) == 1
    assert (directory / "source/short_drama/episodes/episode_01.md").read_bytes() == source


async def test_no_pending_entries_preserves_bytes_without_calling_model(tmp_path: Path) -> None:
    directory = factory_prompt_project(tmp_path)
    path = directory / "scripts/episode_1.json"
    before = path.read_bytes()
    fake = FakeTextGenerator(_response("E1S01"))
    written: list[str] = []

    output = await _generator(directory, fake).generate(1, rewritten_entry_ids=written)

    assert output == path
    assert written == []
    assert fake.requests == []
    assert path.read_bytes() == before


async def test_rewrite_requires_the_current_script_revision(tmp_path: Path) -> None:
    directory = factory_prompt_project(tmp_path)
    path = directory / "scripts/episode_1.json"
    before = path.read_bytes()
    fake = FakeTextGenerator(_response("E1S01"))

    for revision in (None, "old-revision"):
        with pytest.raises(PromptOverwriteRequired):
            await _generator(directory, fake).generate(
                1, entry_ids=["E1S01"], rewrite=True, fields=["image_prompt"], overwrite_revision=revision
            )

    assert fake.requests == []
    assert path.read_bytes() == before


@pytest.mark.parametrize("target", ["E1S99", "E2S01"])
async def test_unknown_target_is_rejected_without_writing(tmp_path: Path, target: str) -> None:
    directory = factory_prompt_project(tmp_path)
    path = directory / "scripts/episode_1.json"
    before = path.read_bytes()
    fake = FakeTextGenerator(_response(target))

    with pytest.raises(PromptAuthoringTargetError):
        await _generator(directory, fake).generate(1, entry_ids=[target])

    assert fake.requests == []
    assert path.read_bytes() == before


@pytest.mark.parametrize("call", ["generate", "preview"])
async def test_missing_formal_script_cannot_be_rebuilt_from_native_drafts(tmp_path: Path, call: str) -> None:
    directory = factory_prompt_project(tmp_path)
    path = directory / "scripts/episode_1.json"
    path.unlink()
    drafts = directory / "drafts/episode_1"
    drafts.mkdir(parents=True)
    (drafts / "script_plan_normalized_script.json").write_text(_response("E1S01"), encoding="utf-8")
    fake = FakeTextGenerator(_response("E1S01"))
    generator = _generator(directory, fake)

    operation = generator.generate if call == "generate" else generator.build_prompt
    with pytest.raises(PromptAuthoringTargetError):
        await operation(1)

    assert fake.requests == []
    assert not path.exists()


async def test_preview_uses_the_selected_formal_scene_and_does_not_write(tmp_path: Path) -> None:
    directory = factory_prompt_project(tmp_path)
    script = read_factory_script(directory)
    script["scenes"][0].update(image_prompt=None, pending_authoring=True)
    _save(directory, script)
    path = directory / "scripts/episode_1.json"
    before = path.read_bytes()

    prompt = await ScriptGenerator(directory).build_prompt(1, entry_ids=["E1S01"], instructions="保留窗边停顿。")

    assert "### E1S01" in prompt
    assert "保留窗边停顿。" in prompt
    assert "本次只编写 image_prompt" in prompt
    assert path.read_bytes() == before


async def test_concurrent_script_edit_survives_a_generation_conflict(tmp_path: Path) -> None:
    directory = factory_prompt_project(tmp_path)
    path = directory / "scripts/episode_1.json"
    baseline = content_fingerprint(path)

    class ConcurrentEdit(FakeTextGenerator):
        async def generate(self, request, project_name=None, *, require_complete=False):
            result = await super().generate(request, project_name, require_complete=require_complete)
            edited = read_factory_script(directory)
            edited["scenes"][1]["note"] = "生成期间的用户修改"
            _save(directory, edited)
            return result

    fake = ConcurrentEdit(_response("E1S01"))
    before = read_factory_script(directory)["scenes"][0]

    with pytest.raises(ScriptWriteConflict):
        await _generator(directory, fake).generate(
            1, entry_ids=["E1S01"], rewrite=True, fields=["image_prompt"], overwrite_revision=baseline
        )

    after = read_factory_script(directory)
    assert after["scenes"][0] == before
    assert after["scenes"][1]["note"] == "生成期间的用户修改"
