"""Factory source admission protects the production boundary."""

import hashlib
from pathlib import Path

import pytest

from lib.infra.api_errors import ConflictError
from lib.script.factory_episode import require_factory_script
from lib.script.short_drama_source import short_drama_source_relative_path


@pytest.fixture
def factory_source(tmp_path: Path) -> tuple[Path, dict, dict]:
    source = tmp_path / short_drama_source_relative_path(1)
    source.parent.mkdir(parents=True)
    source.write_text("# 第1集\n角色推门。\n", encoding="utf-8")
    project = {"content_mode": "drama", "generation_mode": "storyboard"}
    script = {
        "content_mode": "drama",
        "scenes": [],
        "short_drama_meta": {"source_revision": hashlib.sha256(source.read_bytes()).hexdigest()},
    }
    return tmp_path, project, script


@pytest.mark.parametrize(
    ("mode", "route"), [("narration", "storyboard"), ("ad", "storyboard"), ("drama", "reference_video")]
)
def test_other_project_routes_are_refused(factory_source: tuple[Path, dict, dict], mode: str, route: str) -> None:
    directory, project, script = factory_source
    project.update(content_mode=mode, generation_mode=route)
    with pytest.raises(ConflictError, match="short_drama_factory_only"):
        require_factory_script(directory, project, script, 1)


def test_legacy_markdown_does_not_admit_production(factory_source: tuple[Path, dict, dict]) -> None:
    directory, project, script = factory_source
    canonical = directory / short_drama_source_relative_path(1)
    canonical.rename(directory / "source" / "short_drama" / "episode_1.md")
    with pytest.raises(ConflictError, match="short_drama_factory_only"):
        require_factory_script(directory, project, script, 1)


def test_historical_json_without_factory_provenance_is_refused(factory_source: tuple[Path, dict, dict]) -> None:
    directory, project, _script = factory_source
    with pytest.raises(ConflictError, match="short_drama_factory_only"):
        require_factory_script(directory, project, {"scenes": []}, 1)


@pytest.mark.parametrize(("mode", "items"), [("narration", "segments"), ("ad", "shots"), ("drama", "video_units")])
def test_old_script_skeleton_cannot_use_factory_source(
    factory_source: tuple[Path, dict, dict], mode: str, items: str
) -> None:
    directory, project, script = factory_source
    script.pop("scenes")
    script.update(content_mode=mode)
    script[items] = []
    with pytest.raises(ConflictError, match="short_drama_factory_only"):
        require_factory_script(directory, project, script, 1)


def test_changed_source_requires_new_visual_derivation(factory_source: tuple[Path, dict, dict]) -> None:
    directory, project, script = factory_source
    (directory / short_drama_source_relative_path(1)).write_text("# 第1集\n角色离开。\n", encoding="utf-8")
    with pytest.raises(ConflictError, match="short_drama_factory_only"):
        require_factory_script(directory, project, script, 1)
