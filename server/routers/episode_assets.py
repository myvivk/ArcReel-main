"""本集资产：一集正式脚本引用到哪些已登记项目资产。

引用集合只在 :func:`lib.project.episode_asset_references.episode_referenced_assets` 算一次
（集层批量生成资产图、Agent 按集列出待生成资产都取这份），本路由只按集取正式脚本并把它
落成接口形状。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from lib.infra.api_errors import ApiError, NotFoundError
from lib.project.asset_types import ASSET_SPECS
from lib.project.episode_asset_references import episode_referenced_assets
from lib.project.project_manager import get_project_manager
from server.i18n import Translator

logger = logging.getLogger(__name__)
router = APIRouter()


def referenced_asset_items(project: dict[str, Any], script: dict[str, Any] | None) -> list[dict[str, Any]]:
    """本集脚本引用到的资产与它们的资产条目；没有正式脚本时为空。

    只列本体：衍生资产图是对本体资产图的一次编辑，集页按本体成卡（卡上带衍生数），集范围的
    批量生成由服务端自己展开衍生。
    """

    if script is None:
        return []
    items: list[dict[str, Any]] = []
    for ref in sorted(episode_referenced_assets(project, script), key=lambda asset: (asset.asset_type, asset.name)):
        if ref.owner is not None:
            continue
        spec = ASSET_SPECS.get(ref.asset_type)
        if spec is None:
            continue
        bucket = project.get(spec.bucket_key)
        entry = bucket.get(ref.name) if isinstance(bucket, dict) else None
        if not isinstance(entry, dict):
            continue
        items.append({"asset_type": ref.asset_type, "asset_name": ref.name, "asset": entry})
    return items


@router.get("/projects/{name}/episodes/{episode}/referenced-assets")
async def get_episode_referenced_assets(name: str, episode: int, _t: Translator) -> dict[str, Any]:
    """本集正式脚本引用到的已登记项目资产；项目或集不存在 404，还没有正式脚本时列表为空。"""

    def _sync() -> dict[str, Any]:
        manager = get_project_manager()
        if not manager.project_exists(name):
            raise NotFoundError("project_not_found", name=name)
        project = manager.load_project(name)
        entry = next((item for item in project.get("episodes", []) if item.get("episode") == episode), None)
        if entry is None:
            raise NotFoundError("episode_not_found", episode=episode)
        script_file = entry.get("script_file")
        script: dict[str, Any] | None = None
        if isinstance(script_file, str) and script_file:
            try:
                script = manager.load_script_readonly(name, script_file)
            except FileNotFoundError:
                # 集登记了剧本名但文件不在（还没生成或已删掉）：按没有正式脚本处理。
                script = None
        return {
            "episode": episode,
            "has_script": script is not None,
            "assets": referenced_asset_items(project, script),
        }

    try:
        return await asyncio.to_thread(_sync)
    except (HTTPException, ApiError):
        raise
    except Exception as exc:
        logger.exception("Failed to load episode referenced assets: project=%s episode=%s", name, episode)
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc
