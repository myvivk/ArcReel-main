"""Resolve available character reference audio without a project generation route."""

from pathlib import Path

from lib.project.asset_types import BUCKET_KEY, normalize_asset_bucket
from lib.speech.audio_utils import resolve_audio_ref_path

ASSET_AUDIO_SUBDIR = "characters/refs_audio"


def resolve_reference_audio_paths(project: dict, project_path: Path) -> dict[str, Path]:
    """Return canonical character names and existing audio files inside refs_audio."""
    audio_refs_dir = project_path / ASSET_AUDIO_SUBDIR
    resolved: dict[str, Path] = {}
    for name, item in normalize_asset_bucket(project.get(BUCKET_KEY["character"])).items():
        if not isinstance(item, dict):
            continue
        path = resolve_audio_ref_path(project_path, audio_refs_dir, item.get("reference_audio"))
        if path is not None and path.exists():
            resolved[name] = path
    return resolved
