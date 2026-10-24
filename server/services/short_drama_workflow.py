"""短剧工厂 Markdown 到正式剧本的统一导入工作流。"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from collections.abc import Mapping, Sequence
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
from lib.infra.content_digest import canonical_json_digest
from lib.infra.json_io import atomic_write_json
from lib.infra.text_utils import strip_json_code_fences
from lib.project.asset_types import resolve_asset_key
from lib.project.project_manager import ProjectManager, ScriptWriteConflict
from lib.script.script_models import DramaEpisodeScript
from lib.script.short_drama_import import (
    STORY_SCENE_WINDOW_SECONDS,
    ParsedEpisode,
    assets_missing_description,
    beat_catalog,
    convert_planned_scenes,
    parse_short_drama_episode,
    scene_beat_groups,
)
from lib.script.short_drama_source import (
    ShortDramaSourceConflict,
    load_short_drama_source,
    short_drama_source_relative_path,
)
from lib.script.shot_prompt_standard import (
    CAMERA_MOTIONS,
    CAMERA_POSITIONS,
    SHOT_SIZES,
    lint_shot_prompt,
)


class ScenePlan(BaseModel):
    """模型返回的一幕规划。

    字段尽量宽松：模型多写一个 ``video_prompt`` 或漏一个空数组都不该让整次规划作废——语义护栏
    交给 :func:`_validate_plan_coverage` / :func:`_validate_plan_quality`，那里能给出可回喂的原因，
    也能让「时长不在 12–15」这类问题走返修而不是直接抛 ValidationError。
    """

    model_config = ConfigDict(extra="ignore")
    beat_ids: list[int] = Field(min_length=1)
    duration_seconds: int = Field(ge=1, le=600)
    segment_break: bool = False
    scene_description: str = Field(min_length=1)
    image_prompt: str = Field(min_length=1)
    utterances: list[dict[str, Any]] = Field(default_factory=list)
    characters_in_scene: list[str] = Field(default_factory=list)
    scenes: list[str] = Field(default_factory=list)
    props: list[str] = Field(default_factory=list)


class ScenePlanResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenes: list[ScenePlan] = Field(min_length=1)


class SceneRewrite(BaseModel):
    """模型返回的单个已有分镜的重写稿：scene_id 是对齐锚，只重写内容、不重切 beat。"""

    model_config = ConfigDict(extra="ignore")
    scene_id: str = Field(min_length=1)
    duration_seconds: int = Field(ge=1, le=600)
    scene_description: str = Field(min_length=1)
    image_prompt: str = Field(min_length=1)


class SceneRewriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenes: list[SceneRewrite] = Field(min_length=1)


#: 规划缓存的格式版本：校验口径变了就换版本，避免复用旧口径产出的规划。
_PLAN_CACHE_VERSION = "v4"
#: 一次规划最多调用几次文本模型：首轮 + 带拒绝原因的返修轮。校验不通过就继续让 AI 改写优化，
#: 所有轮次都失败则拒绝提交，不交付机械切分结果。
_PLAN_ATTEMPTS = 5

_PLANNING_STRENGTH_HINTS = {
    1: "保守：只做视觉化与格式落地，不改动作顺序、不碰台词。",
    2: "标准：可理顺神态与动作的镜头归属，台词仍逐字照抄。",
    3: "增强：可调整动作分配与台词节奏，但 utterances 仍逐字照抄原稿。",
}

#: ``image_prompt`` 的格式约定（D-3 / D-10 统一标准）：规划与按分镜重写共用同一份指令文案，
#: 景别 / 机位 / 运镜词表取自 :mod:`lib.script.shot_prompt_standard` 的单一真相源，与校验闸同源。
_IMAGE_PROMPT_FORMAT_RULES = (
    "5. image_prompt：单段多行文本，每个镜头一段，镜头之间空一行，格式固定（示例）：",
    "【镜头1｜中景】平视机位，缓推。沈昭@[沈昭]站在床侧，左手把一张 A4 白纸举到台灯下，纸面被灯光穿透，"
    "签名栏的字迹透出淡红；床头监护仪亮着绿色波形线，周敏@[周敏]卧在床上，双眼闭着，呼吸平缓。",
    f"  - 每段以「【镜头N｜景别】」起头，N 从 1 连续递增，只有一个镜头也写【镜头1】；"
    f"景别只能取 {'/'.join(SHOT_SIZES)}。",
    f"  - 景别之后的首句写机位与运镜（如「平视机位，缓推。」）：机位只能取 {'/'.join(CAMERA_POSITIONS)}；"
    f"运镜只能取 {'/'.join(CAMERA_MOTIONS)}。不要照抄“景别、机位、运镜”这几个字。",
    "  - 正文是连贯叙述句：主体（实名 + 具体动作 + 神态）→ 环境道具细节 → 光线氛围一笔，各占一层；"
    "不写台词，只写画面看得见的内容。",
    "  - 资产引用只写「名字@[登记名]」（衍生写「角色/衍生@[角色/衍生]」），只能引用 assets 清单里的名字；"
    "禁止“角色@[...]”这类泛化前缀，禁止把资产外观整份抄进正文；每段第一个主体写实名，句内后续才可用代词。",
    "  - 镜头数由内容决定：12–15 秒一幕常见 3–5 个，平静对话幕 2 个也够，动作/对峙密集幕 4–6 个；"
    "禁止把同一个动作拆成雷同镜头凑数。",
    "  - 不要写 Reference_Images、Style、Avoid、视觉参考 等最终包装字段。",
)


class _PlanRejected(ValueError):
    """AI 规划未通过结构 / 丰满度校验，可将原因回喂模型返修。"""


def _normalize_image_prompt(text: str) -> str:
    """提示词标准化：统一镜头标记写法与行尾空白，让校验、存储与展示同一口径。

    模型常把 ``【镜头1｜中景】`` 写成 ``【镜头 1| 中景 】`` / ``[镜头1]``；这些变体
    语义相同，直接在入库前统一成规范形，而不是让下游各消费方各自兼容。
    """

    def _canon_mark(match: re.Match[str]) -> str:
        shot_size = (match.group(2) or "").strip()
        return f"【镜头{match.group(1)}｜{shot_size}】" if shot_size else f"【镜头{match.group(1)}】"

    text = text.replace("\r\n", "\n")
    text = re.sub(r"[\[【]\s*镜头\s*(\d+)\s*(?:[｜|]\s*([^】\n]*?)\s*)?[\]】]", _canon_mark, text)
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _normalize_plan(plan: ScenePlanResult) -> ScenePlanResult:
    """对整份规划做确定性标准化（不改语义），标准化后再进校验。"""
    return plan.model_copy(
        update={
            "scenes": [
                scene.model_copy(update={"image_prompt": _normalize_image_prompt(scene.image_prompt)})
                for scene in plan.scenes
            ]
        }
    )


def _registered_names(assets: dict[str, list[str]]) -> set[str]:
    """资产名字清单摊平成登记名集合（格式闸的登记与出场判定同用这一份）。"""
    return {name for group in assets.values() for name in group}


def _validate_plan_quality(plan: ScenePlanResult, assets: dict[str, list[str]]) -> None:
    """结构与丰满度同为交付闸，不合格时整体返修（校验口径见 ``lint_shot_prompt``）。"""
    names = _registered_names(assets)
    for scene_index, scene in enumerate(plan.scenes, 1):
        if problem := lint_shot_prompt(scene.image_prompt, registered_names=names, allowed_names=names):
            raise _PlanRejected(f"scene {scene_index}：{problem}")


def _validate_plan_coverage(plan: ScenePlanResult, beats: list[dict[str, Any]]) -> None:
    """结构护栏（D-5）：beat 一一覆盖、不劈断、时长落窗口。"""
    flat_ids = [beat_id for scene in plan.scenes for beat_id in scene.beat_ids]
    if flat_ids != list(range(1, len(beats) + 1)):
        raise _PlanRejected("beat_ids 必须从 1 开始、连续覆盖全部 beats，不重不漏也不劈断")
    for scene_index, scene in enumerate(plan.scenes, 1):
        low, high = STORY_SCENE_WINDOW_SECONDS
        if not low <= scene.duration_seconds <= high:
            raise _PlanRejected(f"scene {scene_index} 时长 {scene.duration_seconds} 秒不在 {low}–{high} 秒窗口内")


def _validate_rewrite(
    rewrite: SceneRewriteResult, requested: list[tuple[str, list[dict[str, Any]]]], assets: dict[str, list[str]]
) -> None:
    """按分镜重写稿的护栏：scene_id 一一对应、时长落窗口、提示词过格式闸。"""
    requested_ids = [scene_id for scene_id, _beats in requested]
    returned_ids = [scene.scene_id for scene in rewrite.scenes]
    if returned_ids != requested_ids:
        raise _PlanRejected(f"scene_id 必须与待重写的分镜一一对应（期望 {requested_ids}，实际 {returned_ids}）")
    names = _registered_names(assets)
    for (scene_id, _beats), scene in zip(requested, rewrite.scenes, strict=True):
        low, high = STORY_SCENE_WINDOW_SECONDS
        if not low <= scene.duration_seconds <= high:
            raise _PlanRejected(f"分镜 {scene_id} 时长 {scene.duration_seconds} 秒不在 {low}–{high} 秒窗口内")
        if problem := lint_shot_prompt(
            _normalize_image_prompt(scene.image_prompt), registered_names=names, allowed_names=names
        ):
            raise _PlanRejected(f"分镜 {scene_id}：{problem}")


def _rewrite_prompt(
    *,
    requested: list[tuple[str, list[dict[str, Any]]]],
    assets: dict[str, list[str]],
    planning_strength: int,
    previous_prompts: dict[str, str],
    rejection: str | None,
) -> str:
    """按分镜重写的提示词：beat 分组固定不变，只重写画面描述与分镜提示词。"""
    plan_rules = "\n".join(
        [
            "你是专业短剧摄影分镜师。用户选定了若干已有分镜要求重写；beat 分组保持不变，"
            "只重写每个分镜的 scene_description 与 image_prompt。",
            "硬约束：",
            "1. 逐条输出 scenes，scene_id 逐字等于输入分镜的 id，不增不减不改名。",
            "2. duration_seconds：取 12–15 的整数。",
            "3. scene_description：改写成镜头可读的画面（具体动作 + 神态 + 环境），只写画面看得见的内容，不写台词。",
            "4. utterances 里的台词不归你管，不要输出；image_prompt 覆盖的画面必须仍承载输入 beats 的剧情。",
            *_IMAGE_PROMPT_FORMAT_RULES,
            f"优化强度={planning_strength}。{_PLANNING_STRENGTH_HINTS[planning_strength]}",
            f"assets={json.dumps(assets, ensure_ascii=False)}（已登记的出场资产名字清单，引用 token 只能写这些名字）",
            "待重写的分镜（old_image_prompt 只作参考，可整体重写）：",
        ]
    )
    scene_blocks = []
    for scene_id, beats in requested:
        scene_blocks.append(
            json.dumps(
                {
                    "scene_id": scene_id,
                    "beats": beats,
                    "old_image_prompt": previous_prompts.get(scene_id, ""),
                },
                ensure_ascii=False,
            )
        )
    prompt = "\n".join([plan_rules, *scene_blocks])
    if rejection:
        prompt += f"\n上一轮输出未通过校验：{rejection}。请修正后重新输出完整 scenes。"
    return prompt


async def _request_rewrite(
    *,
    project_name: str,
    requested: list[tuple[str, list[dict[str, Any]]]],
    assets: dict[str, list[str]],
    planning_strength: int,
    previous_prompts: dict[str, str],
) -> SceneRewriteResult:
    """按分镜重写：校验不合格带原因返修，重跑穷尽仍不合格抛 :class:`_PlanRejected`。"""
    generator = await TextGenerator.create(
        TextTaskType.SCRIPT, project_name=project_name, purpose=CallPurpose.SCRIPT_GENERATION
    )
    rejection: str | None = None
    for _attempt in range(_PLAN_ATTEMPTS):
        generated = await generator.generate(
            BackendTextGenerationRequest(
                prompt=_rewrite_prompt(
                    requested=requested,
                    assets=assets,
                    planning_strength=planning_strength,
                    previous_prompts=previous_prompts,
                    rejection=rejection,
                ),
                response_schema=SceneRewriteResult,
                max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
            ),
            project_name=project_name,
            require_complete=True,
        )
        try:
            payload = json.loads(strip_json_code_fences(generated.text))
            rewrite = SceneRewriteResult.model_validate(payload)
            rewrite = rewrite.model_copy(
                update={
                    "scenes": [
                        scene.model_copy(update={"image_prompt": _normalize_image_prompt(scene.image_prompt)})
                        for scene in rewrite.scenes
                    ]
                }
            )
            _validate_rewrite(rewrite, requested, assets)
        except (json.JSONDecodeError, ValidationError, _PlanRejected) as exc:
            rejection = str(exc)
            continue
        return rewrite
    raise _PlanRejected(rejection or "AI 返回的分镜重写不可用")


def _asset_context(project: dict[str, Any], parsed: ParsedEpisode) -> dict[str, list[str]]:
    """本集源稿提到、且已登记的资产名字清单；只给名字与登记状态，不喂外观描述（D-10）。

    外观一致性由参考图体系承载：喂了外观，模型就会把它整份抄进提示词正文。
    """
    mentioned = {
        "character": {name for block in parsed.blocks for name in block.characters},
        "scene": {block.location for block in parsed.blocks if block.location},
        "prop": {name for block in parsed.blocks for name in block.props},
    }
    context: dict[str, list[str]] = {}
    for asset_type, bucket_name in (
        ("character", "characters"),
        ("scene", "scenes"),
        ("prop", "props"),
    ):
        registered = project.get(bucket_name) or {}
        context[asset_type] = sorted(
            name
            for name in mentioned[asset_type]
            if isinstance(registered, Mapping) and resolve_asset_key(registered, name)
        )
    return context


def _plan_prompt(
    *,
    beats: list[dict[str, Any]],
    assets: dict[str, list[str]],
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
            *_IMAGE_PROMPT_FORMAT_RULES,
            "6. characters_in_scene / scenes / props：只填资产清单或 beats 里出现过的原名，不翻译、不新造；video_prompt 一律不输出。",
            f"优化强度={planning_strength}。{_PLANNING_STRENGTH_HINTS[planning_strength]}",
            f"assets={json.dumps(assets, ensure_ascii=False)}（已登记的出场资产名字清单，引用 token 只能写这些名字）",
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
    assets: dict[str, list[str]],
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
            plan = _normalize_plan(ScenePlanResult.model_validate(payload))
            _validate_plan_coverage(plan, beats)
            _validate_plan_quality(plan, assets)
        except (json.JSONDecodeError, ValidationError, _PlanRejected) as exc:
            rejection = str(exc)
            continue
        return plan
    raise _PlanRejected(rejection or "AI 返回的分镜不可用")


def _read_cached_plan(
    cache_path: Path, beats: list[dict[str, Any]], assets: dict[str, list[str]]
) -> ScenePlanResult | None:
    """读规划缓存；读不出或已不合当前校验口径就当没缓存。"""
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        plan = ScenePlanResult.model_validate(data)
        _validate_plan_coverage(plan, beats)
        _validate_plan_quality(plan, assets)
    except (OSError, json.JSONDecodeError, ValidationError, _PlanRejected):
        return None
    return plan


def _write_cached_plan(cache_path: Path, plan: ScenePlanResult) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(cache_path, plan.model_dump())


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
    declared_seconds: int | None = None
    actual_seconds: int | None = None
    duration_shortfall: bool | None = None
    planning_strength: int = 2
    planning_cached: bool = False
    ai_authoring: bool = True
    #: 本次按分镜重写（部分重生成）的分镜数；整体分镜为 None。
    regenerated_scenes: int | None = None

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
        if self.regenerated_scenes is not None:
            result["regenerated_scenes"] = self.regenerated_scenes
        if self.declared_seconds is not None:
            result.update(
                declared_seconds=self.declared_seconds,
                actual_seconds=self.actual_seconds,
                duration_shortfall=self.duration_shortfall,
            )
        return result


def _require_episode(project: dict[str, Any], episode: int) -> None:
    if project.get("content_mode") != "drama" or project.get("generation_mode", "storyboard") != "storyboard":
        raise ShortDramaWorkflowError("invalid_request", "AI 分镜仅适用于短剧工厂制作路线")
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


def _scene_prompt_text(scene: Mapping[str, Any]) -> str:
    """分镜条目当前的分镜提示词正文；结构化与纯文本两种形态同一出口。"""
    image_prompt = scene.get("image_prompt")
    if isinstance(image_prompt, Mapping):
        return str(image_prompt.get("scene") or "")
    return image_prompt if isinstance(image_prompt, str) else ""


async def _rewrite_selected_scenes(
    *,
    projects: ProjectManager,
    project_name: str,
    episode: int,
    project: Mapping[str, Any],
    parsed: ParsedEpisode,
    beats: list[dict[str, Any]],
    planning_strength: int,
    regenerate_scene_ids: Sequence[str],
) -> tuple[dict[str, Any], int]:
    """按分镜重写：只让 AI 重写选中分镜的描述与提示词，其余分镜原样保留。

    beat 分组从现有脚本的 ``source_text`` 锚定，映射不上即报错要求整体重新分镜；
    未选中分镜连字典引用都不共享，已生成的媒体、视频提示词与手动编辑不会被动到。
    """
    current = await asyncio.to_thread(_load_current_script, projects, project_name, episode)
    if not isinstance(current, dict) or not current.get("scenes"):
        raise ShortDramaWorkflowError(
            "short_drama_ai_planning_failed",
            "本集还没有分镜，无法按分镜重生成；请先执行完整的 AI 分镜",
        )
    try:
        groups = scene_beat_groups(parsed, current["scenes"])
    except ValueError as exc:
        raise ShortDramaWorkflowError("short_drama_ai_planning_failed", str(exc)) from exc
    indexed: dict[str, int] = {}
    for index, (scene, _group) in enumerate(zip(current["scenes"], groups, strict=True)):
        scene_id = str(scene.get("scene_id") or "") if isinstance(scene, Mapping) else ""
        if scene_id:
            indexed[scene_id] = index
    unknown = [scene_id for scene_id in regenerate_scene_ids if scene_id not in indexed]
    if unknown or not regenerate_scene_ids:
        raise ShortDramaWorkflowError(
            "invalid_request",
            f"要重生成的分镜不存在或为空: {', '.join(unknown) if unknown else '<空>'}",
        )
    requested = [
        (scene_id, [beats[beat_id - 1] for beat_id in groups[indexed[scene_id]]]) for scene_id in regenerate_scene_ids
    ]
    previous_prompts = {
        scene_id: _scene_prompt_text(current["scenes"][indexed[scene_id]]) for scene_id, _beats in requested
    }
    try:
        rewrites = await _request_rewrite(
            project_name=project_name,
            requested=requested,
            assets=_asset_context(dict(project), parsed),
            planning_strength=planning_strength,
            previous_prompts=previous_prompts,
        )
    except _PlanRejected as exc:
        # 无机械回落：重写稿不合格就报错，绝不拿旧稿或骨架冒充重写结果。
        raise ShortDramaWorkflowError("short_drama_ai_planning_failed", str(exc)) from exc
    except Exception as exc:
        raise ShortDramaWorkflowError("short_drama_ai_planning_failed", f"AI 分镜重写失败: {exc}") from exc
    script = {
        **current,
        "scenes": [dict(scene) if isinstance(scene, Mapping) else scene for scene in current["scenes"]],
    }
    for rewrite in rewrites.scenes:
        index = indexed[rewrite.scene_id]
        scene = dict(script["scenes"][index])
        scene["duration_seconds"] = rewrite.duration_seconds
        scene["scene_description"] = rewrite.scene_description
        scene["image_prompt"] = rewrite.image_prompt
        scene["video_prompt"] = None
        scene["pending_authoring"] = True
        scene["generated_assets"] = dict(scene.get("generated_assets") or {})
        scene["generated_assets"].pop("video_prompt_basis", None)
        script["scenes"][index] = scene
    return script, len(rewrites.scenes)


async def import_short_drama_episode(
    *,
    project_name: str,
    episode: int,
    markdown: str,
    projects: ProjectManager,
    expected_source_revision: str,
    confirm_artifacts: bool = False,
    planning_strength: int = 2,
    force_regenerate: bool = False,
    regenerate_scene_ids: Sequence[str] = (),
) -> ShortDramaImportResult:
    """从已入库的工厂源稿生成 AI 分镜，不写入正文或登记资产。

    ``regenerate_scene_ids`` 非空时走「按分镜重写」：beat 分组与未选中分镜一律不动，
    只让 AI 重写选中分镜的画面描述与提示词——已生成的媒体与手动编辑因此不会丢。
    智能分镜没有机械回落：校验不通过就带原因持续返修，重跑穷尽仍不合格才报错
    （报错即「模型/流程无法运作」，不产出次品）。
    """
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
    regenerated: int | None = None
    plan: ScenePlanResult | None = None
    try:
        if isinstance(planning_strength, bool) or planning_strength not in _PLANNING_STRENGTH_HINTS:
            raise ShortDramaWorkflowError("invalid_request", "planning_strength 必须是 1 到 3 的整数")
        parsed = parse_short_drama_episode(markdown)
        beats = beat_catalog(parsed)
        if not beats:
            raise ShortDramaWorkflowError("short_drama_ai_planning_failed", "源稿没有可分镜的动作或台词")
        missing_assets = assets_missing_description(project, markdown)
        if missing_assets:
            raise ShortDramaWorkflowError(
                "short_drama_assets_not_ready",
                "请先完成资产步骤，以下资产尚未登记或缺少描述：" + "、".join(item["name"] for item in missing_assets),
            )
        assets = _asset_context(project, parsed)
        existing = await asyncio.to_thread(_load_current_script, projects, project_name, episode)
        existing_meta = existing.get("short_drama_meta") if existing else None
        options = existing_meta.get("ai_storyboard_options") if isinstance(existing_meta, dict) else None
        preserve_current = (
            not force_regenerate
            and not regenerate_scene_ids
            and isinstance(existing_meta, dict)
            and isinstance(options, dict)
            and existing_meta.get("source_revision") == current_source.revision
            and options.get("mode") == "smart"
            and options.get("strength") == planning_strength
        )
        if preserve_current:
            assert existing is not None
            script = copy.deepcopy(existing)
            cached = True
        elif regenerate_scene_ids:
            script, regenerated = await _rewrite_selected_scenes(
                projects=projects,
                project_name=project_name,
                episode=episode,
                project=project,
                parsed=parsed,
                beats=beats,
                planning_strength=planning_strength,
                regenerate_scene_ids=regenerate_scene_ids,
            )
        else:
            cache_path = (
                projects.get_project_path(project_name)
                / "drafts"
                / f"short_drama_plan_{_PLAN_CACHE_VERSION}_e{episode}_{current_source.revision}_s{planning_strength}.json"
            )
            if not force_regenerate:
                plan = await asyncio.to_thread(_read_cached_plan, cache_path, beats, assets)
                cached = plan is not None
            if plan is None:
                try:
                    plan = await _request_plan(
                        project_name=project_name,
                        beats=beats,
                        assets=assets,
                        planning_strength=planning_strength,
                    )
                except _PlanRejected as exc:
                    raise ShortDramaWorkflowError("short_drama_ai_planning_failed", str(exc)) from exc
                except Exception as exc:
                    raise ShortDramaWorkflowError("short_drama_ai_planning_failed", f"AI 分镜规划失败: {exc}") from exc
                await run_sync_transaction(_write_cached_plan, cache_path, plan)
            script = convert_planned_scenes(
                parsed,
                episode,
                [scene.beat_ids for scene in plan.scenes],
                [scene.duration_seconds for scene in plan.scenes],
                [scene.scene_description for scene in plan.scenes],
                [scene.image_prompt for scene in plan.scenes],
            )
            for scene, planned in zip(script["scenes"], plan.scenes, strict=True):
                scene["segment_break"] = planned.segment_break
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
    meta["ai_storyboard_options"] = {
        "mode": "smart",
        "strength": planning_strength,
        "force_regenerate": force_regenerate,
    }
    if regenerate_scene_ids:
        meta["ai_storyboard_options"]["regenerated_scene_ids"] = list(regenerate_scene_ids)

    def _update_project(current_project: dict[str, Any]) -> None:
        _require_episode(current_project, episode)
        current_source = load_short_drama_source(projects, project_name, episode)
        if current_source is None or current_source.revision != source.revision:
            raise ShortDramaSourceConflict("short drama source changed during script commit")
        # 模型调用期间资产可能被删除或清空，提交时在项目锁内复核。
        missing = assets_missing_description(current_project, markdown)
        if missing:
            raise ShortDramaWorkflowError(
                "short_drama_assets_not_ready",
                "请先完成资产步骤：" + "、".join(item["name"] for item in missing),
            )

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

    declared = int(getattr(parsed, "total_duration", 0) or 0)
    actual = sum(int(scene.get("duration_seconds") or 0) for scene in script.get("scenes", []))
    return ShortDramaImportResult(
        episode=episode,
        scenes=len(script["scenes"]),
        source_path=source.path,
        source_revision=source.revision,
        declared_seconds=declared if declared and actual != declared else None,
        actual_seconds=actual if declared and actual != declared else None,
        duration_shortfall=actual < declared if declared and actual != declared else None,
        planning_strength=planning_strength,
        planning_cached=cached,
        regenerated_scenes=regenerated,
    )


__all__ = [
    "ShortDramaImportResult",
    "ShortDramaWorkflowError",
    "import_short_drama_episode",
    "script_has_media_artifacts",
]
