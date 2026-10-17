from pathlib import Path

import pytest

from lib.short_drama_ingest import (
    ShortDramaArtifactError,
    classify_short_drama_artifact,
    ingest_short_drama_artifacts,
)


class Manager:
    def __init__(self, root: Path) -> None:
        self.root = root

    def get_project_path(self, name: str) -> Path:
        return self.root / name

    def locked_source_mutation(self, project_name: str):
        manager = self

        class Context:
            def __enter__(self):
                source = manager.root / project_name / "source"
                source.mkdir(parents=True, exist_ok=True)
                return source

            def __exit__(self, exc_type, exc, tb):
                return False

        return Context()


def test_classifies_all_factory_artifacts() -> None:
    assert classify_short_drama_artifact("character-bible.md") == ("character-bible", None)
    assert classify_short_drama_artifact("episode-plan/episode_03.plan.md") == ("episode-plan", 3)
    assert classify_short_drama_artifact("episodes/episode_03.md") == ("episode", 3)
    assert classify_short_drama_artifact("reports/episode_03.machine.md") == ("machine-report", 3)


def test_rejects_unsafe_or_unknown_path() -> None:
    with pytest.raises(ShortDramaArtifactError):
        classify_short_drama_artifact("../project.json")
    with pytest.raises(ShortDramaArtifactError):
        classify_short_drama_artifact("episodes/episode_3.txt")


def test_ingest_preserves_utf8_bytes(tmp_path: Path) -> None:
    manager = Manager(tmp_path)
    raw = "人物：林夏\n".encode("utf-8")
    result = ingest_short_drama_artifacts(manager, "demo", [("episodes/episode_01.md", raw)])
    assert result[0].revision
    assert (tmp_path / "demo/source/short_drama/episodes/episode_01.md").read_bytes() == raw
