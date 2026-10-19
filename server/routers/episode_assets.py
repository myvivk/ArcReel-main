"""本集资产：一集正式脚本引用到哪些已登记项目资产，以及一键「抽取→登记→生成图」。

引用集合只在 :func:`lib.project.episode_asset_references.episode_referenced_assets` 算一次
（集层批量生成资产图、Agent 按集列出待生成资产都取这份），本路由只按集取正式脚本并把它
落成接口形状。

一键链路（:func:`generate_episode_assets`）把资产页这三步合成一个请求：从本集正式脚本的
引用数组抽取名字 → 把还没登记的名字补进项目资产表并落盘 → 按集范围提交资产图批量生成。
登记与生成之间不预览：抽取出来的名字就是本集剧本写到的名字，登记只是补条目、不动已登记
资产，而生成前要给出名单与预估费用就必须先登记——本集引用是只读不出来未登记名字的。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, HTTPException

from lib.generation.generation_queue import get_generation_queue
from lib.infra.api_errors import ApiError, ConflictError, NotFoundError
from lib.project.asset_types import ASSET_SPECS, asset_name_comparison_key, register_missing_assets
from lib.project.episode_asset_references import episode_referenced_assets
from lib.project.project_change_hints import project_change_source
from lib.project.project_manager import find_episode, get_project_manager
from lib.references.reference_catalog import split_derivative_reference
from lib.script.script_references import iter_reference_sites
from server.auth import CurrentUser
from server.i18n import Translator
from server.services.admission.asset_sheet_batch import (
    DESCRIPTION_REQUIRED_CODES,
    AssetSheetScope,
    asset_unit_view,
    load_asset_sheet_batch_plan,
    load_episode_script,
    submit_asset_sheet_batch,
)

logger = logging.getLogger(__name__)
router = APIRouter()

#: 资产页 tab 的三类；商品不进本集资产视图（本集剧本不引用商品）。
EPISODE_ASSET_TYPES: tuple[str, ...] = ("character", "scene", "prop")


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


def script_asset_names(script: Mapping[str, Any]) -> dict[str, list[str]]:
    """本集脚本在各类型引用数组里写到的名字，按类型去重保序；未登记的也照样列出。

    只认引用数组（``characters_in_scene`` / ``scenes`` / ``props`` 等各骨架的写法由
    ``ASSET_SPECS.reference_list_fields`` 给出），名字因此自带类型、可以据此登记。
    台词说话人与正文 ``@[名称]`` 记号不算：说话人可以是未登记的群演，正文记号只指认本条
    已声明的资产——两者都不构成「要登记这个资产」。

    引用数组写 ``本体/衍生`` 时只登记本体：衍生要有外观变化描述才成立，剧本里写了它也不
    等于要新增一套外观，本体先有条目、衍生再由用户按需添加。
    """

    names: dict[str, list[str]] = {}
    for asset_type in EPISODE_ASSET_TYPES:
        collected: list[str] = []
        seen: set[str] = set()
        for kind, value, _write in iter_reference_sites(
            script, frozenset(ASSET_SPECS[asset_type].reference_list_fields), with_speaker=False
        ):
            if kind != "name":
                continue
            name = split_derivative_reference(value.strip())[0].strip()
            key = asset_name_comparison_key(name)
            if not key or key in seen:
                continue
            seen.add(key)
            collected.append(name)
        names[asset_type] = collected
    return names


def register_episode_assets(project_name: str, episode: int) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
    """把本集正式脚本引用到、还没登记的名字补进资产表并落盘，返回 ``(本次登记, 冲突)``。

    登记在项目锁里重读项目后做（:meth:`ProjectManager.update_project`），已登记的条目一律
    不动——描述与资产图是用户资产；名字被别的类型占着时跳过并记进冲突，不中断这一批。
    """

    manager = get_project_manager()
    if not manager.project_exists(project_name):
        raise NotFoundError("project_not_found", name=project_name)
    project = manager.load_project(project_name)
    if find_episode(project, episode) is None:
        raise NotFoundError("episode_not_found", episode=episode)
    script = load_episode_script(manager, project_name, episode)
    if script is None:
        raise ConflictError("episode_assets_no_script", episode=episode)
    names = script_asset_names(script)
    registered: dict[str, list[str]] = {}
    conflicts: list[dict[str, str]] = []

    def _mutate(current: dict[str, Any]) -> None:
        new_assets, new_conflicts = register_missing_assets(current, names)
        registered.update(new_assets)
        conflicts.extend(new_conflicts)

    with project_change_source("webui"):
        manager.update_project(project_name, _mutate)
    return registered, conflicts


@router.post("/projects/{name}/episodes/{episode}/generate-assets")
async def generate_episode_assets(name: str, episode: int, user: CurrentUser, _t: Translator) -> dict[str, Any]:
    """一键：抽取本集剧本引用 → 补登记未登记资产 → 按集提交资产图批量生成。

    提交不等待结果，成员任务的终态由界面按任务跟踪；缺描述的资产登记了但进不了这一批，
    逐条回执在 ``members`` 的 ``problem_code`` 里（``asset_description_required`` 等），
    同时另给一份 ``without_description`` 供界面直接提示「先补描述」。
    """

    try:
        registered, conflicts = await asyncio.to_thread(register_episode_assets, name, episode)
        _project, plan = await load_asset_sheet_batch_plan(
            get_project_manager(),
            get_generation_queue(),
            project_name=name,
            scope=AssetSheetScope(episode_id=episode),
            user_id=user.id,
        )
        batch, enqueued = await submit_asset_sheet_batch(
            plan,
            project_name=name,
            queue=get_generation_queue(),
            source="webui",
            user_id=user.id,
        )
    except (HTTPException, ApiError):
        raise
    except Exception as exc:
        logger.exception("Failed to generate episode assets: project=%s episode=%s", name, episode)
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc

    task_by_unit = {task.unit_id: task for task in enqueued if task.unit_id is not None}
    members = [
        {
            **asset_unit_view(plan, member.unit_id),
            "task_id": task_by_unit[member.unit_id].task_id if member.unit_id in task_by_unit else None,
            "deduped": member.deduped,
            "status": member.status,
            "problem_code": member.problem.code if member.problem is not None else None,
        }
        for member in batch.members
    ]
    return {
        "batch_id": batch.batch_id,
        "members": members,
        "registered": registered,
        "conflicts": conflicts,
        "without_description": [
            {key: member[key] for key in ("unit_id", "asset_type", "name", "derivative")}
            for member in members
            if member["problem_code"] in DESCRIPTION_REQUIRED_CODES
        ],
    }


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
