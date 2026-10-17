"""短剧工厂 Markdown 源稿的路径、读取与并发写入。"""

from __future__ import annotations

import hashlib
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from lib.infra.json_io import atomic_write_bytes


class _ProjectSourceManager(Protocol):
    def get_project_path(self, name: str) -> Path: ...

    def locked_source_mutation(self, project_name: str) -> AbstractContextManager[Path]: ...


class ShortDramaSourceConflict(ValueError):
    """源稿已被其他写入者更新。"""


@dataclass(frozen=True, slots=True)
class ShortDramaSource:
    episode: int
    path: str
    markdown: str
    revision: str


def short_drama_source_relative_path(episode: int) -> str:
    if type(episode) is not int or episode <= 0:
        raise ValueError("episode must be a positive integer")
    return f"source/short_drama/episodes/episode_{episode:02d}.md"


def _legacy_short_drama_source_relative_path(episode: int) -> str:
    return f"source/short_drama/episode_{episode}.md"


def _revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load_short_drama_source(
    manager: _ProjectSourceManager,
    project_name: str,
    episode: int,
) -> ShortDramaSource | None:
    relative = short_drama_source_relative_path(episode)
    path = manager.get_project_path(project_name) / relative
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        legacy_path = manager.get_project_path(project_name) / _legacy_short_drama_source_relative_path(episode)
        try:
            raw = legacy_path.read_bytes()
        except FileNotFoundError:
            return None
        relative = _legacy_short_drama_source_relative_path(episode)
    return ShortDramaSource(
        episode=episode,
        path=relative,
        markdown=raw.decode("utf-8"),
        revision=_revision(raw),
    )


def save_short_drama_source(
    manager: _ProjectSourceManager,
    project_name: str,
    episode: int,
    markdown: str,
    *,
    expected_revision: str | object | None,
) -> ShortDramaSource:
    """保存源稿；``expected_revision`` 为 None 时只允许新建，传 object 哨兵时无条件写入。"""
    relative = short_drama_source_relative_path(episode)
    raw = markdown.encode("utf-8")
    with manager.locked_source_mutation(project_name) as source_dir:
        path = source_dir / "short_drama" / "episodes" / f"episode_{episode:02d}.md"
        current = None
        with suppress(FileNotFoundError):
            current = _revision(path.read_bytes())
        if expected_revision is not _UNCONDITIONAL and current != expected_revision:
            raise ShortDramaSourceConflict(
                f"short drama source revision changed: expected={expected_revision!r}, actual={current!r}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, raw)
    return ShortDramaSource(episode=episode, path=relative, markdown=markdown, revision=_revision(raw))


_UNCONDITIONAL = object()


def replace_short_drama_source(
    manager: _ProjectSourceManager,
    project_name: str,
    episode: int,
    markdown: str,
) -> ShortDramaSource:
    """首次导入兼容入口：显式替换该集源稿，不做 revision 前置条件。"""
    return save_short_drama_source(
        manager,
        project_name,
        episode,
        markdown,
        expected_revision=_UNCONDITIONAL,
    )


__all__ = [
    "ShortDramaSource",
    "ShortDramaSourceConflict",
    "load_short_drama_source",
    "replace_short_drama_source",
    "save_short_drama_source",
    "short_drama_source_relative_path",
]
