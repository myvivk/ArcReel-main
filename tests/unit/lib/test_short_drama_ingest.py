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


VALID_EPISODE = """\
第【1】集：试戏
【本集时长】：10 秒
[场景]：仓库｜水泥地，单侧顶光
[人物]：甲（杀手｜短发，旧夹克，右眉有疤）
[道具]：铁门｜锈迹斑斑，门轴下垂
△ 铁门第0下被敲响。
△ 铁门第1下被敲响。
△ 铁门第2下被敲响。
△ 铁门第3下被敲响。
△ 铁门第4下被敲响。
【本集断章卡点】：门外传来巨响。（黑屏）
"""


def test_ingest_preserves_utf8_bytes(tmp_path: Path) -> None:
    manager = Manager(tmp_path)
    raw = VALID_EPISODE.replace("\n", "\r\n").encode("utf-8")
    result = ingest_short_drama_artifacts(manager, "demo", [("episodes/episode_01.md", raw)])
    assert result[0].revision
    assert (tmp_path / "demo/source/short_drama/episodes/episode_01.md").read_bytes() == raw


def test_ingest_rejects_episode_failing_quality_gate(tmp_path: Path) -> None:
    manager = Manager(tmp_path)
    raw = "第【1】集：空\n[场景]：仓库\n[人物]：甲（杀手）\n△ 敲门。\n".encode()
    with pytest.raises(ShortDramaArtifactError, match="机检未过"):
        ingest_short_drama_artifacts(manager, "demo", [("episodes/episode_01.md", raw)])
    assert not (tmp_path / "demo/source/short_drama/episodes/episode_01.md").exists()


def test_failed_package_writes_nothing(tmp_path: Path) -> None:
    manager = Manager(tmp_path)
    good = VALID_EPISODE.encode("utf-8")
    bad = VALID_EPISODE.replace("（杀手｜短发，旧夹克，右眉有疤）", "（杀手）").encode("utf-8")
    with pytest.raises(ShortDramaArtifactError):
        ingest_short_drama_artifacts(
            manager,
            "demo",
            [("episodes/episode_01.md", good), ("episodes/episode_02.md", bad)],
        )
    root = tmp_path / "demo/source/short_drama"
    assert not (root / "episodes/episode_01.md").exists()
    assert not (root / "episodes/episode_02.md").exists()


def test_bible_in_same_package_drives_appearance_check(tmp_path: Path) -> None:
    manager = Manager(tmp_path)
    bible = "【角色】：甲\n【外形】：短发，旧夹克，右眉有疤\n".encode()
    ingest_short_drama_artifacts(
        manager,
        "demo",
        [("character-bible.md", bible), ("episodes/episode_01.md", VALID_EPISODE.encode("utf-8"))],
    )

    rewritten = VALID_EPISODE.replace("右眉有疤", "左眉有疤").encode("utf-8")
    with pytest.raises(ShortDramaArtifactError, match="被改写或漏抄"):
        ingest_short_drama_artifacts(manager, "demo", [("episodes/episode_01.md", rewritten)])
