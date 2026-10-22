"""短剧工厂 Markdown 到正式剧本的统一导入工作流。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import itertools
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
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
from lib.project.asset_types import register_missing_assets, resolve_asset_key
from lib.project.project_manager import ProjectManager, ScriptWriteConflict
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
    scene_beat_groups,
)
from lib.script.short_drama_source import (
    ShortDramaSourceConflict,
    load_short_drama_source,
    replace_short_drama_source,
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
_PLAN_CACHE_VERSION = "v3"
#: 一次规划最多调用几次文本模型：首轮 + 带拒绝原因的返修轮。校验不通过就继续让 AI 改写优化，
#: 所有轮次都失败则拒绝提交，不交付机械切分结果。
_PLAN_ATTEMPTS = 5

#: 镜头首行必须是真实镜头语言，不是「景别 / 机位 / 运镜」这类占位字面量。
_SHOT_SIZE_WORDS = ("大远景", "远景", "全景", "中远景", "中景", "中近景", "近景", "大特写", "特写")
_CAMERA_POSITION_WORDS = (
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
)
_CAMERA_MOTION_WORDS = (
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
#: 具体动作判定词表：覆盖短剧里常见的肢体 / 表情 / 手部 / 移动动作。校验只要求镜头块
#: 里出现其中任意一个，词表过窄会把合法提示词误判成「没有动作」，逼出无谓返修甚至
#: 让整次规划失败——所以宁可宽，不可窄。
_ACTION_WORDS = re.compile(
    "推|拉|拽|拎|抬|举|落|放|递|接|抛|扔|丢|掷|挥|挥动|摆|摇|晃|"
    "抓|握|攥|捏|掐|拧|撕|扯|拔|抽|插|按|压|掰|挑|解|系|扣|摘|戴|"
    "敲|砸|摔|拍|推门|掀|踢|踩|踏|跺|踹|撞|抵|顶|撑|挡|拦|护|"
    "走|跑|奔|冲|退|撤|追|逃|跟|随|跃|跳|爬|翻|滚|潜|游|飞|"
    "站|坐|跪|蹲|躺|卧|靠|倚|俯身|弯腰|起身|起立|站起|坐下|跪下|转身|回头|侧身|"
    "抬头|低头|转头|回首|逼近|靠近|凑近|后退|贴近|顿住|停住|僵住|定住|"
    "看|望|盯|瞥|瞪|瞧|瞄|扫视|打量|凝视|注视|仰望|俯瞰|环顾|"
    "睁|闭|眨|眯|皱眉|挑眉|蹙眉|咧嘴|抿|咬|嚼|吞|咽|喘|吸|呼|屏息|叹|吐|"
    "笑|哭|泣|哽咽|颤抖|发抖|哆嗦|瑟缩|缩|蜷|瘫|软|"
    "点头|摇头|颔首|鞠躬|挥手|招手|握手|拥抱|搂|抱|揽|挽|扶|搀|牵|拉住|拽住|"
    "打开|合上|关上|翻开|合拢|点亮|熄灭|点|燃|吹|"
    "说话|开口|喊|叫|吼|吼道|低声|轻声|念|读|问|答|应|"
    "想|忆|想起|记起|思索|沉思"
)
_EMOTION_WORDS = re.compile(
    "眼|眉|眸|神色|神情|表情|脸|面色|目光|笑|哭|泣|怒|惊|惧|怯|悲|痛|"
    "恨|冷|暖|紧|颤|抖|慌|恐|急|忧|焦|愣|怔|僵|屏息|叹|喘|哽|咬|抿|"
    "坚定|迟疑|犹豫|压声|软|疲|累|困|兴奋|欣喜|放松|羞|傲|愧|疑|不甘|失落"
)
#: 镜头块起点：``【镜头1】`` 是规范写法，模型的 ``【镜头 1】`` / ``[镜头1]`` 变体同样认。
_SHOT_MARK = r"【?\s*镜头\s*\d+\s*】?"
_SHOT_BLOCK = re.compile(rf"{_SHOT_MARK}.*?(?={_SHOT_MARK}|$)", flags=re.S)

_PLANNING_STRENGTH_HINTS = {
    1: "保守：只做视觉化与格式落地，不改动作顺序、不碰台词。",
    2: "标准：可理顺神态与动作的镜头归属，台词仍逐字照抄。",
    3: "增强：可调整动作分配与台词节奏，但 utterances 仍逐字照抄原稿。",
}


class _PlanRejected(ValueError):
    """AI 规划未通过结构 / 丰满度校验，可将原因回喂模型返修。"""


def _shot_header(block: str) -> str:
    """镜头块首行：景别 / 机位 / 运镜按模板写在行首。"""
    return next((line.strip() for line in block.splitlines() if line.strip()), "")


def _lint_shot_block(block: str, assets: dict[str, dict[str, str]]) -> str | None:
    """镜头块不合格的原因；合格返回 ``None``。"""
    if len(block.strip()) < 70:
        return "镜头块过短，内容不够丰满"
    subject = next((line for line in block.splitlines() if line.startswith("画面主体：")), "")
    environment = next((line for line in block.splitlines() if line.startswith("环境道具：")), "")
    if not subject or not _ACTION_WORDS.search(subject):
        return "镜头块没有具体动作"
    if not _EMOTION_WORDS.search(subject):
        return "镜头块缺具体神态或情绪"
    if not environment.removeprefix("环境道具：").strip(" 。"):
        return "镜头块缺具体环境与光影描写"
    if any(word in block for word in ("一位人物", "一处场景", "气氛紧张", "人物站着")):
        return "镜头块包含空泛占位描写"
    names = {name for group in assets.values() for name in group}
    anchors = re.findall(r"(?:(风格锚))?@\[([^]\n]+)\]", block)
    if not any(anchor.split("/", 1)[0] in names for _style, anchor in anchors):
        return "镜头块没有引用任何已登记资产锚点"
    if any(not style and anchor.split("/", 1)[0] not in names for style, anchor in anchors):
        return "镜头块引用了未登记资产"
    header = _shot_header(block)
    if (
        not any(word in header for word in _SHOT_SIZE_WORDS)
        or not any(word in header for word in _CAMERA_POSITION_WORDS)
        or not any(word in header for word in _CAMERA_MOTION_WORDS)
    ):
        return "镜头首行缺景别、机位或运镜（须写成「中景，平视机位，缓推。」）"
    if "视觉参考：" not in block:
        return "镜头块缺视觉参考行"
    return None


def _normalize_image_prompt(text: str) -> str:
    """提示词标准化：统一镜头标记写法与行尾空白，让校验、存储与展示同一口径。

    模型常把 ``【镜头1】`` 写成 ``【镜头 1】`` / ``[镜头1]`` / ``[镜头 1]``；这些变体
    语义相同，直接在入库前统一成规范形，而不是让下游各消费方各自兼容。
    """
    text = text.replace("\r\n", "\n")
    text = re.sub(r"[\[【]\s*镜头\s*(\d+)\s*[\]】]", lambda match: f"【镜头{match.group(1)}】", text)
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


def _validate_shot_blocks(blocks: list[str], assets: dict[str, dict[str, str]]) -> None:
    for block in blocks:
        if problem := _lint_shot_block(block, assets):
            raise _PlanRejected(problem)
    # 剥掉镜头编号后比较所有组合，编号变化和非相邻重复均不能绕过凑数检查。
    bodies = [re.sub(r"【镜头\d+】", "", block).strip() for block in blocks]
    for left, right in itertools.combinations(bodies, 2):
        if SequenceMatcher(None, left, right, autojunk=False).ratio() >= 0.92:
            raise _PlanRejected("有语义高度重复的凑数镜头")


def _validate_plan_quality(plan: ScenePlanResult, assets: dict[str, dict[str, str]]) -> None:
    """结构与丰满度同为交付闸，不合格时整体返修。"""
    for scene_index, scene in enumerate(plan.scenes, 1):
        blocks = _SHOT_BLOCK.findall(scene.image_prompt)
        if not blocks:
            raise _PlanRejected(f"scene {scene_index} 的 image_prompt 里没有【镜头N】分镜块")
        _validate_shot_blocks(blocks, assets)


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
    rewrite: SceneRewriteResult, requested: list[tuple[str, list[dict[str, Any]]]], assets: dict[str, dict[str, str]]
) -> None:
    """按分镜重写稿的护栏：scene_id 一一对应、时长落窗口、镜头块过丰满度校验。"""
    requested_ids = [scene_id for scene_id, _beats in requested]
    returned_ids = [scene.scene_id for scene in rewrite.scenes]
    if returned_ids != requested_ids:
        raise _PlanRejected(f"scene_id 必须与待重写的分镜一一对应（期望 {requested_ids}，实际 {returned_ids}）")
    for (scene_id, _beats), scene in zip(requested, rewrite.scenes, strict=True):
        low, high = STORY_SCENE_WINDOW_SECONDS
        if not low <= scene.duration_seconds <= high:
            raise _PlanRejected(f"分镜 {scene_id} 时长 {scene.duration_seconds} 秒不在 {low}–{high} 秒窗口内")
        blocks = _SHOT_BLOCK.findall(_normalize_image_prompt(scene.image_prompt))
        if not blocks:
            raise _PlanRejected(f"分镜 {scene_id} 的 image_prompt 里没有【镜头N】分镜块")
        _validate_shot_blocks(blocks, assets)


def _rewrite_prompt(
    *,
    requested: list[tuple[str, list[dict[str, Any]]]],
    assets: dict[str, dict[str, str]],
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
            "5. image_prompt：单段多行文本，每个镜头 4 行，镜头之间空一行，格式固定：",
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
            f"优化强度={planning_strength}。{_PLANNING_STRENGTH_HINTS[planning_strength]}",
            f"assets={json.dumps(assets, ensure_ascii=False)}",
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
    assets: dict[str, dict[str, str]],
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


def _asset_context(project: dict[str, Any], parsed: ParsedEpisode) -> dict[str, dict[str, str]]:
    """只把已登记资产的名字与外观传给分镜模型。"""
    mentioned = {
        "character": {name for block in parsed.blocks for name in block.characters},
        "scene": {block.location for block in parsed.blocks if block.location},
        "prop": {name for block in parsed.blocks for name in block.props},
    }
    context: dict[str, dict[str, str]] = {"character": {}, "scene": {}, "prop": {}}
    for asset_type, bucket_name in (
        ("character", "characters"),
        ("scene", "scenes"),
        ("prop", "props"),
    ):
        registered = project.get(bucket_name) or {}
        for name in sorted(mentioned[asset_type]):
            key = resolve_asset_key(registered, name)
            entry = registered.get(key) if key else None
            description = str(entry.get("description") or "").strip() if isinstance(entry, dict) else ""
            if description:
                context[asset_type][name] = description
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
            plan = _normalize_plan(ScenePlanResult.model_validate(payload))
            _validate_plan_coverage(plan, beats)
            _validate_plan_quality(plan, assets)
        except (json.JSONDecodeError, ValidationError, _PlanRejected) as exc:
            rejection = str(exc)
            continue
        return plan
    raise _PlanRejected(rejection or "AI 返回的分镜不可用")


def _read_cached_plan(
    cache_path: Path, beats: list[dict[str, Any]], assets: dict[str, dict[str, str]]
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
    registered_assets: dict[str, list[str]]
    asset_conflicts: list[dict[str, str]]
    assets_without_description: list[dict[str, str]]
    declared_seconds: int | None = None
    actual_seconds: int | None = None
    duration_shortfall: bool | None = None
    planning_strength: int = 2
    planning_cached: bool = False
    ai_authoring: bool = False
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


def assets_missing_description(project: Mapping[str, Any], markdown: str) -> list[dict[str, str]]:
    """分镜前置检查：本集源稿引用的资产里，缺外观描述的清单。

    未登记或描述为空均阻断；源稿写明外观不能替代资产步骤的登记与描述规范化。
    """
    parsed = parse_short_drama_episode(markdown)
    mentioned = {
        "character": sorted({name for block in parsed.blocks for name in block.characters}),
        "scene": sorted({block.location for block in parsed.blocks if block.location}),
        "prop": sorted({name for block in parsed.blocks for name in block.props}),
    }
    missing: list[dict[str, str]] = []
    for asset_type, bucket_name in (("character", "characters"), ("scene", "scenes"), ("prop", "props")):
        bucket = project.get(bucket_name) or {}
        for name in mentioned[asset_type]:
            key = resolve_asset_key(bucket, name) if isinstance(bucket, Mapping) else None
            entry = bucket.get(key) if isinstance(bucket, Mapping) and key else None
            if not isinstance(entry, Mapping):
                missing.append({"type": asset_type, "name": name, "reason": "unregistered"})
                continue
            if not str(entry.get("description") or "").strip():
                missing.append({"type": asset_type, "name": name, "reason": "existing_blank"})
    return missing


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
    save_source: bool = True,
    expected_source_revision: str | None = None,
    confirm_artifacts: bool = True,
    planning_strength: int = 2,
    ai_planning: bool = False,
    force_regenerate: bool = False,
    regenerate_scene_ids: Sequence[str] = (),
) -> ShortDramaImportResult:
    """解析源稿并提交视觉制作状态；Web 与兼容导入入口共用此命令。

    ``ai_planning`` 是显式的选择加入开关：只有分镜页「AI 分镜」才置 True 走文本模型，
    旧单集/批量导入的兼容路径保持机械切分、不调用模型。工厂交稿直接保存 Markdown 工件，
    不调用此命令。

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
    if not save_source:
        if current_source is None:
            raise ShortDramaWorkflowError("short_drama_source_missing", "本集没有已保存的 Markdown 源稿")
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
        if ai_planning:
            if not beats:
                raise ShortDramaWorkflowError("short_drama_ai_planning_failed", "源稿没有可分镜的动作或台词")
            missing_assets = assets_missing_description(project, markdown)
            if missing_assets:
                raise ShortDramaWorkflowError(
                    "short_drama_assets_not_ready",
                    "请先完成资产步骤，以下资产尚未登记或缺少描述："
                    + "、".join(item["name"] for item in missing_assets),
                )
        assets = _asset_context(project, parsed)
        existing = await asyncio.to_thread(_load_current_script, projects, project_name, episode)
        existing_meta = existing.get("short_drama_meta") if existing else None
        options = existing_meta.get("ai_storyboard_options") if isinstance(existing_meta, dict) else None
        preserve_current = (
            ai_planning
            and not force_regenerate
            and not regenerate_scene_ids
            and current_source is not None
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
                        # 无机械回落：返修穷尽仍不合格就报错，绝不把骨架提示词当 AI 分镜交付。
                        raise ShortDramaWorkflowError("short_drama_ai_planning_failed", str(exc)) from exc
                    except Exception as exc:
                        raise ShortDramaWorkflowError(
                            "short_drama_ai_planning_failed", f"AI 分镜规划失败: {exc}"
                        ) from exc
                    else:
                        await run_sync_transaction(_write_cached_plan, cache_path, plan)
            if plan is None:
                if ai_planning:
                    raise ShortDramaWorkflowError("short_drama_ai_planning_failed", "AI 分镜未返回可用规划")
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
                for scene, planned in zip(script["scenes"], plan.scenes, strict=True):
                    scene["segment_break"] = planned.segment_break
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
        # 两态可分辨：AI 分镜成功（smart，含按分镜重写）/ 兼容导入的机械骨架（fast，非用户可选）。
        "mode": "fast" if not ai_planning else "smart",
        "strength": planning_strength,
        "force_regenerate": force_regenerate,
    }
    if regenerate_scene_ids:
        meta["ai_storyboard_options"]["regenerated_scene_ids"] = list(regenerate_scene_ids)
    names = referenced_asset_names(script)
    descriptions = referenced_asset_descriptions(parsed)
    registered: dict[str, list[str]] = {}
    conflicts: list[dict[str, str]] = []
    assets_without_description: list[dict[str, str]] = []

    def _update_project(current_project: dict[str, Any]) -> None:
        current_source = load_short_drama_source(projects, project_name, episode)
        if current_source is None or current_source.revision != source.revision:
            raise ShortDramaSourceConflict("short drama source changed during script commit")
        if ai_planning:
            # 资产在模型调用期间可能被删除或清空，提交时在项目锁内再次复核。
            missing = assets_missing_description(current_project, markdown)
            if missing:
                raise ShortDramaWorkflowError(
                    "short_drama_assets_not_ready",
                    "请先完成资产步骤：" + "、".join(item["name"] for item in missing),
                )
            return
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
        registered_assets=registered,
        asset_conflicts=conflicts,
        assets_without_description=assets_without_description,
        declared_seconds=declared if declared and actual != declared else None,
        actual_seconds=actual if declared and actual != declared else None,
        duration_shortfall=actual < declared if declared and actual != declared else None,
        planning_strength=planning_strength,
        planning_cached=cached,
        ai_authoring=ai_planning and (plan is not None or regenerated is not None or cached),
        regenerated_scenes=regenerated,
    )


__all__ = [
    "ShortDramaImportResult",
    "ShortDramaWorkflowError",
    "assets_missing_description",
    "import_short_drama_episode",
    "script_has_media_artifacts",
]
