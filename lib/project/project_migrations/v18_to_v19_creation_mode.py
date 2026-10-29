"""v18→v19 迁移：项目补写创作模式 ``creation_mode``。

新字段表达「剧本从哪里来」：``ai_original``（默认，剧本由 AI 原创链路产出）或
``source_import``（剧本由用户粘贴的原文经服务端改写生成）。v18 及更早的代码从不写这个字段，
所以存量项目一律是 AI 原创，缺失或空值在此补写 ``ai_original``；已有的非空值原样保留（包括
非法值——那是手工改坏的数据，由 ``DataValidator`` 在归档边界报告，迁移不替用户猜测）。

只改 ``project.json``，不触碰剧本、源文与产物清单：创作模式不进任何产物的生成依据，改写不让
任何登记过期。备份由 runner 负责。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lib.artifacts.formal_write import project_metadata_lock
from lib.infra.json_io import atomic_write_json
from lib.project.project_schema import DEFAULT_CREATION_MODE, parse_project_schema_version

TARGET_SCHEMA_VERSION = 19


def migrate_project_dict(project: Mapping[str, Any]) -> dict[str, Any]:
    """纯函数：缺失或空值补写 ``ai_original``，已有非空值原样保留。幂等。

    不改 ``schema_version``（由文件级 migrate 提交时写入）。
    """

    migrated = dict(project)
    if not migrated.get("creation_mode"):
        migrated["creation_mode"] = DEFAULT_CREATION_MODE
    return migrated


def migrate_v18_to_v19(project_dir: Path) -> None:
    """v18→v19 文件级迁移。单次原子写，崩溃可重试（要么旧值要么新值，无半态）。"""

    project_file = Path(project_dir) / "project.json"
    if not project_file.is_file():
        return
    with project_metadata_lock(project_dir):
        project = json.loads(project_file.read_bytes())
        if not isinstance(project, dict):
            raise ValueError("project.json 必须是对象")
        if parse_project_schema_version(project) >= TARGET_SCHEMA_VERSION:
            return
        atomic_write_json(project_file, {**migrate_project_dict(project), "schema_version": TARGET_SCHEMA_VERSION})


__all__ = ["TARGET_SCHEMA_VERSION", "migrate_project_dict", "migrate_v18_to_v19"]
