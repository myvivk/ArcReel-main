"""短剧工厂 Markdown 到正式剧本的统一导入工作流。"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lib.backends.providers import CallPurpose
from lib.backends.text_backends.base import DEFAULT_MAX_OUTPUT_TOKENS, TextTaskType
from lib.backends.text_backends.base import TextGenerationRequest as BackendTextGenerationRequest
from lib.backends.text_generator import TextGenerator
from lib.episode.episode_paths import episode_script_filename
from lib.infra.async_thread import run_sync_transaction
from lib.infra.text_utils import strip_json_code_fences
from lib.project.asset_types import register_missing_assets
from lib.project.project_manager import ProjectManager
from lib.script.script_models import DramaEpisodeScript
from lib.script.short_drama_import import (
    STORY_SCENE_WINDOW_SECONDS,
    beat_catalog,
    convert_planned_scenes,
    convert_to_drama_script,
    parse_short_drama_episode,
    referenced_asset_descriptions,
    referenced_asset_names,
)
from lib.script.short_drama_source import (
    ShortDramaSourceConflict,
    load_short_drama_source,
    replace_short_drama_source,
)


class ScenePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    beat_ids: list[int] = Field(min_length=1)
    duration_seconds: int = Field(ge=12, le=15)
    segment_break: bool = False
    scene_description: str = Field(min_length=1)
    image_prompt: str = Field(min_length=1)
    utterances: list[dict[str, Any]]
    characters_in_scene: list[str]
    scenes: list[str]
    props: list[str]


class ScenePlanResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenes: list[ScenePlan] = Field(min_length=1)


def _validate_plan_quality(plan: ScenePlanResult) -> None:
    action = re.compile(r"推|拉|转|抬|落|抓|握|奔|停|望|看|举|敲|砸|开|合|走|冲|递|抽|攥|跌|挥|点头|摇头")
    for scene_index, scene in enumerate(plan.scenes, 1):
        prompt = scene.image_prompt
        blocks = re.findall(r"【镜头\d+】.*?(?=【镜头\d+】|$)", prompt, flags=re.S)
        if not blocks:
            raise ValueError(f"scene {scene_index} has no formatted shot blocks")
        for block in blocks:
            if (
                len(block.strip()) < 70
                or not action.search(block)
                or not re.search(r"(?:角色|场景|道具|风格锚)@\[", block)
            ):
                raise ValueError(f"scene {scene_index} contains an underdeveloped shot block")
            if not re.search(r"景别.{0,16}机位.{0,16}(?:运镜|固定|缓推|缓拉)", block):
                raise ValueError(f"scene {scene_index} is missing camera grammar")
            if not re.search(r"视觉参考：", block):
                raise ValueError(f"scene {scene_index} is missing visual reference")
        for left, right in itertools.pairwise(blocks):
            if left.strip() == right.strip():
                raise ValueError(f"scene {scene_index} contains duplicate filler shots")


class ShortDramaWorkflowError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ShortDramaImportResult:
    episode: int
    scenes: int
    source_path: str
    source_revision: str
    registered_assets: dict[str, list[str]]
    asset_conflicts: list[dict[str, str]]
    assets_without_description: list[dict[str, str]]
    declared_seconds: int | None = None
    actual_seconds: int | None = None
    duration_shortfall: bool | None = None
    planning_strength: int = 2
    planning_cached: bool = False
    ai_authoring: bool = False

    def as_dict(self) -> dict[str, Any]:
        result = {
            "episode": self.episode,
            "scenes": self.scenes,
            "source_path": self.source_path,
            "source_revision": self.source_revision,
            "planning_strength": self.planning_strength,
            "planning_cached": self.planning_cached,
            "ai_authoring": self.ai_authoring,
        }
        if self.registered_assets:
            result["registered_assets"] = self.registered_assets
        if self.asset_conflicts:
            result["asset_conflicts"] = self.asset_conflicts
        if self.assets_without_description:
            result["assets_without_description"] = self.assets_without_description
        if self.declared_seconds is not None:
            result.update(
                declared_seconds=self.declared_seconds,
                actual_seconds=self.actual_seconds,
                duration_shortfall=self.duration_shortfall,
            )
        return result


def _require_episode(project: dict[str, Any], episode: int) -> None:
    if project.get("content_mode") != "drama":
        raise ShortDramaWorkflowError("invalid_request", "短剧工厂导入仅适用于剧情演绎（drama）项目")
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


async def import_short_drama_episode(
    *,
    project_name: str,
    episode: int,
    markdown: str,
    projects: ProjectManager,
    save_source: bool = True,
    expected_source_revision: str | None = None,
    confirm_artifacts: bool = True,
    planning_strength: int = 2,
    fast_split: bool = False,
    force_regenerate: bool = False,
) -> ShortDramaImportResult:
    """解析源稿并提交正式剧本；Web 与 Agent 入口共用此命令。"""
    try:
        project = await asyncio.to_thread(projects.load_project, project_name)
    except FileNotFoundError as exc:
        raise ShortDramaWorkflowError("project_not_found", str(exc)) from exc
    _require_episode(project, episode)

    if not confirm_artifacts:
        entry = next(item for item in project["episodes"] if item.get("episode") == episode)
        script_file = entry.get("script_file")
        if isinstance(script_file, str) and script_file:
            try:
                current_script = await asyncio.to_thread(projects.load_script, project_name, script_file)
            except FileNotFoundError:
                pass
            else:
                if script_has_media_artifacts(current_script):
                    raise ShortDramaWorkflowError(
                        "short_drama_reimport_confirmation_required",
                        "本集已有分镜图或视频，重新导入后这些产物会变为过期状态",
                    )

    cached = False
    try:
        if isinstance(planning_strength, bool) or not 1 <= planning_strength <= 3:
            raise ShortDramaWorkflowError("invalid_request", "planning_strength 必须是 1 到 3 的整数")
        parsed = parse_short_drama_episode(markdown)
        beats = beat_catalog(parsed)
        if fast_split or not beats:
            script = convert_to_drama_script(parsed, episode, scene_window=STORY_SCENE_WINDOW_SECONDS)
        else:
            source = await asyncio.to_thread(load_short_drama_source, projects, project_name, episode)
            revision_key = (
                source.revision if source is not None else hashlib.sha256(markdown.encode("utf-8")).hexdigest()
            )
            cache_path = (
                projects.get_project_path(project_name)
                / "drafts"
                / f"short_drama_plan_e{episode}_{revision_key}_s{planning_strength}.json"
            )
            try:
                plan_data = await asyncio.to_thread(lambda: json.loads(cache_path.read_text(encoding="utf-8")))
                plan = ScenePlanResult.model_validate(plan_data)
                cached = True
            except FileNotFoundError:
                plan = None
            except (OSError, json.JSONDecodeError, ValidationError):
                plan = None
            if force_regenerate:
                plan = None
            if plan is None:
                prompt = (
                    "你是专业短剧摄影分镜师。对输入的全部 beats 一次性输出 scenes。"
                    "scene.beat_ids 必须连续且完整覆盖，绝不劈开动作/台词；语义转折优先切分，场景块边界优先。"
                    "逐幕输出经 show-don't-tell 优化的 scene_description 和标准 image_prompt 单段文本。"
                    "每个镜头块须有具体动作及神态、可拍的环境光影细节、至少一个已登记资产引用；禁止空泛模板和重复凑数。"
                    "image_prompt 使用多行固定格式：每镜头独立行‘【镜头N】景别，机位，运镜。’，下一行‘画面主体：角色@[角色/动作锚点]’，"
                    "下一行‘环境道具：场景@[场景/锚点]，道具@[道具/锚点]，光影细节’，下一行‘视觉参考：风格锚@[短剧/质感]’。"
                    "scene.utterances 必须从输入原文逐字复制，不得修改台词；source_text 会由程序从 beat 原文回填。"
                    "duration_seconds 必须为 12-15 整数；video_prompt 不输出。"
                    f"\\n优化强度={planning_strength}（1保守：只视觉化不碰台词；2标准；3增强：可调整动作分配与台词节奏但 utterances 仍原样）。"
                    f"\\nassets={json.dumps(referenced_asset_descriptions(parsed), ensure_ascii=False)}"
                    f"\\nbeats={json.dumps(beats, ensure_ascii=False)}"
                )
                try:
                    generator = await TextGenerator.create(
                        TextTaskType.SCRIPT, project_name=project_name, purpose=CallPurpose.SCRIPT_GENERATION
                    )
                    generated = await generator.generate(
                        BackendTextGenerationRequest(
                            prompt=prompt,
                            response_schema=ScenePlanResult,
                            max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
                        ),
                        project_name=project_name,
                    )
                    payload = json.loads(strip_json_code_fences(generated.text))
                    plan = ScenePlanResult.model_validate(payload)
                    flat_ids = [beat_id for scene in plan.scenes for beat_id in scene.beat_ids]
                    if flat_ids != list(range(1, len(beats) + 1)):
                        raise ValueError("scene plan must cover source beats exactly once in order")
                    if any(
                        not STORY_SCENE_WINDOW_SECONDS[0] <= scene.duration_seconds <= STORY_SCENE_WINDOW_SECONDS[1]
                        for scene in plan.scenes
                    ):
                        raise ValueError("scene duration is outside the configured window")
                    _validate_plan_quality(plan)
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(
                        cache_path.write_text,
                        json.dumps(plan.model_dump(), ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception as exc:
                    raise ShortDramaWorkflowError("short_drama_ai_planning_failed", f"AI 分镜规划失败: {exc}") from exc
            script = convert_planned_scenes(
                parsed,
                episode,
                [scene.beat_ids for scene in plan.scenes],
                [scene.duration_seconds for scene in plan.scenes],
                [scene.scene_description for scene in plan.scenes],
                [scene.image_prompt for scene in plan.scenes],
            )
        core = {key: value for key, value in script.items() if key != "short_drama_meta"}
        DramaEpisodeScript.model_validate(core)
    except ShortDramaWorkflowError:
        raise
    except Exception as exc:
        raise ShortDramaWorkflowError("short_drama_parse_failed", f"Markdown 解析失败: {exc}") from exc

    if save_source:
        source = await run_sync_transaction(
            replace_short_drama_source,
            projects,
            project_name,
            episode,
            markdown,
        )
    else:
        source = await asyncio.to_thread(load_short_drama_source, projects, project_name, episode)
        if source is None:
            raise ShortDramaWorkflowError("short_drama_source_missing", "本集没有已保存的 Markdown 源稿")
        if source.revision != expected_source_revision:
            raise ShortDramaWorkflowError(
                "short_drama_source_conflict",
                "Markdown 源稿已被其他修改更新，请刷新后重试",
            )
        if source.markdown != markdown:
            raise ShortDramaWorkflowError(
                "short_drama_source_conflict",
                "本次导入内容与已保存的 Markdown 源稿不一致",
            )

    meta = script.setdefault("short_drama_meta", {})
    meta["source_path"] = source.path
    meta["source_revision"] = source.revision
    meta["ai_storyboard_options"] = {
        "mode": "fast" if fast_split else "smart",
        "strength": planning_strength,
        "force_regenerate": force_regenerate,
    }
    names = referenced_asset_names(script)
    descriptions = referenced_asset_descriptions(parsed)
    registered: dict[str, list[str]] = {}
    conflicts: list[dict[str, str]] = []
    assets_without_description: list[dict[str, str]] = []

    def _update_project(current_project: dict[str, Any]) -> None:
        current_source = load_short_drama_source(projects, project_name, episode)
        if current_source is None or current_source.revision != source.revision:
            raise ShortDramaSourceConflict("short drama source changed during script commit")
        new_assets, new_conflicts = register_missing_assets(
            current_project,
            names,
            descriptions_by_type=descriptions,
        )
        registered.update(new_assets)
        conflicts.extend(new_conflicts)
        for asset_type, bucket_name in (
            ("character", "characters"),
            ("scene", "scenes"),
            ("prop", "props"),
        ):
            for name in names.get(asset_type, []):
                entry = (current_project.get(bucket_name) or {}).get(name) or {}
                if not str(entry.get("description") or "").strip():
                    assets_without_description.append(
                        {
                            "type": asset_type,
                            "name": name,
                            "reason": "source_missing" if name in new_assets.get(asset_type, []) else "existing_blank",
                        }
                    )

    try:
        await run_sync_transaction(
            projects.save_script,
            project_name,
            script,
            episode_script_filename(episode),
            project_update=_update_project,
        )
    except ShortDramaSourceConflict as exc:
        raise ShortDramaWorkflowError(
            "short_drama_source_conflict",
            "Markdown 源稿已被其他修改更新，请刷新后重试",
        ) from exc
    except Exception as exc:
        raise ShortDramaWorkflowError("short_drama_save_failed", f"剧本落盘失败: {exc}") from exc

    declared = int(getattr(parsed, "total_duration", 0) or 0)
    actual = sum(int(scene.get("duration_seconds") or 0) for scene in script.get("scenes", []))
    return ShortDramaImportResult(
        episode=episode,
        scenes=len(script["scenes"]),
        source_path=source.path,
        source_revision=source.revision,
        registered_assets=registered,
        asset_conflicts=conflicts,
        assets_without_description=assets_without_description,
        declared_seconds=declared if declared and actual != declared else None,
        actual_seconds=actual if declared and actual != declared else None,
        duration_shortfall=actual < declared if declared and actual != declared else None,
        planning_strength=planning_strength,
        planning_cached=cached,
        ai_authoring=not fast_split,
    )


__all__ = [
    "ShortDramaImportResult",
    "ShortDramaWorkflowError",
    "import_short_drama_episode",
    "script_has_media_artifacts",
]
