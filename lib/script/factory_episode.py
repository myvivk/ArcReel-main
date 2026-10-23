"""Production admission for visual state derived from factory Markdown."""

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lib.infra.api_errors import ConflictError
from lib.script.short_drama_source import short_drama_source_relative_path


def require_factory_script(
    project_path: Path,
    project: Mapping[str, Any],
    script: Mapping[str, Any],
    episode: int,
) -> None:
    """Require the factory source and its matching visual derivation before production."""
    if project.get("content_mode") != "drama" or project.get("generation_mode") != "storyboard":
        raise ConflictError("short_drama_factory_only")
    if script.get("content_mode") != "drama" or not isinstance(script.get("scenes"), list):
        raise ConflictError("short_drama_factory_only")
    path = project_path / short_drama_source_relative_path(episode)
    if path.is_symlink():
        raise ConflictError("short_drama_factory_only")
    try:
        source = path.read_bytes()
    except OSError as exc:
        raise ConflictError("short_drama_factory_only") from exc
    metadata = script.get("short_drama_meta")
    if not source.strip() or not isinstance(metadata, Mapping):
        raise ConflictError("short_drama_factory_only")
    if metadata.get("source_revision") != hashlib.sha256(source).hexdigest():
        raise ConflictError("short_drama_factory_only")
