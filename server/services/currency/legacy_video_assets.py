"""Finalize and restore media for historical reference-video projects."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lib.artifacts.version_manager import VersionManager
from lib.infra.async_thread import run_sync_transaction
from lib.infra.thumbnail import extract_video_thumbnail
from lib.project.project_manager import get_project_manager
from lib.script.script_editor import ScriptEditError

logger = logging.getLogger(__name__)


def apply_unit_video_assets(
    script: dict,
    resource_id: str,
    *,
    video_uri: str | None,
    thumb_rel: str | None,
    generated_at: str | None = None,
) -> str | None:
    """在剧本 dict 上写回 unit.generated_assets（video_clip / video_uri / video_thumbnail / status）。

    生成 finalize 与版本还原共用，保证两条路径写出的字段口径一致。所有创作类型的 unit
    都位于 ``video_units``。新结果不含 video_uri / 缩略图时清空旧值，避免指向过期 URI /
    已删除文件。写回失败必须让调用方可见、finalize 不能在剧本未更新时静默成功，
    且两种失败要可区分：unit 不存在抛 KeyError（还原侧跨集同步把它当正常跳过），
    unit 列表结构损坏抛 ScriptEditError（还原侧按脏脚本 warning 降级）。

    ``generated_at`` 缺省戳当前时间（生成 finalize）；版本还原传入被还原版本的入库时间，
    使「片段是否早于当前参考音频设置」按还原回来的旧内容成立，而不是被还原动作洗成最新。

    迁移保留下来的旧 ``source_signature`` 是惰性历史数据，本函数不读取、比较或新增它。
    """
    units = script.get("video_units")
    if not isinstance(units, list):
        raise ScriptEditError("video_units 必须是 list", key="script_edit_unit_lists_invalid")
    for u in units:
        if not isinstance(u, dict) or u.get("unit_id") != resource_id:
            continue
        ga = u.setdefault("generated_assets", {})
        if not isinstance(ga, dict):
            raise ScriptEditError("generated_assets 必须是 dict", key="script_edit_generated_assets_invalid")
        ga["video_clip"] = f"reference_videos/{resource_id}.mp4"
        ga["video_generated_at"] = generated_at or datetime.now(UTC).isoformat()
        if video_uri:
            ga["video_uri"] = video_uri
        else:
            ga.pop("video_uri", None)
        if thumb_rel:
            ga["video_thumbnail"] = thumb_rel
        else:
            ga.pop("video_thumbnail", None)
        ga["status"] = "completed"
        return None
    raise KeyError(resource_id)


async def finalize_reference_video_unit(
    *,
    project_name: str,
    script_file: str,
    project_path: Path,
    resource_id: str,
    output_path: Path,
    version: int,
    video_uri: str | None,
    versions: VersionManager,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Normal + resume + 上传共用：抽缩略图、写 unit.generated_assets、返回 result dict。

    迁移保留的旧来源签名不再参与 finalize。
    """
    warnings = warnings if warnings is not None else []

    thumb_dir = project_path / "reference_videos" / "thumbnails"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = thumb_dir / f"{resource_id}.jpg"
    if await extract_video_thumbnail(output_path, thumb_path):
        thumb_rel: str | None = f"reference_videos/thumbnails/{resource_id}.jpg"
    else:
        thumb_path.unlink(missing_ok=True)
        thumb_rel = None

    def _update_unit_assets() -> str | None:
        pm = get_project_manager()
        # 资产回写热路径：只动 unit.generated_assets，结构不可能因此变坏，豁免结构校验。
        with pm.locked_script(project_name, script_file, validate=False) as script:
            return apply_unit_video_assets(script, resource_id, video_uri=video_uri, thumb_rel=thumb_rel)

    await run_sync_transaction(_update_unit_assets)

    def _latest_created_at() -> str | None:
        # 与上面的档案补写读同一个 versions.json，同样在产物与剧本落盘之后：文件不可读时
        # 让结果少一个时间戳，而不是把已成功的付费生成报成失败——只吞掉护栏才成立。
        try:
            history = versions.get_versions("reference_videos", resource_id) or {}
        except Exception:
            logger.warning("读取版本记录取入库时间失败 resource_id=%s", resource_id, exc_info=True)
            return None
        records = history.get("versions") or []
        if not records:
            return None
        return records[-1].get("created_at")

    created_at = await asyncio.to_thread(_latest_created_at)

    return {
        "version": version,
        "file_path": f"reference_videos/{resource_id}.mp4",
        "created_at": created_at,
        "resource_type": "reference_videos",
        "resource_id": resource_id,
        "video_uri": video_uri,
        "warnings": warnings,
    }
