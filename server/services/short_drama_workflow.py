"""短剧工厂 Markdown 到正式剧本的统一导入工作流。"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import re
from dataclasses import dataclass
from pathlib import Path
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
    ParsedEpisode,
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


#: 规划缓存的格式版本：校验口径变了就换版本，避免复用旧口径产出的规划。
_PLAN_CACHE_VERSION = "v2"
#: 一次规划最多调用几次文本模型：首轮 + 带拒绝原因的返修轮；仍不合格则回落机械切分。
_PLAN_ATTEMPTS = 2

#: 镜头首行必须是真实镜头语言，不是「景别 / 机位 / 运镜」这类占位字面量。
_SHOT_SIZE_WORDS = ("大远景", "远景", "全景", "中远景", "中景", "中近景", "近景", "大特写", "特写")
_CAMERA_WORDS = (
    "平视",
    "俯拍",
    "仰拍",
    "俯视",
    "仰视",
    "过肩",
    "主观",
    "侧拍",
    "正拍",
    "斜侧",
    "顶拍",
    "机位",
    "固定",
    "推",
    "拉",
    "摇",
    "移",
    "跟",
    "升",
    "降",
    "环绕",
    "手持",
    "变焦",
    "晃动",
)
_ACTION_WORDS = re.compile(r"推|拉|转|抬|落|抓|握|奔|停|望|看|举|敲|砸|开|合|走|冲|递|抽|攥|跌|挥|点头|摇头")
_ASSET_ANCHOR = re.compile(r"(?:角色|场景|道具|风格锚)@\[")
_SHOT_BLOCK = re.compile(r"【镜头\d+】.*?(?=【镜头\d+】|$)", flags=re.S)

_PLANNING_STRENGTH_HINTS = {
    1: "保守：只做视觉化与格式落地，不改动作顺序、不碰台词。",
    2: "标准：可理顺神态与动作的镜头归属，台词仍逐字照抄。",
    3: "增强：可调整动作分配与台词节奏，但 utterances 仍逐字照抄原稿。",
}


class _PlanRejected(ValueError):
    """AI 规划未通过结构 / 丰满度校验：可带原因重跑，重跑仍不合格则回落机械切分。"""


def _shot_header(block: str) -> str:
    """镜头块首行：景别 / 机位 / 运镜按模板写在行首。"""
    return next((line.strip() for line in block.splitlines() if line.strip()), "")


def _lint_shot_block(block: str) -> str | None:
    """镜头块不合格的原因；合格返回 ``None``。"""
    if len(block.strip()) < 70:
        return "镜头块过短，内容不够丰满"
    if not _ACTION_WORDS.search(block):
        return "镜头块没有具体动作"
    if not _ASSET_ANCHOR.search(block):
        return "镜头块没有引用任何已登记资产锚点"
    header = _shot_header(block)
    if not any(word in header for word in _SHOT_SIZE_WORDS) or not any(word in header for word in _CAMERA_WORDS):
        return "镜头首行缺景别或运镜（须写成「中景，平视机位，缓推。」）"
    if "视觉参考：" not in block:
        return "镜头块缺视觉参考行"
    return None


def _validate_plan_quality(plan: ScenePlanResult) -> None:
    """丰满度校验（硬性质量闸）：不合格抛 :class:`_PlanRejected`，由调用方重跑或回落。"""
    for scene_index, scene in enumerate(plan.scenes, 1):
        blocks = _SHOT_BLOCK.findall(scene.image_prompt)
        if not blocks:
            raise _PlanRejected(f"scene {scene_index} 的 image_prompt 里没有【镜头N】分镜块")
        for shot_index, block in enumerate(blocks, 1):
            problem = _lint_shot_block(block)
            if problem:
                raise _PlanRejected(f"scene {scene_index} 镜头{shot_index} {problem}")
        for left, right in itertools.pairwise(blocks):
            if left.strip() == right.strip():
                raise _PlanRejected(f"scene {scene_index} 有完全重复的凑数镜头")


def _validate_plan_coverage(plan: ScenePlanResult, beats: list[dict[str, Any]]) -> None:
    """结构护栏（D-5）：beat 一一覆盖、不劈断、时长落窗口。"""
    flat_ids = [beat_id for scene in plan.scenes for beat_id in scene.beat_ids]
    if flat_ids != list(range(1, len(beats) + 1)):
        raise _PlanRejected("beat_ids 必须从 1 开始、连续覆盖全部 beats，不重不漏也不劈断")
    for scene_index, scene in enumerate(plan.scenes, 1):
        low, high = STORY_SCENE_WINDOW_SECONDS
        if not low <= scene.duration_seconds <= high:
            raise _PlanRejected(f"scene {scene_index} 时长 {scene.duration_seconds} 秒不在 {low}–{high} 秒窗口内")


def _asset_context(project: dict[str, Any], parsed: ParsedEpisode) -> dict[str, dict[str, str]]:
    """喂给模型的名字与外观：已登记资产的外观优先，源稿写明的外观兜底。"""
    mentioned = {
        "character": {name for block in parsed.blocks for name in block.characters},
        "scene": {block.location for block in parsed.blocks if block.location},
        "prop": {name for block in parsed.blocks for name in block.props},
    }
    from_source = referenced_asset_descriptions(parsed)
    context: dict[str, dict[str, str]] = {"character": {}, "scene": {}, "prop": {}}
    for asset_type, bucket_name in (
        ("character", "characters"),
        ("scene", "scenes"),
        ("prop", "props"),
    ):
        registered = project.get(bucket_name) or {}
        for name in sorted(mentioned[asset_type]):
            entry = registered.get(name)
            description = str(entry.get("description") or "").strip() if isinstance(entry, dict) else ""
            context[asset_type][name] = description or from_source.get(asset_type, {}).get(name, "")
    return context


def _plan_prompt(
    *,
    beats: list[dict[str, Any]],
    assets: dict[str, dict[str, str]],
    planning_strength: int,
    rejection: str | None,
) -> str:
    """一次规划请求的提示词；返修轮把上一轮被拒原因一起喂回去。"""
    prompt = "\n".join(
        [
            "你是专业短剧摄影分镜师。对下面给出的全部 beats 一次性输出 scenes 数组。",
            "硬约束：",
            "1. scene.beat_ids：从 1 开始、按顺序连续覆盖每一个 beat，不重不漏；"
            "绝不劈开一个动作行或一句台词；优先在【场景】块边界切分，其次在拍边界；"
            "按叙事语义（说话人更替、地点切换、情绪转折）决定切点，不要按字数均分。",
            "2. scene.duration_seconds：每幕取 12–15 的整数。",
            "3. scene.scene_description：把这些拍改写成镜头可读的画面（具体动作 + 神态 + 环境），"
            "只写画面看得见的内容，不写台词。",
            "4. scene.utterances：逐字照抄输入拍里的台词（dialogue 带 speaker，画外音 voiceover），不得改字、不得增删。",
            "5. scene.image_prompt：单段多行文本，每个镜头 4 行，镜头之间空一行，格式固定：",
            "【镜头1】中景，平视机位，缓推。",
            "画面主体：沈昭@[沈昭/将断头金簪从掌心缓缓抽出，血珠滚落]，沈婉@[沈婉/跪坐一侧，眼底伪怯转冷]。",
            "环境道具：暗殿@[暗殿/残并蒂莲屏风]，朱红漆案、铜醮香炉，喜烛暖橙光斑落在鎏金雕面。",
            "视觉参考：风格锚@[短剧/暗殿对峙质感]",
            "  - 首行必须是「<景别>，<机位>，<运镜>。」：景别只能取 大远景/远景/全景/中景/近景/特写/大特写；"
            "机位只能取 平视/俯拍/仰拍/过肩/主观/侧拍；运镜只能取 固定/推/拉/摇/移/跟/升/降/环绕/手持。"
            "不要照抄“景别、机位、运镜”这几个字。",
            "  - 画面主体必须写具体动作 + 神态，且至少引用一个已登记资产锚点"
            "（角色@[名字/锚点]、场景@[名字/锚点]、道具@[名字/锚点]）；禁止“一位人物”“气氛紧张”这类空泛占位。",
            "  - 镜头数由内容决定：12–15 秒一幕常见 3–5 个，平静对话幕 2 个也够，"
            "动作/对峙密集幕 4–6 个；禁止把同一个动作拆成雷同镜头凑数。",
            "  - 视觉参考行只写文本引用（风格锚或已登记资产），不要描述像素级参考图。",
            "6. characters_in_scene / scenes / props：只填资产清单或 beats 里出现过的原名，不翻译、不新造；video_prompt 一律不输出。",
            f"优化强度={planning_strength}。{_PLANNING_STRENGTH_HINTS[planning_strength]}",
            f"assets={json.dumps(assets, ensure_ascii=False)}",
            f"beats={json.dumps(beats, ensure_ascii=False)}",
        ]
    )
    if rejection:
        prompt += f"\n上一轮输出未通过校验：{rejection}。请修正后重新输出完整 scenes。"
    return prompt


async def _request_plan(
    *,
    project_name: str,
    beats: list[dict[str, Any]],
    assets: dict[str, dict[str, str]],
    planning_strength: int,
) -> ScenePlanResult:
    """调用文本模型产出规划；结构 / 丰满度不合格时带原因返修一轮，仍不合格抛 :class:`_PlanRejected`。"""
    generator = await TextGenerator.create(
        TextTaskType.SCRIPT, project_name=project_name, purpose=CallPurpose.SCRIPT_GENERATION
    )
    rejection: str | None = None
    for _attempt in range(_PLAN_ATTEMPTS):
        generated = await generator.generate(
            BackendTextGenerationRequest(
                prompt=_plan_prompt(
                    beats=beats,
                    assets=assets,
                    planning_strength=planning_strength,
                    rejection=rejection,
                ),
                response_schema=ScenePlanResult,
                max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
            ),
            project_name=project_name,
            require_complete=True,
        )
        try:
            payload = json.loads(strip_json_code_fences(generated.text))
            plan = ScenePlanResult.model_validate(payload)
            _validate_plan_coverage(plan, beats)
            _validate_plan_quality(plan)
        except (json.JSONDecodeError, ValidationError, _PlanRejected) as exc:
            rejection = str(exc)
            continue
        return plan
    raise _PlanRejected(rejection or "AI 返回的分镜不可用")


def _read_cached_plan(cache_path: Path, beats: list[dict[str, Any]]) -> ScenePlanResult | None:
    """读规划缓存；读不出或已不合当前校验口径就当没缓存。"""
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        plan = ScenePlanResult.model_validate(data)
        _validate_plan_coverage(plan, beats)
        _validate_plan_quality(plan)
    except (OSError, json.JSONDecodeError, ValidationError, _PlanRejected):
        return None
    return plan


def _write_cached_plan(cache_path: Path, plan: ScenePlanResult) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(plan.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")


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
    #: AI 规划被护栏拒绝后回落机械切分的原因；未回落为 None。
    planning_fallback: str | None = None

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
        if self.planning_fallback:
            result["planning_fallback"] = self.planning_fallback
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
    ai_planning: bool = False,
    force_regenerate: bool = False,
) -> ShortDramaImportResult:
    """解析源稿并提交正式剧本；Web 与 Agent 入口共用此命令。

    ``ai_planning`` 是显式的选择加入开关：只有分镜页「智能切分」才置 True 走文本模型，
    其余入口（工厂/Agent 导入）保持机械切分、不调用任何模型。
    """
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
    fallback_reason: str | None = None
    plan: ScenePlanResult | None = None
    try:
        if isinstance(planning_strength, bool) or planning_strength not in _PLANNING_STRENGTH_HINTS:
            raise ShortDramaWorkflowError("invalid_request", "planning_strength 必须是 1 到 3 的整数")
        parsed = parse_short_drama_episode(markdown)
        beats = beat_catalog(parsed)
        if ai_planning and beats:
            source = await asyncio.to_thread(load_short_drama_source, projects, project_name, episode)
            revision_key = (
                source.revision if source is not None else hashlib.sha256(markdown.encode("utf-8")).hexdigest()
            )
            cache_path = (
                projects.get_project_path(project_name)
                / "drafts"
                / f"short_drama_plan_{_PLAN_CACHE_VERSION}_e{episode}_{revision_key}_s{planning_strength}.json"
            )
            if not force_regenerate:
                plan = await asyncio.to_thread(_read_cached_plan, cache_path, beats)
                cached = plan is not None
            if plan is None:
                try:
                    plan = await _request_plan(
                        project_name=project_name,
                        beats=beats,
                        assets=_asset_context(project, parsed),
                        planning_strength=planning_strength,
                    )
                except _PlanRejected as exc:
                    # 护栏（D-5）：重跑仍不合格就回落机械切分，绝不把用户卡在一个失败弹窗上。
                    fallback_reason = str(exc)
                except Exception as exc:
                    raise ShortDramaWorkflowError("short_drama_ai_planning_failed", f"AI 分镜规划失败: {exc}") from exc
                else:
                    await asyncio.to_thread(_write_cached_plan, cache_path, plan)
        if plan is None:
            script = convert_to_drama_script(parsed, episode, scene_window=STORY_SCENE_WINDOW_SECONDS)
        else:
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
        "mode": "smart" if ai_planning else "fast",
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
        ai_authoring=ai_planning and plan is not None,
        planning_fallback=fallback_reason,
    )


__all__ = [
    "ShortDramaImportResult",
    "ShortDramaWorkflowError",
    "import_short_drama_episode",
    "script_has_media_artifacts",
]
