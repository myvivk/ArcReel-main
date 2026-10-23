"""项目创建只允许短剧工厂，历史模式不得从数据层旁路新建。"""

from pathlib import Path

import pytest

from lib.project.project_manager import ProjectManager


@pytest.mark.parametrize("mode", ["narration", "ad"])
def test_create_rejects_retired_modes_before_creating_a_directory(tmp_path: Path, mode: str) -> None:
    manager = ProjectManager(tmp_path)
    with pytest.raises(ValueError, match="短剧工厂"):
        manager.create_project("retired", content_mode=mode)
    assert not manager.get_project_path("retired").exists()


@pytest.mark.parametrize("mode", ["narration", "ad"])
def test_metadata_rejects_retired_modes_without_overwriting_factory_project(tmp_path: Path, mode: str) -> None:
    manager = ProjectManager(tmp_path)
    directory = manager.create_project("factory")
    before = (directory / "project.json").read_bytes()
    with pytest.raises(ValueError, match="短剧工厂"):
        manager.create_project_metadata("factory", content_mode=mode)
    assert (directory / "project.json").read_bytes() == before


@pytest.mark.parametrize(
    "extras",
    [
        {"content_mode": "ad"},
        {"generation_mode": "reference_video"},
        {"grid_storyboard": True},
        {"brief": "广告诉求"},
        {"target_duration": 60},
    ],
)
def test_extras_cannot_reintroduce_retired_workflows(tmp_path: Path, extras: dict[str, object]) -> None:
    manager = ProjectManager(tmp_path)
    directory = manager.create_project("factory")
    before = (directory / "project.json").read_bytes()
    with pytest.raises(ValueError, match="extras"):
        manager.create_project_metadata("factory", extras=extras)
    assert (directory / "project.json").read_bytes() == before


def test_factory_metadata_persists_creation_preferences(tmp_path: Path) -> None:
    manager = ProjectManager(tmp_path)
    manager.create_project("factory")
    project = manager.create_project_metadata(
        "factory", "短剧", "Realistic", default_duration=8, episode_target_duration=120, total_episodes=12
    )
    assert project["content_mode"] == "drama"
    assert project["generation_mode"] == "storyboard"
    assert project["default_duration"] == 8
    assert project["episode_target_duration"] == 120
    assert project["total_episodes"] == 12
    assert manager.load_project("factory") == project
