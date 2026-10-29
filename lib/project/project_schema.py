"""Strict parsing for the persisted project schema discriminator."""

from __future__ import annotations

from collections.abc import Mapping

CURRENT_PROJECT_SCHEMA_VERSION = 19

# 磁盘项目包含历史创作类型；新建与生成入口另行限制为短剧工厂。
PERSISTED_PROJECT_CONTENT_MODES: frozenset[str] = frozenset({"narration", "drama", "ad"})

#: 创作模式（creation_mode）：表达「剧本从哪里来」，创建即定、之后不可变。
#: ``ai_original`` 是存量与新项目的默认，剧本由 AI 原创链路产出；``source_import`` 的剧本由用户
#: 粘贴的原文经服务端改写生成。它不参与 content_mode / generation_mode 的任何判定。
CREATION_MODES: frozenset[str] = frozenset({"ai_original", "source_import"})
DEFAULT_CREATION_MODE = "ai_original"


def parse_project_schema_version(project: Mapping[str, object]) -> int:
    """Return a canonical non-negative version without coercing corrupt data."""

    raw_version = project.get("schema_version")
    if raw_version is None:
        return 0
    if type(raw_version) is not int or raw_version < 0:
        raise ValueError(f"schema_version must be an integer, got {raw_version!r}")
    return raw_version


def project_schema_is_current(project: Mapping[str, object]) -> bool:
    """Return whether a strictly parsed project uses the current schema."""

    version = parse_project_schema_version(project)
    if version > CURRENT_PROJECT_SCHEMA_VERSION:
        raise ValueError(f"schema_version {version} is newer than supported version {CURRENT_PROJECT_SCHEMA_VERSION}")
    return version == CURRENT_PROJECT_SCHEMA_VERSION


def resolve_creation_mode(project: Mapping[str, object]) -> str:
    """Read the project creation mode, defaulting legacy or unrecognised data to ``ai_original``.

    The field is written explicitly by ``create_project_metadata`` and backfilled by the
    v18→v19 migration. Reads still fall back to the default for archives and demo data that
    have not run the migration yet; invalid values are reported by ``DataValidator`` at the
    archive boundary rather than crashing every reader.
    """

    raw_mode = project.get("creation_mode")
    return raw_mode if isinstance(raw_mode, str) and raw_mode in CREATION_MODES else DEFAULT_CREATION_MODE


__all__ = [
    "CREATION_MODES",
    "CURRENT_PROJECT_SCHEMA_VERSION",
    "DEFAULT_CREATION_MODE",
    "PERSISTED_PROJECT_CONTENT_MODES",
    "parse_project_schema_version",
    "project_schema_is_current",
    "resolve_creation_mode",
]
