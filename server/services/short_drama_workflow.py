"""短剧工厂 Markdown 到分镜结构的机械导入工作流（不调用文本模型）。"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lib.db.base import DEFAULT_USER_ID
from lib.episode.episode_paths import episode_script_filename
from lib.generation.generation_queue import get_generation_queue
from lib.generation.generation_queue_client import wait_for_task
from lib.infra.async_thread import run_sync_transaction
from lib.infra.content_digest import canonical_json_digest
from lib.project.project_manager import ProjectManager, ScriptWriteConflict
from lib.script.mechanical_storyboard import (
    MECHANICAL_STORYBOARD_VERSION,
    MechanicalStoryboard,
    StoryboardSplitError,
    split_episode,
)
from lib.script.script_models import DramaEpisodeScript
from lib.script.short_drama_import import (
    assets_missing_description,
    beat_catalog,
    build_mechanical_scenes,
    parse_short_drama_episode,
    unregistered_handled_props,
)
from lib.script.short_drama_source import (
    ShortDramaSourceConflict,
    load_short_drama_source,
    short_drama_source_relative_path,
)
from server.agent_runtime.tool_progress import (
    ProgressReporter,
)
from server.services.admission.asset_sheet_batch import (
    AssetSheetScope,
    load_asset_sheet_batch_plan,
    submit_asset_sheet_batch,
)

logger = logging.getLogger(__name__)


class ShortDramaWorkflowError(ValueError):
    def __init__(self, code: str, detail: str, params: dict[str, Any] | None = None) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        #: 可选的机器可读补充（如失败路径的 ``rounds``）；由工具层原样透传给 Agent。
        self.params = params


@dataclass(frozen=True, slots=True)
class ShortDramaImportResult:
    episode: int
    scenes: int
    source_path: str
    source_revision: str
    mode: str = "mechanical"
    splitter_version: int = MECHANICAL_STORYBOARD_VERSION
    #: 结构落盘后仍待编写分镜提示词的条目数。
    pending_authoring: int = 0
    #: 本次是否直接复用了既有结构（源稿与切分器版本都没变）。
    reused: bool = False
    declared_seconds: int | None = None
    actual_seconds: int | None = None
    duration_shortfall: bool | None = None
    #: 本次按分镜重切（部分重生成）的分镜数；整体分镜为 None。
    regenerated_scenes: int | None = None
    #: 机械分镜的可修正告警（如多场景块场景归属歧义）。
    warnings: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        result = {
            "episode": self.episode,
            "scenes": self.scenes,
            "source_path": self.source_path,
            "source_revision": self.source_revision,
            "mode": self.mode,
            "splitter_version": self.splitter_version,
            "pending_authoring": self.pending_authoring,
            "reused": self.reused,
            "warnings": list(self.warnings),
        }
        if self.regenerated_scenes is not None:
            result["regenerated_scenes"] = self.regenerated_scenes
        if self.declared_seconds is not None:
            result.update(
                declared_seconds=self.declared_seconds,
                actual_seconds=self.actual_seconds,
                duration_shortfall=self.duration_shortfall,
            )
        return result


def _project_registered_names(project: Mapping[str, Any]) -> dict[str, set[str]]:
    """项目资产表全量登记名（按类型）。token 硬闸的「已登记」判定用全量，不用本集子集。"""
    return {
        "character": {str(name) for name in (project.get("characters") or {}) if str(name).strip()},
        "scene": {str(name) for name in (project.get("scenes") or {}) if str(name).strip()},
        "prop": {str(name) for name in (project.get("props") or {}) if str(name).strip()},
    }


def _require_episode(project: dict[str, Any], episode: int) -> None:
    if project.get("content_mode") != "drama" or project.get("generation_mode", "storyboard") != "storyboard":
        raise ShortDramaWorkflowError("invalid_request", "机械分镜仅适用于短剧工厂制作路线")
    episodes = project.get("episodes")
    if not isinstance(episodes, list) or not any(
        isinstance(entry, dict) and entry.get("episode") == episode for entry in episodes
    ):
        raise ShortDramaWorkflowError("episode_not_found", f"第 {episode} 集不存在")


def script_has_media_artifacts(script: dict[str, Any]) -> bool:
    for scene in script.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        generated = scene.get("generated_assets")
        if isinstance(generated, dict) and any(generated.get(key) for key in ("storyboard_image", "video_clip")):
            return True
    return False


def _load_current_script(projects: ProjectManager, project_name: str, episode: int) -> dict[str, Any] | None:
    """读本集当前正式脚本；没有脚本文件时返回 None。"""
    project = projects.load_project(project_name)
    entry = next(
        (item for item in project.get("episodes") or [] if isinstance(item, dict) and item.get("episode") == episode),
        None,
    )
    script_file = entry.get("script_file") if isinstance(entry, dict) else None
    if not isinstance(script_file, str) or not script_file:
        return None
    try:
        return projects.load_script(project_name, script_file)
    except FileNotFoundError:
        return None


async def _generate_auto_prop_sheets(project_name: str, projects: ProjectManager, names: Sequence[str]) -> None:
    """为自动登记的道具生成白底资产图，并等到任务终态。

    资产图是分镜/宫格参考图的必要输入；只登记不生成，下一步仍会在参考图准入被拦。
    这里复用资产图批量服务与队列等待，不另造生图路径。
    """

    if not names:
        return
    queue = get_generation_queue()
    _, plan = await load_asset_sheet_batch_plan(
        projects,
        queue,
        project_name=project_name,
        scope=AssetSheetScope(asset_type="prop", names=tuple(names)),
        user_id=DEFAULT_USER_ID,
    )
    if not plan.target_ids:
        return
    _, enqueued = await submit_asset_sheet_batch(
        plan,
        project_name=project_name,
        queue=queue,
        source="webui",
        user_id=DEFAULT_USER_ID,
    )
    for item in enqueued:
        task = await wait_for_task(item.task_id, queue=queue)
        if task.get("status") != "succeeded":
            reason = task.get("error_message") or task.get("error") or item.task_id
            raise ShortDramaWorkflowError("short_drama_assets_not_ready", f"自动生成道具资产图失败：{reason}")


def _storyboard_pending_ids(script: Mapping[str, Any]) -> list[str]:
    """结构落盘后仍缺分镜提示词的条目 id，按脚本顺序。"""
    pending: list[str] = []
    for scene in script.get("scenes") or []:
        if not isinstance(scene, Mapping):
            continue
        prompt = scene.get("image_prompt")
        if scene.get("pending_authoring") is True or not prompt:
            scene_id = str(scene.get("scene_id") or "")
            if scene_id:
                pending.append(scene_id)
    return pending


def _scene_prompt_key(scene: Mapping[str, Any]) -> tuple[str, int]:
    """结构指纹：原文锚 + 时长都相同才允许沿用已有提示词。"""
    return str(scene.get("source_text") or ""), int(scene.get("duration_seconds") or 0)


def _preserve_unchanged_scenes(
    script: dict[str, Any],
    existing: dict[str, Any],
    *,
    reset_ids: Collection[str],
) -> None:
    """重切后整体沿用结构未变的分镜（含提示词、媒体绑定与手改）；点名分镜回到待编写。

    判据是「原文锚 + 时长」：两者都相同即同一段 beat，结构必然一致，直接保留旧条目；
    结构变了的分镜用新条目，不把对不上结构的提示词或媒体绑定带过来。
    """
    preserved: dict[tuple[str, int], dict[str, Any]] = {}
    for scene in existing.get("scenes") or []:
        if isinstance(scene, Mapping):
            preserved[_scene_prompt_key(scene)] = dict(scene)
    scenes: list[Any] = []
    for scene in script.get("scenes") or []:
        if not isinstance(scene, dict):
            scenes.append(scene)
            continue
        scene_id = str(scene.get("scene_id") or "")
        old = preserved.get(_scene_prompt_key(scene))
        if old is not None and scene_id not in reset_ids:
            merged = copy.deepcopy(old)
            merged["scene_id"] = scene_id
            scenes.append(merged)
        else:
            scenes.append(scene)
    script["scenes"] = scenes


async def import_short_drama_episode(
    *,
    project_name: str,
    episode: int,
    markdown: str,
    projects: ProjectManager,
    expected_source_revision: str,
    confirm_artifacts: bool = False,
    force_regenerate: bool = False,
    regenerate_scene_ids: Sequence[str] = (),
    planning_strength: int | None = None,
    on_round: ProgressReporter | None = None,
    session_id: str | None = None,
) -> ShortDramaImportResult:
    """从已入库的工厂源稿机械生成分镜结构，不调用文本模型。

    分镜边界、时长、出场资产、逐字口播与原文锚全部由
    :mod:`lib.script.mechanical_storyboard` 确定性推导；``image_prompt`` 留空并置
    ``pending_authoring``，交给下一步「分镜提示词编写」消费该产物来写。

    ``planning_strength`` / ``on_round`` / ``session_id`` 只为兼容旧调用方保留，机械分镜
    不再有规划强度与返修轮次。``regenerate_scene_ids`` 非空时按同一份源稿重切，只把这些
    分镜的提示词清回待编写；未点名的分镜在结构（原文锚 + 时长）不变时保留已有提示词。
    """

    del planning_strength, on_round, session_id
    try:
        project = await asyncio.to_thread(projects.load_project, project_name)
    except FileNotFoundError as exc:
        raise ShortDramaWorkflowError("project_not_found", str(exc)) from exc
    _require_episode(project, episode)

    current_source = await asyncio.to_thread(load_short_drama_source, projects, project_name, episode)
    if current_source is None or current_source.path != short_drama_source_relative_path(episode):
        raise ShortDramaWorkflowError("short_drama_source_missing", "请先通过工件入库提交本集 Markdown 源稿")
    if current_source.revision != expected_source_revision or current_source.markdown != markdown:
        raise ShortDramaWorkflowError("short_drama_source_conflict", "Markdown 源稿已更新，请刷新后重试")

    existing = await asyncio.to_thread(_load_current_script, projects, project_name, episode)
    if not confirm_artifacts and existing is not None and script_has_media_artifacts(existing):
        raise ShortDramaWorkflowError(
            "short_drama_reimport_confirmation_required",
            "本集已有分镜图或视频，重新分镜后这些产物会变为过期状态",
        )

    warnings: list[dict[str, Any]] = []
    reused = False
    regenerated: int | None = None
    try:
        parsed = parse_short_drama_episode(markdown)
        beats = beat_catalog(parsed)
        if not beats:
            raise ShortDramaWorkflowError("short_drama_ai_planning_failed", "源稿没有可分镜的动作或台词")
        auto_props = unregistered_handled_props(project, markdown, parsed=parsed)
        if auto_props:
            try:
                await asyncio.to_thread(
                    projects.add_props_batch,
                    project_name,
                    {
                        name: {
                            "description": (f"源稿动作里被手持/交接的道具「{name}」。外观以分镜描述与后续资产图为准。")
                        }
                        for name in auto_props
                    },
                )
            except ValueError as exc:
                raise ShortDramaWorkflowError("short_drama_assets_not_ready", f"自动登记道具失败：{exc}") from exc
            project = await asyncio.to_thread(projects.load_project, project_name)
            try:
                await _generate_auto_prop_sheets(project_name, projects, auto_props)
            except ShortDramaWorkflowError:
                raise
            except Exception as exc:
                raise ShortDramaWorkflowError("short_drama_assets_not_ready", f"自动生成道具资产图失败：{exc}") from exc
            project = await asyncio.to_thread(projects.load_project, project_name)
        missing_assets = assets_missing_description(project, markdown)
        if missing_assets:
            raise ShortDramaWorkflowError(
                "short_drama_assets_not_ready",
                "请先完成资产步骤，以下资产尚未登记或缺少描述：" + "、".join(item["name"] for item in missing_assets),
            )

        existing_meta = existing.get("short_drama_meta") if existing else None
        options = existing_meta.get("mechanical_storyboard_options") if isinstance(existing_meta, Mapping) else None
        source_current = (
            isinstance(existing_meta, Mapping) and existing_meta.get("source_revision") == current_source.revision
        )
        version_current = (
            isinstance(options, Mapping) and options.get("splitter_version") == MECHANICAL_STORYBOARD_VERSION
        )
        if (
            not force_regenerate
            and not regenerate_scene_ids
            and existing is not None
            and isinstance(existing.get("scenes"), list)
            and existing["scenes"]
            and source_current
            and version_current
        ):
            script = copy.deepcopy(existing)
            reused = True
            if isinstance(options, Mapping) and isinstance(options.get("scene_attribution_warnings"), list):
                warnings = [item for item in options["scene_attribution_warnings"] if isinstance(item, dict)]
        else:
            registered = _project_registered_names(project)
            declared_seconds = int(getattr(parsed, "total_duration", 0) or 0)
            try:
                storyboard: MechanicalStoryboard = split_episode(
                    parsed,
                    episode,
                    registered=registered,
                    declared_seconds=declared_seconds,
                )
            except StoryboardSplitError as exc:
                raise ShortDramaWorkflowError("short_drama_import_refused", str(exc)) from exc
            script = build_mechanical_scenes(
                parsed,
                episode,
                [list(scene.beat_ids) for scene in storyboard.scenes],
                [scene.duration_seconds for scene in storyboard.scenes],
                [scene.assets for scene in storyboard.scenes],
            )
            warnings = [warning.as_dict() for warning in storyboard.warnings]
            unknown = [
                scene_id
                for scene_id in regenerate_scene_ids
                if not any(
                    isinstance(scene, Mapping) and str(scene.get("scene_id")) == scene_id
                    for scene in script.get("scenes") or []
                )
            ]
            if unknown:
                raise ShortDramaWorkflowError(
                    "short_drama_import_refused",
                    f"要重分镜的分镜不存在：{'、'.join(unknown)}",
                )
            if existing is not None:
                _preserve_unchanged_scenes(script, existing, reset_ids=set(regenerate_scene_ids))
            if regenerate_scene_ids:
                regenerated = len(regenerate_scene_ids)
        core = {key: value for key, value in script.items() if key != "short_drama_meta"}
        DramaEpisodeScript.model_validate(core)
    except ShortDramaWorkflowError:
        raise
    except Exception as exc:
        raise ShortDramaWorkflowError("short_drama_parse_failed", f"Markdown 解析失败: {exc}") from exc

    source = await asyncio.to_thread(load_short_drama_source, projects, project_name, episode)
    if source is None or source.revision != expected_source_revision or source.markdown != markdown:
        raise ShortDramaWorkflowError("short_drama_source_conflict", "Markdown 源稿已更新，请刷新后重试")

    meta = script.setdefault("short_drama_meta", {})
    meta["source_path"] = source.path
    meta["source_revision"] = source.revision
    meta["mechanical_storyboard_options"] = {
        "mode": "mechanical",
        "splitter_version": MECHANICAL_STORYBOARD_VERSION,
        "force_regenerate": force_regenerate,
        "regenerated_scene_ids": list(regenerate_scene_ids),
        "scene_attribution_warnings": warnings,
    }

    def _update_project(current_project: dict[str, Any]) -> None:
        _require_episode(current_project, episode)
        current_source = load_short_drama_source(projects, project_name, episode)
        if current_source is None or current_source.revision != source.revision:
            raise ShortDramaSourceConflict("short drama source changed during script commit")
        # 落盘前在项目锁内复核资产前提，避免构建期间资产被删除或清空。
        missing = assets_missing_description(current_project, markdown)
        if missing:
            raise ShortDramaWorkflowError(
                "short_drama_assets_not_ready",
                "请先完成资产步骤：" + "、".join(item["name"] for item in missing),
            )

    if not reused:
        try:
            await run_sync_transaction(
                projects.save_script,
                project_name,
                script,
                episode_script_filename(episode),
                project_update=_update_project,
                expected_fingerprint=canonical_json_digest(existing) if existing is not None else None,
            )
        except ShortDramaWorkflowError:
            raise
        except ScriptWriteConflict as exc:
            raise ShortDramaWorkflowError(
                "short_drama_script_conflict",
                "分镜已被其他修改更新，请刷新后重试",
            ) from exc
        except ShortDramaSourceConflict as exc:
            raise ShortDramaWorkflowError(
                "short_drama_source_conflict",
                "Markdown 源稿已被其他修改更新，请刷新后重试",
            ) from exc
        except Exception as exc:
            raise ShortDramaWorkflowError("short_drama_save_failed", f"剧本落盘失败: {exc}") from exc

    pending = _storyboard_pending_ids(script)
    declared = int(getattr(parsed, "total_duration", 0) or 0)
    actual = sum(int(scene.get("duration_seconds") or 0) for scene in script.get("scenes", []))
    return ShortDramaImportResult(
        episode=episode,
        scenes=len(script["scenes"]),
        source_path=source.path,
        source_revision=source.revision,
        pending_authoring=len(pending),
        reused=reused,
        declared_seconds=declared if declared and actual != declared else None,
        actual_seconds=actual if declared and actual != declared else None,
        duration_shortfall=actual < declared if declared and actual != declared else None,
        regenerated_scenes=regenerated,
        warnings=tuple(warnings),
    )


__all__ = [
    "ShortDramaImportResult",
    "ShortDramaWorkflowError",
    "import_short_drama_episode",
    "script_has_media_artifacts",
]
