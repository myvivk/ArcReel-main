from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest

from lib.project.project_change_hints import register_project_change_batch_listener
from lib.short_drama_ingest import (
    ShortDramaArtifactError,
    classify_short_drama_artifact,
    ingest_short_drama_artifacts,
    read_draft_artifact,
)
from tests.short_drama_support import factory_episode, factory_package

pytestmark = pytest.mark.usefixtures("factory_profile")


class Manager:
    def __init__(self, root: Path) -> None:
        self.root = root

    def get_project_path(self, name: str) -> Path:
        return self.root / name

    @contextmanager
    def locked_source_registration(self, project_name: str):
        source = self.root / project_name / "source"
        source.mkdir(parents=True, exist_ok=True)
        with ExitStack() as undo:
            yield source, {"content_mode": "drama", "episodes": []}, undo
            undo.pop_all()


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


def test_read_draft_artifact_reads_staged_bytes(tmp_path: Path) -> None:
    draft = tmp_path / "demo/drafts/short_drama/character-bible.md"
    draft.parent.mkdir(parents=True)
    draft.write_bytes(b"# bible\n")
    assert read_draft_artifact(tmp_path / "demo", "character-bible.md") == b"# bible\n"


def test_read_draft_artifact_rejects_missing_and_unsafe_paths(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    (project / "drafts/short_drama").mkdir(parents=True)
    with pytest.raises(ShortDramaArtifactError, match="暂存工件不存在"):
        read_draft_artifact(project, "character-bible.md")
    with pytest.raises(ShortDramaArtifactError):
        read_draft_artifact(project, "../project.json")


def test_read_draft_artifact_rejects_symlink(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    drafts = project / "drafts/short_drama"
    drafts.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"secret")
    (drafts / "character-bible.md").symlink_to(outside)
    with pytest.raises(ShortDramaArtifactError):
        read_draft_artifact(project, "character-bible.md")


def test_ingest_preserves_utf8_bytes(tmp_path: Path) -> None:
    manager = Manager(tmp_path)
    markdown = factory_episode().replace("\n", "\n")
    raw = markdown.encode("utf-8")
    result = ingest_short_drama_artifacts(manager, "demo", factory_package(markdown))
    assert result[0].revision
    assert (tmp_path / "demo/source/short_drama/episodes/episode_01.md").read_bytes() == raw


def test_ingest_rejects_episode_failing_quality_gate(tmp_path: Path) -> None:
    manager = Manager(tmp_path)
    raw = "第【1】集：空\n[场景]：仓库\n[人物]：甲（杀手）\n△ 敲门。\n".encode()
    with pytest.raises(ShortDramaArtifactError, match="机检未过"):
        ingest_short_drama_artifacts(
            manager,
            "demo",
            [(path, raw if path.startswith("episodes/") else content) for path, content in factory_package()],
        )
    assert not (tmp_path / "demo/source/short_drama/episodes/episode_01.md").exists()


def test_failed_package_writes_nothing(tmp_path: Path) -> None:
    manager = Manager(tmp_path)
    good = factory_package()
    bad = factory_package(factory_episode().replace("三十岁上下，寸头，旧夹克磨白", "寸头"), episode=2)
    with pytest.raises(ShortDramaArtifactError):
        ingest_short_drama_artifacts(
            manager,
            "demo",
            good + [(path, raw) for path, raw in bad if "/" in path],
        )
    root = tmp_path / "demo/source/short_drama"
    assert not (root / "episodes/episode_01.md").exists()
    assert not (root / "episodes/episode_02.md").exists()


def test_bible_in_same_package_drives_appearance_check(tmp_path: Path) -> None:
    manager = Manager(tmp_path)
    ingest_short_drama_artifacts(manager, "demo", factory_package())
    rewritten = factory_episode().replace("三十岁上下，寸头", "四十岁上下，寸头")
    with pytest.raises(ShortDramaArtifactError, match="被改写或漏抄"):
        ingest_short_drama_artifacts(
            manager, "demo", [(path, raw) for path, raw in factory_package(rewritten) if "/" in path]
        )


@pytest.mark.parametrize(
    "missing",
    [
        "character-bible.md",
        "series-outline.md",
        "ledger.md",
        "episode-plan/episode_01.plan.md",
        "reports/episode_01.machine.md",
        "compliance/episode_01.compliance.md",
    ],
)
def test_incomplete_first_batch_is_rejected(tmp_path: Path, missing: str) -> None:
    with pytest.raises(ShortDramaArtifactError, match="工件包不完整"):
        ingest_short_drama_artifacts(
            Manager(tmp_path), "demo", [(path, raw) for path, raw in factory_package() if path != missing]
        )
    assert not list((tmp_path / "demo/source").rglob("*.md"))


@pytest.mark.parametrize("report", ["reports/episode_01.machine.md", "compliance/episode_01.compliance.md"])
@pytest.mark.parametrize("conclusion", ["结果: FAILED", "尚未检查", "结果: 不通过"])
def test_failed_or_pending_report_rejects_whole_batch(tmp_path: Path, report: str, conclusion: str) -> None:
    package = [(path, conclusion.encode() if path == report else raw) for path, raw in factory_package()]
    with pytest.raises(ShortDramaArtifactError, match="报告未通过"):
        ingest_short_drama_artifacts(Manager(tmp_path), "demo", package)
    assert not list((tmp_path / "demo/source").rglob("*.md"))


@pytest.mark.parametrize("path", ["episodes/episode_0.md", "episodes/episode_1.md", "episodes/episode_001.md"])
def test_episode_path_has_one_canonical_spelling(path: str) -> None:
    with pytest.raises(ShortDramaArtifactError):
        classify_short_drama_artifact(path)


def test_symlink_directory_cannot_escape_factory_root(tmp_path: Path) -> None:
    root = tmp_path / "demo/source/short_drama"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "episodes").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ShortDramaArtifactError):
        ingest_short_drama_artifacts(Manager(tmp_path), "demo", factory_package())
    assert list(outside.iterdir()) == []
    assert not (root / "character-bible.md").exists()


def test_disk_failure_rolls_back_prior_files(monkeypatch, tmp_path: Path) -> None:
    from lib.infra.json_io import atomic_write_bytes

    manager = Manager(tmp_path)
    package = factory_package()
    ingest_short_drama_artifacts(manager, "demo", package)
    root = tmp_path / "demo/source/short_drama"
    original = {path: (root / path).read_bytes() for path, _ in package}

    def failing_write(path: Path, raw: bytes) -> None:
        if path.name == "episode_02.md":
            raise OSError("disk full")
        atomic_write_bytes(path, raw)

    monkeypatch.setattr("lib.short_drama_ingest.atomic_write_bytes", failing_write)
    with pytest.raises(OSError, match="disk full"):
        ingest_short_drama_artifacts(manager, "demo", factory_package(episode=2))
    assert {path: (root / path).read_bytes() for path in original} == original
    assert not (root / "episodes/episode_02.md").exists()
    assert not (root / "episode-plan/episode_02.plan.md").exists()


def test_series_only_write_notifies_readers_after_success(tmp_path: Path) -> None:
    observed = []
    manager = Manager(tmp_path)

    def on_change(project, source, changes):
        observed.append((project, (tmp_path / project / "source/short_drama/ledger.md").read_bytes(), changes))

    unregister = register_project_change_batch_listener(on_change)
    try:
        ingest_short_drama_artifacts(manager, "demo", [("ledger.md", b"# ledger")])
    finally:
        unregister()
    assert observed[0][:2] == ("demo", b"# ledger")
    assert observed[0][2][0]["entity_id"] == "short_drama_factory"
