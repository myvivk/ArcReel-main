"""本集资产：一集剧本引用到哪些已登记项目资产，以及一键「抽取→登记→生成描述→生成图」。

引用集合只在 :func:`lib.project.episode_asset_references.episode_referenced_assets` 算一次
（集层批量生成资产图、Agent 按集列出待生成资产都取这份），本路由只按集取剧本并把它
落成接口形状。

一键链路（:func:`generate_episode_assets`）把资产页这四步合成一个请求：抽取名字 → 把还没
登记的名字补进项目资产表并落盘 → 给缺描述的资产按剧本生成描述 → 按集范围提交资产图批量
生成。

**抽取来源（D-35 / D-36）**：

1. 本集正式脚本的引用数组（:func:`script_asset_names`）；
2. 本集剧本（工厂源稿 ``source/short_drama/episodes/episode_NN.md``）的 ``[人物]/[场景]/[道具]``
   标记行（:func:`source_markdown_asset_names`）——标记行形如 ``名称｜外观描述``，本身已带外观；
3. 故事规划四类文档——人物圣经 / 全剧大纲 / 连续性台账 / 分集规划
   （:func:`server.services.story_plan_docs.story_plan_asset_names`），补剧本尚未点名、但规划
   已定义的角色 / 场景 / 道具，保证登记完整性。

生成描述夹在登记与生成之间，是因为资产图按描述出图、缺描述会被 ``asset_description_required``
准入挡下；标记行已带外观描述，但它是零散原始文本，需 AI 强化规范成资产图可用的
``description``，不生成描述就是「登记了 N 个、出图 0 张」。生成描述只填空字段，已有描述一律不动。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import APIRouter, HTTPException

from lib.artifacts.artifact_activation import active_artifact_currency_resolver
from lib.artifacts.artifact_manifest import ArtifactManifestError
from lib.generation.generation_queue import get_generation_queue
from lib.infra.api_errors import ApiError, ConflictError, NotFoundError
from lib.project.asset_types import (
    ASSET_SPECS,
    asset_name_comparison_key,
    register_missing_assets,
    resolve_asset_key,
)
from lib.project.episode_asset_references import episode_referenced_assets
from lib.project.project_change_hints import project_change_source
from lib.project.project_manager import ProjectManager, find_episode, get_project_manager
from lib.references.reference_catalog import split_derivative_reference
from lib.script.script_references import iter_reference_sites
from lib.script.short_drama_source import load_short_drama_source
from server.auth import CurrentUser
from server.i18n import Translator
from server.services.admission.asset_sheet_batch import (
    DESCRIPTION_REQUIRED_CODES,
    AssetSheetScope,
    asset_sheet_statuses,
    asset_unit_view,
    load_asset_sheet_batch_plan,
    load_episode_script,
    submit_asset_sheet_batch,
)
from server.services.asset_ai_fill import (
    apply_asset_fill,
    assets_without_description,
    fill_assets,
)
from server.services.story_plan_docs import marker_asset_names, story_plan_asset_names

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


def source_markdown_asset_names(manager: ProjectManager, project_name: str, episode: int) -> dict[str, list[str]]:
    """本集剧本（工厂源稿）``[人物]/[场景]/[道具]`` 标记行写到的名字，按类型去重保序。

    工厂源稿是逐集的定稿脚本：标记行形如 ``名称｜外观描述``，名字自带类型、未登记的也照样
    列出，外观描述同时是生成描述的主素材。没有本集源稿时返回三类空列表。
    """

    source = load_short_drama_source(manager, project_name, episode)
    if source is None:
        return {asset_type: [] for asset_type in EPISODE_ASSET_TYPES}
    return marker_asset_names(source.markdown)


def merge_asset_names(*groups: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """按类型合并多份名字；去重（比较键）保序，先给的来源排在前面。"""

    merged: dict[str, list[str]] = {asset_type: [] for asset_type in EPISODE_ASSET_TYPES}
    for group in groups:
        for asset_type, names in group.items():
            if asset_type not in merged:
                continue
            seen = {asset_name_comparison_key(name) for name in merged[asset_type]}
            for name in names:
                key = asset_name_comparison_key(name)
                if not key or key in seen:
                    continue
                seen.add(key)
                merged[asset_type].append(name)
    return merged


def episode_asset_names(
    manager: ProjectManager,
    project_name: str,
    episode: int,
    script: Mapping[str, Any] | None,
) -> dict[str, list[str]]:
    """本集抽取到的全部资产名：剧本（正式脚本引用数组 + 工厂源稿标记行）与故事规划。

    三个来源互补，合并顺序即优先级：正式脚本给出本集实际用到的名字；工厂源稿标记行补上剧本
    里还没落进引用数组的名字（标记行已带外观描述）；故事规划补上剧本尚未点名、但规划已定义
    的角色 / 场景 / 道具——没登记就谈不上生成描述与出图，登记完整性优先。
    """

    script_names = script_asset_names(script) if script is not None else {}
    return merge_asset_names(
        script_names,
        source_markdown_asset_names(manager, project_name, episode),
        story_plan_asset_names(manager.get_project_path(project_name), episode=episode),
    )


def episode_assets_plan(manager: ProjectManager, project_name: str, episode: int) -> dict[str, Any]:
    """一键的本批清单预估（只读、不落盘）：将处理多少资产、预计多少次文本 / 图像模型调用。

    - 文本模型：只给**描述为空**的资产生成描述，已有描述的一律不动、不占调用；
    - 图像模型：资产图未就绪（缺图或过期）的资产各一张；仍缺描述的进不了队列，不计。

    预估与实际共用 :func:`episode_asset_names` 的抽取口径，故弹窗里报的数就是点「确定」后
    要跑的数。
    """

    project = manager.load_project(project_name)
    script = load_episode_script(manager, project_name, episode)
    names = episode_asset_names(manager, project_name, episode, script)
    by_type = {asset_type: len(names.get(asset_type, [])) for asset_type in EPISODE_ASSET_TYPES}
    try:
        resolver = active_artifact_currency_resolver(manager.get_project_path(project_name), project)
    except ArtifactManifestError:
        logger.warning("episode assets plan: artifact manifest unreadable for project %s", project_name)
        resolver = None
    status_by_unit = {
        (row["asset_type"], row["name"]): row["status"] for row in asset_sheet_statuses(project, resolver)
    }
    text_calls = 0
    image_calls = 0
    for asset_type, group in names.items():
        spec = ASSET_SPECS.get(asset_type)
        bucket = project.get(spec.bucket_key) if spec is not None else None
        for name in group:
            key = resolve_asset_key(bucket, name) if isinstance(bucket, dict) else None
            entry = bucket.get(key) if isinstance(bucket, dict) and key is not None else None
            has_description = isinstance(entry, dict) and bool(str(entry.get("description") or "").strip())
            if not has_description:
                text_calls += 1
            if status_by_unit.get((asset_type, key or name), "missing") != "current":
                image_calls += 1
    return {
        "total": sum(by_type.values()),
        "by_type": by_type,
        "text_calls": text_calls,
        "image_calls": image_calls,
    }


def register_episode_assets(project_name: str, episode: int) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
    """把本集抽取到、还没登记的名字补进资产表并落盘，返回 ``(本次登记, 冲突)``。

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
    names = episode_asset_names(manager, project_name, episode, script)
    if script is None and not any(names.values()):
        # 既没有正式脚本、工厂源稿与故事规划也抽不出名字：本集无剧本可依。
        raise ConflictError("episode_assets_no_script", episode=episode)
    registered: dict[str, list[str]] = {}
    conflicts: list[dict[str, str]] = []

    def _mutate(current: dict[str, Any]) -> None:
        new_assets, new_conflicts = register_missing_assets(current, names)
        registered.update(new_assets)
        conflicts.extend(new_conflicts)

    with project_change_source("webui"):
        manager.update_project(project_name, _mutate)
    return registered, conflicts


async def fill_episode_asset_descriptions(
    project_name: str, episode: int
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """给本集引用到、描述为空的资产生成描述，返回 ``(已生成, 生成失败)``。

    生成依据是「剧本（工厂源稿）> 故事规划 > 题材补全」（D-35）：工厂源稿标记行里已写的外观
    是基础，本集剧本里该资产出场的场景与台词用于强化，故事规划补跨集背景，再按
    ``shared/asset_description_rule`` 规范成资产图可用的 ``description``。只填尚为空的字段，
    已有描述一律不动——绝不覆盖人工写好的内容。

    N 个资产并发调模型、但只在一次项目锁里批量写回：逐条落盘会互相覆盖，也会把项目写 N 遍。
    """

    manager = get_project_manager()
    project = await asyncio.to_thread(manager.load_project, project_name)
    script = load_episode_script(manager, project_name, episode)
    names = episode_asset_names(manager, project_name, episode, script)
    if script is None and not any(names.values()):
        return [], []
    targets = assets_without_description(project, names)
    if not targets:
        return [], []
    fills, failed = await fill_assets(
        project, manager.get_project_path(project_name), targets, project_name, episode=episode
    )
    if not fills:
        return [], failed
    filled: list[dict[str, str]] = []

    def _mutate(current: dict[str, Any]) -> None:
        for (asset_type, asset_name), fill in fills.items():
            spec = ASSET_SPECS.get(asset_type)
            if spec is None:
                continue
            bucket = current.get(spec.bucket_key)
            if not isinstance(bucket, dict):
                continue
            entry = bucket.get(asset_name)
            if not isinstance(entry, dict):
                continue
            if "description" in apply_asset_fill(entry, asset_type, asset_name, fill):
                filled.append({"asset_type": asset_type, "name": asset_name})

    with project_change_source("webui"):
        await asyncio.to_thread(manager.update_project, project_name, _mutate)
    return filled, failed


@router.post("/projects/{name}/episodes/{episode}/generate-assets")
async def generate_episode_assets(name: str, episode: int, user: CurrentUser, _t: Translator) -> dict[str, Any]:
    """一键：抽取本集剧本引用 → 补登记未登记资产 → 生成描述 → 按集提交资产图批量生成。

    提交不等待结果，成员任务的终态由界面按任务跟踪；生成描述失败或仍缺描述的资产进不了这一批，
    逐条回执在 ``members`` 的 ``problem_code`` 里（``asset_description_required`` 等），
    同时另给一份 ``without_description`` 供界面直接提示「描述没补上」。

    **只出第 3 步已生成描述的资产**：缺描述会被 ``asset_description_required`` 准入挡下，
    留在外面的项保持「缺描述」、不静默吞掉，用户补描述后可单独再生成资产图。
    """

    try:
        registered, conflicts = await asyncio.to_thread(register_episode_assets, name, episode)
        filled, fill_failed = await fill_episode_asset_descriptions(name, episode)
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
        "description_filled": filled,
        "description_fill_failed": fill_failed,
        "conflicts": conflicts,
        "without_description": [
            {key: member[key] for key in ("unit_id", "asset_type", "name", "derivative")}
            for member in members
            if member["problem_code"] in DESCRIPTION_REQUIRED_CODES
        ],
    }


@router.get("/projects/{name}/episodes/{episode}/referenced-assets")
async def get_episode_referenced_assets(name: str, episode: int, _t: Translator) -> dict[str, Any]:
    """本集剧本引用到的已登记项目资产与一键的本批清单预估。

    ``assets`` 是「本集引用 ∩ 已登记」，未登记的名字要等登记后才进得来；
    ``plan`` 是本批清单的**预估**（只读、不落盘）：将处理多少资产、预计多少次文本 / 图像模型
    调用——弹窗据此把范围与花费摆给用户确认。项目或集不存在 404；没有本集剧本（正式脚本与
    工厂源稿都没有）时 ``has_script`` 为 False、列表为空。
    """

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
            "has_script": script is not None or load_short_drama_source(manager, name, episode) is not None,
            "assets": referenced_asset_items(project, script),
            "plan": episode_assets_plan(manager, name, episode),
        }

    try:
        return await asyncio.to_thread(_sync)
    except (HTTPException, ApiError):
        raise
    except Exception as exc:
        logger.exception("Failed to load episode referenced assets: project=%s episode=%s", name, episode)
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc
