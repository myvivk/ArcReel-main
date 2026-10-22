"""短剧工厂工件的质量校验、原样入库与读取。"""

from __future__ import annotations

import base64
import hashlib
import re
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from lib.episode.episode_paths import episode_source_path
from lib.episode.episode_sources import SOURCE_ORIGIN_FIELD, SourceOrigin
from lib.episode.source_kinds import record_episode_kind
from lib.infra.json_io import atomic_write_bytes
from lib.infra.path_safety import PathTraversalError, safe_join
from lib.project.project_change_hints import emit_project_change_batch
from lib.short_drama_quality import check_episode, check_series, report_passed


class ShortDramaArtifactError(ValueError):
    """工件路径或内容不符合短剧工厂导入契约。"""


class _ArtifactManager(Protocol):
    def get_project_path(self, name: str) -> Path: ...

    def locked_source_registration(
        self, project_name: str
    ) -> AbstractContextManager[tuple[Path, dict[str, Any], ExitStack]]: ...


_ARTIFACT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("character-bible", re.compile(r"^character-bible\.md$")),
    ("series-outline", re.compile(r"^series-outline\.md$")),
    ("ledger", re.compile(r"^ledger\.md$")),
    ("episode-plan", re.compile(r"^episode-plan/episode_(\d+)\.plan\.md$")),
    ("episode", re.compile(r"^episodes/episode_(\d+)\.md$")),
    ("machine-report", re.compile(r"^reports/episode_(\d+)\.machine\.md$")),
    ("compliance-report", re.compile(r"^compliance/episode_(\d+)\.compliance\.md$")),
)


@dataclass(frozen=True, slots=True)
class ShortDramaArtifact:
    path: str
    content: bytes
    revision: str
    kind: str
    episode: int | None

    def as_dict(self, *, include_content: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "path": self.path,
            "revision": self.revision,
            "kind": self.kind,
            "episode": self.episode,
        }
        if include_content:
            result["content"] = self.content.decode("utf-8")
        return result


def classify_short_drama_artifact(path: str) -> tuple[str, int | None]:
    if "\\" in path or path.startswith("/") or ".." in Path(path).parts or Path(path).is_absolute():
        raise ShortDramaArtifactError("工件路径必须是 source/short_drama 下的安全相对路径")
    for kind, pattern in _ARTIFACT_PATTERNS:
        match = pattern.fullmatch(path)
        if match:
            episode = int(match.group(1)) if match.lastindex else None
            if episode is not None and (episode <= 0 or match.group(1) != f"{episode:02d}"):
                raise ShortDramaArtifactError("工件集号必须是正整数，且至少补齐两位（如 episode_01）")
            return kind, episode
    raise ShortDramaArtifactError(f"不支持的短剧工件路径: {path}")


def _artifact_from_bytes(path: str, raw: bytes) -> ShortDramaArtifact:
    kind, episode = classify_short_drama_artifact(path)
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ShortDramaArtifactError(f"工件必须是 UTF-8 文本: {path}") from exc
    return ShortDramaArtifact(path, raw, hashlib.sha256(raw).hexdigest(), kind, episode)


def load_short_drama_artifacts(
    manager: _ArtifactManager, project_name: str, *, include_content: bool = True
) -> list[ShortDramaArtifact]:
    project_dir = manager.get_project_path(project_name)
    root = _safe_path(project_dir, "source/short_drama")
    result: list[ShortDramaArtifact] = []
    for path in sorted(root.rglob("*.md")) if root.exists() else ():
        relative = path.relative_to(root).as_posix()
        try:
            result.append(_artifact_from_bytes(relative, _safe_path(root, relative).read_bytes()))
        except ShortDramaArtifactError:
            continue
    return result


def _safe_path(root: Path, relative: str) -> Path:
    try:
        resolved = safe_join(root, relative)
    except PathTraversalError as exc:
        raise ShortDramaArtifactError(f"工件路径越界：{relative}") from exc
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink() or current.is_junction():
            raise ShortDramaArtifactError(f"工件路径不能经过符号链接：{relative}")
    return resolved


def _restore(path: Path, raw: bytes | None) -> None:
    if raw is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write_bytes(path, raw)


def _write(path: Path, raw: bytes, undo: ExitStack) -> None:
    previous = path.read_bytes() if path.exists() else None
    undo.callback(_restore, path, previous)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, raw)


def _validate_package(prepared: list[ShortDramaArtifact], existing: list[ShortDramaArtifact]) -> None:
    submitted = {item.path: item for item in prepared}
    combined = {item.path: item for item in existing}
    combined.update(submitted)
    episodes = {item.episode for item in prepared if item.episode is not None}
    if not episodes:
        return
    required = {"character-bible.md", "series-outline.md", "ledger.md"}
    for episode in episodes:
        required.update(
            {
                f"episode-plan/episode_{episode:02d}.plan.md",
                f"episodes/episode_{episode:02d}.md",
                f"reports/episode_{episode:02d}.machine.md",
                f"compliance/episode_{episode:02d}.compliance.md",
            }
        )
    missing = sorted(path for path in required if path not in (submitted if "/" in path else combined))
    if missing:
        raise ShortDramaArtifactError(f"工件包不完整，缺少：{'、'.join(missing)}")
    for path in required:
        if not combined[path].content.decode("utf-8").strip():
            raise ShortDramaArtifactError(f"工件内容不能为空：{path}")
    bible = combined["character-bible.md"].content.decode("utf-8")
    scripts = {
        item.episode: item.content.decode("utf-8")
        for item in combined.values()
        if item.kind == "episode" and item.episode is not None
    }
    for episode in sorted(episodes):
        issues = check_episode(
            scripts[episode], bible=bible, previous=[text for number, text in scripts.items() if number < episode]
        )
        failures = [issue.render() for issue in issues if issue.level == "fail"]
        if failures:
            raise ShortDramaArtifactError(f"第 {episode} 集机检未过：{'；'.join(failures)}")
        for path in (f"reports/episode_{episode:02d}.machine.md", f"compliance/episode_{episode:02d}.compliance.md"):
            if not report_passed(submitted[path].content.decode("utf-8")):
                raise ShortDramaArtifactError(f"报告未通过或缺少明确结论（结果: PASSED）：{path}")
    ledger = combined["ledger.md"].content.decode("utf-8")
    total = re.search(r"总集数\s*[:：]\s*(\d+)", ledger)
    failures = [
        issue.render()
        for issue in check_series(ledger, scripts=scripts, total_episodes=int(total.group(1)) if total else 80)
        if issue.level == "fail"
    ]
    if failures:
        raise ShortDramaArtifactError(f"系列机检未过：{'；'.join(failures)}")


def ingest_short_drama_artifacts(
    manager: _ArtifactManager,
    project_name: str,
    artifacts: list[tuple[str, bytes]],
    *,
    expected_revisions: dict[str, str] | None = None,
) -> list[ShortDramaArtifact]:
    if not artifacts:
        raise ShortDramaArtifactError("至少需要一个短剧工件")
    prepared = [_artifact_from_bytes(path, raw) for path, raw in artifacts]
    paths = [item.path for item in prepared]
    if len(paths) != len(set(paths)):
        raise ShortDramaArtifactError("同一导入包内不能重复提交工件")
    expected = expected_revisions or {}
    with manager.locked_source_registration(project_name) as (source_dir, project, undo):
        if project.get("content_mode") != "drama":
            raise ShortDramaArtifactError("只有剧情演绎项目可以导入短剧工厂工件")
        root = _safe_path(source_dir, "short_drama")
        destinations = {item.path: _safe_path(root, item.path) for item in prepared}
        for item in prepared:
            path = destinations[item.path]
            current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            if item.path in expected and current != expected[item.path]:
                raise ShortDramaArtifactError(f"工件已被其他修改更新: {item.path}")
        _validate_package(prepared, load_short_drama_artifacts(manager, project_name))
        for item in prepared:
            _write(destinations[item.path], item.content, undo)
        _register_sources(source_dir.parent, project, prepared, undo)
    # 系列工件更新可能不改 project.json；显式刷新阅读器，不依赖原生脚本快照差分。
    emit_project_change_batch(
        project_name,
        [
            {
                "entity_type": "project",
                "action": "updated",
                "entity_id": "short_drama_factory",
                "label": "",
                "focus": None,
                "important": False,
            }
        ],
    )
    return prepared


def decode_artifact_content(content: str | None, content_base64: str | None) -> bytes:
    if (content is None) == (content_base64 is None):
        raise ShortDramaArtifactError("工件必须提供 content 或 content_base64 之一")
    if content_base64 is not None:
        try:
            return base64.b64decode(content_base64, validate=True)
        except ValueError as exc:
            raise ShortDramaArtifactError("content_base64 不是有效的 Base64") from exc
    assert content is not None
    try:
        return content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ShortDramaArtifactError("工件必须是 UTF-8 文本") from exc


def register_factory_episode_sources(
    manager: _ArtifactManager, project_name: str, artifacts: list[tuple[str, bytes]]
) -> None:
    """给已落盘工件补登记 OWN 源文本，供存量项目补录命令使用。"""
    prepared = [_artifact_from_bytes(path, raw) for path, raw in artifacts]
    with manager.locked_source_registration(project_name) as (source_dir, project, undo):
        _register_sources(source_dir.parent, project, prepared, undo)


def _register_sources(
    project_dir: Path, project: dict[str, Any], artifacts: list[ShortDramaArtifact], undo: ExitStack
) -> None:
    entries = {entry["episode"]: entry for entry in project.get("episodes", [])}
    for item in artifacts:
        if item.kind != "episode" or item.episode not in entries:
            continue
        assert item.episode is not None
        entry = entries[item.episode]
        path = episode_source_path(project_dir, item.episode)
        path = _safe_path(project_dir, path.relative_to(project_dir).as_posix())
        # 集账本的基础设施副本与工厂源稿保持相同字节，不经过通用源文归一化。
        _write(path, item.content, undo)
        entry[SOURCE_ORIGIN_FIELD] = SourceOrigin.OWN.value
        entry.pop("source_range", None)
        record_episode_kind(project, entry, "screenplay")


__all__ = [
    "ShortDramaArtifact",
    "ShortDramaArtifactError",
    "classify_short_drama_artifact",
    "decode_artifact_content",
    "ingest_short_drama_artifacts",
    "load_short_drama_artifacts",
]
