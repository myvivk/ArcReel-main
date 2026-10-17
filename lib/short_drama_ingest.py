"""短剧工厂工件的零处理写入与读取。"""

from __future__ import annotations

import base64
import hashlib
import re
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from lib.infra.json_io import atomic_write_bytes
from lib.infra.path_safety import safe_join


class ShortDramaArtifactError(ValueError):
    """工件路径或内容不符合短剧工厂导入契约。"""


class _ArtifactManager(Protocol):
    def get_project_path(self, name: str) -> Path: ...

    def locked_source_mutation(self, project_name: str) -> AbstractContextManager[Path]: ...


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
    if (
        not isinstance(path, str)
        or "\\" in path
        or path.startswith("/")
        or ".." in Path(path).parts
        or Path(path).is_absolute()
    ):
        raise ShortDramaArtifactError("工件路径必须是 source/short_drama 下的安全相对路径")
    for kind, pattern in _ARTIFACT_PATTERNS:
        match = pattern.fullmatch(path)
        if match:
            return kind, int(match.group(1)) if match.lastindex else None
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
    root = safe_join(manager.get_project_path(project_name), "source", "short_drama")
    result: list[ShortDramaArtifact] = []
    for path in sorted(root.rglob("*.md")) if root.exists() else ():
        relative = path.relative_to(root).as_posix()
        try:
            result.append(_artifact_from_bytes(relative, path.read_bytes()))
        except ShortDramaArtifactError:
            continue
    if not any(artifact.kind == "episode" for artifact in result):
        legacy_root = safe_join(manager.get_project_path(project_name), "source", "short_drama")
        for path in sorted(legacy_root.glob("episode_*.md")):
            match = re.fullmatch(r"episode_(\d+)\.md", path.name)
            if match:
                try:
                    result.append(
                        _artifact_from_bytes(f"episodes/episode_{int(match.group(1)):02d}.md", path.read_bytes())
                    )
                except ShortDramaArtifactError:
                    continue
    return result


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
    with manager.locked_source_mutation(project_name) as source_dir:
        for item in prepared:
            path = source_dir / "short_drama" / item.path
            current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            if item.path in expected and current != expected[item.path]:
                raise ShortDramaArtifactError(f"工件已被其他修改更新: {item.path}")
        for item in prepared:
            path = source_dir / "short_drama" / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(path, item.content)
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
    return content.encode("utf-8")


__all__ = [
    "ShortDramaArtifact",
    "ShortDramaArtifactError",
    "classify_short_drama_artifact",
    "decode_artifact_content",
    "ingest_short_drama_artifacts",
    "load_short_drama_artifacts",
]
