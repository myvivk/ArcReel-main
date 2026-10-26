"""短剧工厂 Markdown 到正式剧本的统一导入工作流。"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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
    SceneAssets,
    assets_missing_description,
    beat_catalog,
    build_episode_brief,
    convert_planned_scenes,
    fragment_scene_names,
    parse_short_drama_episode,
    resolve_scene_assets,
    scene_beat_groups,
    scene_place_name,
)
from lib.script.short_drama_source import (
    ShortDramaSourceConflict,
    load_short_drama_source,
    short_drama_source_relative_path,
)
from lib.script.shot_prompt_standard import (
    BREAK_MARKER_HINT,
    CAMERA_MOTIONS,
    CAMERA_POSITIONS,
    CONTINUITY_MARKER_HINT,
    SHOT_SIZES,
    handoff_problem,
    lint_shot_prompt,
    shot_blocks,
)
from lib.speech.speech_rate import estimate_spoken_seconds
from server.agent_runtime.tool_progress import (
    GATE_STORYBOARD_PLAN,
    GATE_STORYBOARD_REWRITE,
    MAX_REASON_CHARS,
    GateOutcome,
    ProgressReporter,
    QualityGateRound,
)

logger = logging.getLogger(__name__)


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
_PLAN_CACHE_VERSION = "v7"
#: 一次规划最多调用几次文本模型：首轮 + 带拒绝原因的返修轮。校验不通过就继续让 AI 改写优化，
#: 所有轮次都失败则拒绝提交，不交付机械切分结果。3 轮是实测上限之外的止损：返修轮只重发
#: 失败的那几幕，再拖长只是把同一幕反复磨。
_PLAN_ATTEMPTS = 3
#: 单 scene 局部修复连续失败达到这个次数后回退全量；全局结构错误则立即回退全量。
_SCENE_REPAIR_CONSECUTIVE_LIMIT = 2

#: MiniMax 等供应商的 ``json_schema`` 不可靠：分镜改走轻量 Markdown，本地解析后再进同一套质量闸。
_MARKDOWN_SCENE_HEADER = re.compile(
    r"^【\s*分镜\s*(\d+)\s*[｜|]\s*beats?\s*[:：]?\s*([^｜|\n]+?)\s*[｜|]\s*(\d+)\s*(?:秒|s)\s*】\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_MARKDOWN_REWRITE_HEADER = re.compile(
    r"^【\s*分镜\s*([^｜|\]\n]+?)\s*[｜|]\s*(\d+)\s*(?:秒|s)\s*】\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_MARKDOWN_DESCRIPTION = re.compile(r"^\s*\**\s*(?:描述|画面)\s*[:：]\s*(.*)$")

_PLANNING_STRENGTH_HINTS = {
    1: "保守：只做视觉化与格式落地，不改动作顺序、不碰台词。",
    2: "标准：可理顺神态与动作的镜头归属；台词由系统从 beats 派生到 utterances，image_prompt 不写台词。",
    3: "增强：可调整动作分配与镜头节奏，但 utterances 仍逐字照抄原稿；image_prompt 不写台词。",
}

#: ``image_prompt`` 的格式约定（D-3 / D-10 统一标准）：规划与按分镜重写共用同一份指令文案，
#: 景别 / 机位 / 运镜词表取自 :mod:`lib.script.shot_prompt_standard` 的单一真相源，与校验闸同源。
_IMAGE_PROMPT_FORMAT_RULES = (
    "5. image_prompt：单段多行文本，每个镜头一段，镜头之间空一行，格式固定（示例；第 2 镜首句用了承接词「又」）：",
    "【镜头1｜中景】平视机位，缓推。沈昭@[沈昭]站在床侧，左手把一张 A4 白纸举到台灯下，纸面被灯光穿透，"
    "签名栏的字迹透出淡红；床头监护仪亮着绿色波形线，周敏@[周敏]卧在床上，双眼闭着，呼吸平缓。",
    "【镜头2｜近景】平视机位，固定。沈昭@[沈昭]又把白纸往上抬了半寸，指腹压住签名栏边缘，"
    "眉心收紧，下颌绷直；台灯的暖光从纸背透过来，把她半张脸压进阴影里。",
    f"  - 每段以「【镜头N｜景别】」起头，N 从 1 连续递增，只有一个镜头也写【镜头1】；"
    f"景别只能取 {'/'.join(SHOT_SIZES)}。",
    f"  - 景别之后的首句写机位与运镜（如「平视机位，缓推。」）：机位只能取 {'/'.join(CAMERA_POSITIONS)}；"
    f"运镜只能取 {'/'.join(CAMERA_MOTIONS)}。不要照抄“景别、机位、运镜”这几个字。",
    "  - 正文是连贯叙述句：主体（实名 + 具体动作 + 神态）→ 环境道具细节 → 光线氛围一笔，各占一层；"
    "不写台词：严禁出现「台词：」「对白：」「画外音：」原句行；台词只由 utterances 承载，"
    "画面只写看得见的内容（可写启唇说话、冷笑、画外传来宣读声等动作/环境描述）。",
    "  - 资产引用只写「名字@[登记名]」（衍生写「角色/衍生@[角色/衍生]」），只能引用本幕 beats 里出现的资产；"
    "每幕至少用一个「场景名@[登记场景名]」指认本幕场景；其余已登记但不在本幕出场的资产写裸名，"
    "不要写 token。禁止“角色@[...]”这类泛化前缀，禁止把资产外观整份抄进正文；"
    "每段第一个主体写实名，句内后续才可用代词。",
    "  - 镜头数由内容决定：12–15 秒一幕常见 3–5 个，平静对话幕 2 个也够，动作/对峙密集幕 4–6 个；"
    "禁止把同一个动作拆成雷同镜头凑数。",
    "  - 不要写 Reference_Images、Style、Avoid、视觉参考 等最终包装字段。",
)

#: 切点规则：切点必须落在叙事关节上，不允许按字数 / 时长均分凑幕。
_CUT_RULE = (
    "切点只允许落在：场边界、时间跳（前世/今生、闪回与现实的切换）、或主体切换之后的第一个新动作；"
    "不得按字数或时长均分，不得把同一个连续动作劈成两幕。"
)

#: 事实约束：画面里的穿戴、持物、道具形状与来路只能来自本集事实卡与 beats。
_FACT_RULE = (
    "画面只写「本集事实卡」与 beats 支持的事实：人物穿戴与持物、道具形状与来路、空间陈设以卡片为准；"
    "卡片没写的不得写成既成事实（例如谁把东西放在哪、东西从哪来）。"
)

#: 连续性规则：幕内逐镜承接，幕间首镜接上一幕末镜；断点必须显式写出来。
_CONTINUITY_RULE = (
    "连续性（闸门按字面判，别只写语义）：同一幕内从第 2 条镜头起，首句必须出现下列承接词或位移动作之一——"
    f"{CONTINUITY_MARKER_HINT}；出现其一即算承接，一个都没有就判不合格。"
    "每一幕的首镜要接住上一幕末镜的人物、视线或声源；确实换场或跳时间，就在首镜写断点标记"
    f"（{BREAK_MARKER_HINT}）；断点场景同样只认这几个词。不要写成互不相干的两段。"
)

#: 断点标记：与 `_CONTINUITY_RULE` 配套，幕间校验用它判定「显式切点」。
_BREAK_MARKER = re.compile("硬切|黑场|光转|闪回|跳切|转场")

#: 台词时长规则：每幕台词按语速估算，必须给镜头留出画面时间。
_DIALOGUE_RULE = (
    "台词时长：每幕台词按约 5 字/秒估算念完需要多少秒，必须不超过「幕时长 − 镜头数 × 1.2 秒」"
    "（每个镜头至少要有 1.2 秒画面）。说话多的幕减少镜头数，动作多的幕才多切；"
    "装不下就拆幕或精简台词，不许把 12 秒台词塞进 15 秒的 7 个镜头里。"
)

#: 每个镜头至少要占的画面秒数（台词时长上界里的扣减项）。
MIN_SHOT_SECONDS = 1.2

#: 关键帧规则：每幕首镜给人开始状态、末镜收稳，宫格按关键瞬间挑帧。
_FRAME_RULE = (
    "关键帧：每一幕的首镜写清起始状态（人物位置、姿态、手持物、光线），末镜收在稳定状态"
    "（动作完成后的定姿，不要停在半途），中间镜头写过程。需要当关键瞬间的镜头，在正文开头"
    "标「关键帧：」（梦境→现实、人物入场、失态、断章这类转折）；宫格优先取这些镜头，"
    "首尾必留，不按镜头数平均切。"
)


def _context_lines(brief: str, declared_seconds: int = 0) -> list[str]:
    """本集事实卡与总时长约束：规划 / 重写 / 局部修复共用同一段文案。"""
    lines: list[str] = []
    if declared_seconds > 0:
        tolerance = max(1, round(declared_seconds * 0.05))
        lines.append(
            f"全部 scene 的 duration_seconds 合计必须为 {declared_seconds} 秒（±{tolerance} 秒）；"
            "不足或超出会被整批拒绝，不得按均分凑数。"
        )
        low, high = STORY_SCENE_WINDOW_SECONDS
        if declared_seconds >= low:
            min_scenes = (declared_seconds + high - 1) // high
            max_scenes = declared_seconds // low
            lines.append(
                f"按 {low}–{high} 秒窗口倒推，本集应切 {min_scenes}–{max_scenes} 幕；幕数由总时长决定，不要沿用固定幕数。"
            )
    if brief:
        lines.extend(
            [
                "本集事实卡（已登记事实，不得改写）：",
                brief,
                "事实卡里未登记为场景的地点（如钩子或记忆里的旧地）不是本集空间：只能以光、声、叠影的碎片出现，"
                "不得当作独立场景拍成完整空间；本幕空间以登记场景 token 为准。",
            ]
        )
    return lines


class _PlanRejected(ValueError):
    """AI 规划未通过结构 / 丰满度校验，可将原因回喂模型返修。"""

    def __init__(self, detail: str, rounds: Sequence[QualityGateRound] = ()) -> None:
        super().__init__(detail)
        #: 本次闸门跑过的全部轮次；失败路径据此把返修过程交给工具结果。
        self.rounds = tuple(rounds)


async def _report_round(
    on_round: ProgressReporter | None,
    round_: QualityGateRound,
) -> None:
    """回报一轮质量闸结果。

    纯观测：宿主未注入回调时零调用；回报抛异常或卡住（最多等 2 秒）都只记日志，
    绝不影响本轮判定、返修循环与最终落盘。调用点必须在本轮 ``try/except ValueError``
    之外，避免回报失败被误判成「校验拒绝」而白烧一轮。
    """
    if on_round is None:
        return
    try:
        async with asyncio.timeout(2):
            await on_round(round_)
    except Exception:
        logger.exception(
            "质量闸进度回报失败 gate=%s episode=%s attempt=%s",
            round_.gate,
            round_.episode,
            round_.attempt,
        )


def _quality_gate_round(
    *,
    gate: str,
    episode: int,
    attempt: int,
    outcome: GateOutcome,
    elapsed_ms: int,
    reason: str | None = None,
) -> QualityGateRound:
    """构造一轮进度载荷（拒绝原因按展示上限截断）。"""
    return QualityGateRound(
        gate=gate,
        episode=episode,
        attempt=attempt,
        max_attempts=_PLAN_ATTEMPTS,
        outcome=outcome,
        elapsed_ms=elapsed_ms,
        reason=reason[:MAX_REASON_CHARS] if reason else None,
    )


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


def _parse_beat_ids(spec: str) -> list[int]:
    """把 ``1-8`` / ``1、2、3-5`` 解析成 beat 序号列表；不能解析时让规划返修。"""
    text = spec.strip()
    if not text:
        raise ValueError("beats 范围为空")

    def expand(part: str) -> list[int]:
        match = re.fullmatch(r"(\d+)\s*[-–~]\s*(\d+)", part)
        if match:
            start, end = (int(value) for value in match.groups())
            if end < start:
                raise ValueError(f"beats 范围起止反了：{part}")
            return list(range(start, end + 1))
        if part.isdigit():
            return [int(part)]
        raise ValueError(f"无法解析 beats 范围：{part}")

    whole = re.fullmatch(r"(\d+)\s*[-–~]\s*(\d+)", text)
    if whole:
        return expand(text)
    ids: list[int] = []
    for part in re.split(r"[、,，]", text):
        part = part.strip()
        if part:
            ids.extend(expand(part))
    if not ids:
        raise ValueError("beats 范围为空")
    return ids


def _markdown_block_parts(block: str, label: str) -> tuple[str, str]:
    """从分镜块里取 ``描述：`` 之后的一句和全部 ``【镜头N｜景别】`` 提示词行。"""
    lines = [line.strip() for line in block.splitlines()]
    description = ""
    image_lines: list[str] = []
    for line in lines:
        if not line:
            continue
        if not description:
            match = _MARKDOWN_DESCRIPTION.match(line)
            if match:
                description = match.group(1).strip().strip("*").strip()
                continue
        if line.startswith("【镜头") or image_lines:
            image_lines.append(line)
    if not description:
        for line in lines:
            if line and not line.startswith("【镜头") and not _MARKDOWN_DESCRIPTION.match(line):
                description = line
                break
    if not description:
        raise ValueError(f"{label} 缺少“描述：”或画面描述")
    if not image_lines:
        raise ValueError(f"{label} 缺少【镜头N｜景别】提示词")
    return description, "\n".join(image_lines)


def _parse_markdown_plan(text: str) -> ScenePlanResult:
    """把轻量 Markdown 分镜解析成 ``ScenePlanResult``；失败即抛可回喂的 ValueError。"""
    text = strip_json_code_fences(text).strip()
    matches = list(_MARKDOWN_SCENE_HEADER.finditer(text))
    if not matches:
        raise ValueError("没有找到【分镜N｜beats …｜N秒】标题")
    scenes: list[ScenePlan] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        description, image_prompt = _markdown_block_parts(text[match.end() : end], f"分镜{match.group(1)}")
        scenes.append(
            ScenePlan(
                beat_ids=_parse_beat_ids(match.group(2)),
                duration_seconds=int(match.group(3)),
                scene_description=description,
                image_prompt=image_prompt,
            )
        )
    return ScenePlanResult(scenes=scenes)


def _parse_markdown_rewrite(text: str) -> SceneRewriteResult:
    """把轻量 Markdown 重写稿解析成 ``SceneRewriteResult``。"""
    text = strip_json_code_fences(text).strip()
    matches = list(_MARKDOWN_REWRITE_HEADER.finditer(text))
    if not matches:
        raise ValueError("没有找到【分镜 scene_id｜N秒】标题")
    scenes: list[SceneRewrite] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        description, image_prompt = _markdown_block_parts(text[match.end() : end], f"分镜 {match.group(1).strip()}")
        scenes.append(
            SceneRewrite(
                scene_id=match.group(1).strip(),
                duration_seconds=int(match.group(2)),
                scene_description=description,
                image_prompt=image_prompt,
            )
        )
    return SceneRewriteResult(scenes=scenes)


def _use_markdown_storyboards(generator: TextGenerator) -> bool:
    """MiniMax 及其自定义端点不强制执行 json_schema，分镜改走 Markdown 本地解析。"""
    model = str(getattr(generator, "model", "") or "").casefold()
    provider = str(getattr(generator, "provider_id", "") or "").casefold()
    return "minimax" in model or "minimax" in provider


def _project_registered_names(project: Mapping[str, Any]) -> dict[str, set[str]]:
    """项目资产表全量登记名（按类型）。token 硬闸的「已登记」判定用全量，不用本集子集。"""
    return {
        "character": {str(name) for name in (project.get("characters") or {}) if str(name).strip()},
        "scene": {str(name) for name in (project.get("scenes") or {}) if str(name).strip()},
        "prop": {str(name) for name in (project.get("props") or {}) if str(name).strip()},
    }


def _resolve_plan_assets(
    plan: ScenePlanResult,
    parsed: ParsedEpisode,
    registered: Mapping[str, Collection[str]],
) -> list[SceneAssets]:
    """解析整份计划每幕的实际出场资产；场景/资产无法解析属于全局结构失败。"""
    groups = [scene.beat_ids for scene in plan.scenes]
    image_prompts = [scene.image_prompt for scene in plan.scenes]
    plan_scenes = [{"scenes": list(scene.scenes)} for scene in plan.scenes]
    return resolve_scene_assets(
        parsed,
        groups,
        image_prompts,
        registered=registered,
        plan_scenes=plan_scenes,
    )


def _plan_handoff_problems(plan: ScenePlanResult, registered: Mapping[str, Collection[str]]) -> list[tuple[int, str]]:
    """幕间承接：除换场 / 跳时间外，后一幕首镜必须接住前一幕末镜的在场人物。

    判据是「人物交集」或「断点标记」二选一：能接就接；确实要切，就在首镜写出断点
    （硬切 / 黑场 / 光转 / 闪回），把切点显式化，而不是让观众自己脑补。
    """
    characters = {str(name) for name in registered.get("character", ())}
    problems: list[tuple[int, str]] = []
    for index in range(1, len(plan.scenes)):
        previous, current = plan.scenes[index - 1], plan.scenes[index]
        if current.segment_break:
            continue
        if problem := handoff_problem(previous.image_prompt, current.image_prompt, character_names=characters):
            problems.append((index + 1, f"scene {index + 1}：{problem}"))
    return problems


def _plan_lint_problems(
    plan: ScenePlanResult,
    scene_assets: Collection[SceneAssets],
    registered: Mapping[str, Collection[str]],
) -> list[tuple[int, str]]:
    """逐幕 lint + 幕间承接，返回 ``(1-based scene 序号, 可回喂原因)``；空列表表示全部通过。"""
    names = {name for group in registered.values() for name in group}
    problems: list[tuple[int, str]] = []
    for scene_index, (scene, allowed) in enumerate(zip(plan.scenes, scene_assets, strict=True), 1):
        if problem := lint_shot_prompt(
            scene.image_prompt,
            registered_names=names,
            allowed_names=allowed.allowed_names(),
            character_names=registered.get("character", ()),
        ):
            problems.append((scene_index, f"scene {scene_index}：{problem}"))
    problems.extend(_plan_handoff_problems(plan, registered))
    return problems


def _validate_plan_quality(
    plan: ScenePlanResult,
    parsed: ParsedEpisode,
    registered: Mapping[str, Collection[str]],
) -> list[SceneAssets]:
    """结构与丰满度同为交付闸，不合格时整体返修（校验口径见 ``lint_shot_prompt``）。

    token 闸用「本幕实际出场资产」作为 ``allowed_names``：先把每幕 scenes/characters/props
    按 beats 与提示词场景 token 解析出来，再校验 token ⊆ 本幕清单 ⊆ 登记名。返回的解析结果
    交给转换层复用，落盘字段与闸门允许清单因此同源。
    """
    scene_assets = _resolve_plan_assets(plan, parsed, registered)
    problems = _plan_lint_problems(plan, scene_assets, registered)
    if problems:
        raise _PlanRejected(problems[0][1])
    _validate_scene_timing(plan)
    return scene_assets


def _validate_scene_timing(plan: ScenePlanResult) -> None:
    """镜头数必须与幕时长匹配：一镜塞十几秒、十几镜塞一个短幕都不行。

    ``lint_shot_prompt`` 只看画面质量，不看时间；台词预算只对有台词的幕生效。这里补上
    与时长无关的通用约束：每镜至少 2 秒、至多 6 秒。
    """

    for index, scene in enumerate(plan.scenes, 1):
        shots = max(len(shot_blocks(scene.image_prompt)), 1)
        low, high = shots * 2, shots * 6
        if not low <= scene.duration_seconds <= high:
            raise _PlanRejected(
                f"scene {index} 有 {shots} 个镜头，{scene.duration_seconds} 秒对不上"
                f"（每镜 2–6 秒，合理区间 {low}–{high} 秒）；请增减镜头或调整时长"
            )


def _validate_plan_coverage(plan: ScenePlanResult, beats: list[dict[str, Any]], *, declared_seconds: int = 0) -> None:
    """结构护栏（D-5）：beat 一一覆盖、不劈断、时长落窗口、整集合计对上源稿标注。

    ``declared_seconds`` 取源稿 ``【本集时长】``：合计缺口超过 5% 即拒绝。只报不拦会让
    分镜按 12–15 秒均分凑幕，把钩子与断章的秒数一起摊平。
    """
    flat_ids = [beat_id for scene in plan.scenes for beat_id in scene.beat_ids]
    if flat_ids != list(range(1, len(beats) + 1)):
        raise _PlanRejected("beat_ids 必须从 1 开始、连续覆盖全部 beats，不重不漏也不劈断")
    for scene_index, scene in enumerate(plan.scenes, 1):
        low, high = STORY_SCENE_WINDOW_SECONDS
        if not low <= scene.duration_seconds <= high:
            raise _PlanRejected(f"scene {scene_index} 时长 {scene.duration_seconds} 秒不在 {low}–{high} 秒窗口内")
    if declared_seconds > 0:
        actual = sum(scene.duration_seconds for scene in plan.scenes)
        tolerance = max(1, round(declared_seconds * 0.05))
        if abs(actual - declared_seconds) > tolerance:
            raise _PlanRejected(
                f"各幕时长合计 {actual} 秒，与源稿标注的 {declared_seconds} 秒相差超过 {tolerance} 秒；"
                "请按源稿时长重新分配各幕秒数，不要均分凑数"
            )
    if problem := _dialogue_overflow(plan, beats):
        raise _PlanRejected(problem)


def _validate_one_scene(
    scene: SceneRewrite,
    beats: list[dict[str, Any]],
    registered: Mapping[str, Collection[str]],
    allowed_by_scene: Mapping[str, Collection[str]] | None,
    durations: Mapping[str, int],
) -> str | None:
    """单条分镜稿的护栏：时长窗口 + 回显时长 + 提示词格式闸。合格返回 ``None``。"""

    low, high = STORY_SCENE_WINDOW_SECONDS
    if not low <= scene.duration_seconds <= high:
        return f"时长 {scene.duration_seconds} 秒不在 {low}–{high} 秒窗口内"
    expected = durations.get(scene.scene_id)
    if expected is not None and scene.duration_seconds != expected:
        return f"时长由切分给定为 {expected} 秒，必须原样回显"
    names = {name for group in registered.values() for name in group}
    allowed = allowed_by_scene.get(scene.scene_id, names) if allowed_by_scene else names
    return lint_shot_prompt(
        _normalize_image_prompt(scene.image_prompt),
        registered_names=names,
        allowed_names=allowed,
        character_names=registered.get("character", ()),
    )


def _fragment_problem(prompt: str, fragment_scenes: Sequence[str]) -> str | None:
    """首镜必须写出本集碎片空间；合格返回 ``None``。"""

    if not fragment_scenes:
        return None
    if any(scene_place_name(name) in prompt for name in fragment_scenes):
        return None
    if any(marker in prompt for marker in ("冷光", "叠影", "闪回", "画外", "虚影", "残影")):
        return None
    places = "、".join(scene_place_name(name) for name in fragment_scenes)
    return f"没有写出本集的碎片空间（{places}）：必须有冷光 / 叠影 / 画外声，不能省略"


def _require_fragment_in_rewrite(
    rewrite: SceneRewriteResult, hook_scene_id: str, fragment_scenes: Sequence[str]
) -> None:
    """首镜必须写出本集碎片空间：没有冷光/叠影/画外声，后面视频就没有这个画面。

    只在本批重写里包含首镜时才判——重写中间某一幕时，碎片由首镜负责。
    """

    if not fragment_scenes or not hook_scene_id:
        return
    target = next((scene for scene in rewrite.scenes if scene.scene_id == hook_scene_id), None)
    if target is None:
        return
    prompt = target.image_prompt
    if any(scene_place_name(name) in prompt for name in fragment_scenes):
        return
    if any(marker in prompt for marker in ("冷光", "叠影", "闪回", "画外", "虚影", "残影")):
        return
    places = "、".join(scene_place_name(name) for name in fragment_scenes)
    raise _PlanRejected(f"首镜没有写出本集的碎片空间（{places}）：必须有冷光 / 叠影 / 画外声，不能省略")


def _dialogue_overflow(plan: ScenePlanResult, beats: list[dict[str, Any]]) -> str | None:
    """台词量必须在该幕时长里念得完，且给每个镜头留出画面时间。

    只判「台词 ≤ 幕时长」不够：12 秒台词塞进 15 秒、还要切 7 个镜头，每个镜头只剩 0.4 秒，
    成片必然吞词或抢拍。这里按「幕时长 − 镜头数 × 1.2 秒」给说话留预算，说话多的幕只能少切
    镜头——这正是剪辑上的真实约束。
    """
    for index, scene in enumerate(plan.scenes, 1):
        lines = [
            str(beats[beat_id - 1].get("text") or "")
            for beat_id in scene.beat_ids
            if 0 < beat_id <= len(beats) and beats[beat_id - 1].get("kind") == "dialogue"
        ]
        if not lines:
            continue
        spoken = sum(estimate_spoken_seconds(line) for line in lines)
        shots = max(len(shot_blocks(scene.image_prompt)), 1)
        budget = scene.duration_seconds - shots * MIN_SHOT_SECONDS
        if spoken > budget:
            raise _PlanRejected(
                f"scene {index} 的台词念完约需 {spoken:.1f} 秒，该幕 {scene.duration_seconds} 秒里 "
                f"{shots} 个镜头至少要占 {shots * MIN_SHOT_SECONDS:.1f} 秒，"
                f"只剩 {max(budget, 0.0):.1f} 秒给说话；请拆幕、精简台词或减少镜头数"
            )
    return None


def _validate_rewrite(
    rewrite: SceneRewriteResult,
    requested: list[tuple[str, list[dict[str, Any]]]],
    registered: Mapping[str, Collection[str]],
    allowed_by_scene: Mapping[str, Collection[str]] | None,
    fixed_durations: Mapping[str, int] | None = None,
) -> None:
    """按分镜重写稿的护栏：scene_id 一一对应、时长落窗口、提示词过格式闸。

    ``fixed_durations`` 非空时（机械切分模式）时长由代码给定，模型必须原样回显。
    token 闸的出场清单取现有条目已落盘的 characters_in_scene/scenes/props（与编写路径的
    per-entry 口径一致）。存量脏字段会让正确的绣楼 token 被拒，这是在提示用户做整集
    ``force_regenerate``，而不是放宽闸门。
    """
    requested_ids = [scene_id for scene_id, _beats in requested]
    returned_ids = [scene.scene_id for scene in rewrite.scenes]
    if returned_ids != requested_ids:
        raise _PlanRejected(f"scene_id 必须与待重写的分镜一一对应（期望 {requested_ids}，实际 {returned_ids}）")
    names = {name for group in registered.values() for name in group}
    for (scene_id, _beats), scene in zip(requested, rewrite.scenes, strict=True):
        low, high = STORY_SCENE_WINDOW_SECONDS
        if not low <= scene.duration_seconds <= high:
            raise _PlanRejected(f"分镜 {scene_id} 时长 {scene.duration_seconds} 秒不在 {low}–{high} 秒窗口内")
        if fixed_durations is not None and scene_id in fixed_durations:
            expected = fixed_durations[scene_id]
            if scene.duration_seconds != expected:
                raise _PlanRejected(f"分镜 {scene_id} 的时长由切分给定为 {expected} 秒，必须原样回显")
        allowed = allowed_by_scene[scene_id] if allowed_by_scene is not None and scene_id in allowed_by_scene else names
        if problem := lint_shot_prompt(
            _normalize_image_prompt(scene.image_prompt),
            registered_names=names,
            allowed_names=allowed,
            character_names=registered.get("character", ()),
        ):
            raise _PlanRejected(f"分镜 {scene_id}：{problem}")


def _scene_allowed_names(scene: Mapping[str, Any]) -> set[str]:
    """现有分镜条目已落盘的出场资产清单（partial rewrite 的 token 闸）。"""
    allowed: set[str] = set()
    for field in ("characters_in_scene", "scenes", "props"):
        values = scene.get(field)
        if isinstance(values, list):
            allowed.update(str(name) for name in values if isinstance(name, str) and name.strip())
    return allowed


def _require_preservable_script(
    script: Mapping[str, Any],
    parsed: ParsedEpisode,
    registered: Mapping[str, Collection[str]],
) -> None:
    """``force=False`` 复用现有脚本前的合规复核。

    旧版产出可能含台词行，或 scenes/characters/props 与提示词不一致，或各幕时长合计与源稿
    标注相差超过 5%；直接复用会让脏提示词绕过新质量闸并继续污染宫格/K 帧。这里按新口径复算
    并要求与落盘字段一致，不一致就明确要求 ``force_regenerate=true``，而不是静默复用。
    """
    scenes = script.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("现有分镜为空")
    declared = int(getattr(parsed, "total_duration", 0) or 0)
    if declared > 0:
        stored = sum(int(scene.get("duration_seconds") or 0) for scene in scenes if isinstance(scene, Mapping))
        tolerance = max(1, round(declared * 0.05))
        if abs(stored - declared) > tolerance:
            raise ValueError(f"现有分镜时长合计 {stored} 秒，与源稿标注的 {declared} 秒相差超过 {tolerance} 秒")
    try:
        groups = scene_beat_groups(parsed, scenes)
    except ValueError as exc:
        raise ValueError(f"现有分镜与当前源稿不对应：{exc}") from exc
    image_prompts = [_scene_prompt_text(scene) for scene in scenes]
    resolved = resolve_scene_assets(parsed, groups, image_prompts, registered=registered)
    names = {name for group in registered.values() for name in group}
    for index, (scene, allowed) in enumerate(zip(scenes, resolved, strict=True), 1):
        if not isinstance(scene, Mapping):
            raise ValueError(f"第 {index} 个分镜不是对象")
        if problem := lint_shot_prompt(
            _scene_prompt_text(scene),
            registered_names=names,
            allowed_names=allowed.allowed_names(),
            character_names=registered.get("character", ()),
        ):
            raise ValueError(f"第 {index} 个分镜：{problem}")
        for field, resolved_names in (
            ("characters_in_scene", allowed.characters_in_scene),
            ("scenes", allowed.scenes),
            ("props", allowed.props),
        ):
            stored = scene.get(field)
            if not isinstance(stored, list) or set(stored) != set(resolved_names):
                raise ValueError(
                    f"第 {index} 个分镜的 {field} 与提示词实际出场资产不一致"
                    f"（落盘 {stored}，应为 {list(resolved_names)}）"
                )


def _preserve_requires_regeneration(detail: str) -> ShortDramaWorkflowError:
    return ShortDramaWorkflowError(
        "short_drama_ai_planning_failed",
        f"{detail}；现有分镜不符合当前质量闸，请用 force_regenerate=true 重新生成整集分镜（局部重写不会修元数据）",
    )


def _rewrite_prompt(
    *,
    requested: list[tuple[str, list[dict[str, Any]]]],
    assets: dict[str, list[str]],
    planning_strength: int,
    previous_prompts: dict[str, str],
    rejection: str | None,
    markdown: bool = False,
    brief: str = "",
    new_scenes: bool = False,
) -> str:
    """按分镜重写的提示词：beat 分组固定不变，只重写画面描述与分镜提示词。

    ``new_scenes`` 为真时（机械切分新建分镜）没有 old_image_prompt 可参照，token 改为
    指向 assets 清单，标题也不再写「已有分镜」。
    """
    if new_scenes:
        intro_markdown = (
            "你是专业短剧摄影分镜师。下面每一幕的 beat 分组与时长已由切分给定，"
            "你只写这一幕的描述与分镜提示词，严格按 Markdown 输出。"
        )
        intro_json = (
            "你是专业短剧摄影分镜师。下面每一幕的 beat 分组与时长已由切分给定，"
            "你只写这一幕的 scene_description 与 image_prompt。"
        )
        token_rule = (
            "5. 每个分镜至少用一个「场景名@[登记场景名]」指认本幕场景；"
            "token 只能写 assets 清单里的登记名，其他资产写裸名。"
        )
        tail = "待写的分镜（beat 分组与时长已给定）："
    else:
        intro_markdown = (
            "你是专业短剧摄影分镜师。用户选定了若干已有分镜要求重写；beat 分组保持不变，"
            "只重写每个分镜的描述与提示词，严格按 Markdown 输出。"
        )
        intro_json = (
            "你是专业短剧摄影分镜师。用户选定了若干已有分镜要求重写；beat 分组保持不变，"
            "只重写每个分镜的 scene_description 与 image_prompt。"
        )
        token_rule = (
            "5. 每个分镜至少用一个「场景名@[登记场景名]」指认本幕场景；token 只能写该分镜"
            " old_image_prompt 里出现过的登记名（本次重写不改变资产清单），其他资产写裸名。"
        )
        tail = "待重写的分镜（old_image_prompt 只作参考，可整体重写）："
    if markdown:
        rules = [
            intro_markdown,
            "输出格式（每个分镜一段；不要 JSON、不要代码围栏）：",
            "【分镜 E1S01｜14秒】",
            "描述：一句画面描述",
            "【镜头1｜中景】平视机位，缓推。……",
            "硬约束：",
            "1. 标题里的 scene_id 逐字等于输入分镜的 id，不增不减不改名。",
            "2. 标题里的时长取 12–15 的整数。",
            "3. “描述：”改写成镜头可读的画面（具体动作 + 神态 + 环境），只写画面看得见的内容，不写台词。",
            "4. 台词不归你管，不要输出；提示词覆盖的画面必须仍承载输入 beats 的剧情。",
            token_rule,
            *_IMAGE_PROMPT_FORMAT_RULES,
            "6. 只输出上述 Markdown；不要输出 JSON、代码围栏、utterances、video_prompt 或解释。"
            "严禁输出「台词：」「对白：」「画外音：」行。",
            _FACT_RULE,
            _CONTINUITY_RULE,
            _DIALOGUE_RULE,
            _FRAME_RULE,
            f"优化强度={planning_strength}。{_PLANNING_STRENGTH_HINTS[planning_strength]}",
            *_context_lines(brief),
            f"assets={json.dumps(assets, ensure_ascii=False)}（已登记的出场资产名字清单，引用 token 只能写这些名字）",
            tail,
        ]
    else:
        rules = [
            intro_json,
            "硬约束：",
            "1. 逐条输出 scenes，scene_id 逐字等于输入分镜的 id，不增不减不改名。",
            "2. duration_seconds：取 12–15 的整数。",
            "3. scene_description：改写成镜头可读的画面（具体动作 + 神态 + 环境），只写画面看得见的内容，不写台词。",
            "4. utterances 里的台词不归你管，不要输出；image_prompt 覆盖的画面必须仍承载输入 beats 的剧情。",
            token_rule,
            *_IMAGE_PROMPT_FORMAT_RULES,
            _FACT_RULE,
            _CONTINUITY_RULE,
            _DIALOGUE_RULE,
            _FRAME_RULE,
            f"优化强度={planning_strength}。{_PLANNING_STRENGTH_HINTS[planning_strength]}",
            *_context_lines(brief),
            f"assets={json.dumps(assets, ensure_ascii=False)}（已登记的出场资产名字清单，引用 token 只能写这些名字）",
            tail,
        ]
    plan_rules = "\n".join(rules)
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
        suffix = "完整 Markdown" if markdown else "完整 scenes"
        prompt += f"\n上一轮输出未通过校验：{rejection}。请修正后重新输出{suffix}。"
    return prompt


async def _request_rewrite(
    *,
    project_name: str,
    episode: int,
    requested: list[tuple[str, list[dict[str, Any]]]],
    assets: dict[str, list[str]],
    registered: Mapping[str, Collection[str]],
    allowed_by_scene: Mapping[str, Collection[str]],
    planning_strength: int,
    previous_prompts: dict[str, str],
    on_round: ProgressReporter | None = None,
    brief: str = "",
    hook_scene_id: str = "",
    fragment_scenes: Sequence[str] = (),
    fixed_durations: Mapping[str, int] | None = None,
    new_scenes: bool = False,
) -> SceneRewriteResult:
    """按分镜写/重写：逐分镜验收，只把不合格的那几条再发一次，合格的直接留下。

    早先的写法是「一条不合格就整批重来」——8 幕里 1 幕没过，模型就得把另外 7 幕再写一遍，
    既费时又可能把已经合格的写坏。现在每轮只重发 pending 里的分镜。
    """
    generator = await TextGenerator.create(
        TextTaskType.SCRIPT, project_name=project_name, purpose=CallPurpose.SCRIPT_GENERATION
    )
    markdown = _use_markdown_storyboards(generator)
    durations = dict(fixed_durations or {})
    by_id = dict(requested)
    accepted: dict[str, SceneRewrite] = {}
    pending = [scene_id for scene_id, _beats in requested]
    rejection: str | None = None
    rounds: list[QualityGateRound] = []
    for attempt in range(1, _PLAN_ATTEMPTS + 1):
        requested_now = [(scene_id, by_id[scene_id]) for scene_id in pending]
        prompt = _rewrite_prompt(
            requested=requested_now,
            assets=assets,
            planning_strength=planning_strength,
            previous_prompts=previous_prompts,
            rejection=rejection,
            markdown=markdown,
            brief=brief,
            new_scenes=new_scenes,
        )
        if durations:
            fixed_lines = "\n".join(
                f"- {scene_id}：{durations[scene_id]} 秒" for scene_id in pending if scene_id in durations
            )
            prompt += (
                "\n\n## 时长由切分给定（必须原样回显）\n"
                f"{fixed_lines}\n"
                "不要改动这些秒数：幕已经按 12–15 秒切好，你只负责写这一幕的镜头与画面。"
            )
        started = time.monotonic()
        generated = await generator.generate(
            BackendTextGenerationRequest(
                prompt=prompt,
                response_schema=None if markdown else SceneRewriteResult,
                max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
            ),
            project_name=project_name,
            require_complete=True,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        try:
            if markdown:
                rewrite = _parse_markdown_rewrite(generated.text)
            else:
                payload = json.loads(strip_json_code_fences(generated.text))
                rewrite = SceneRewriteResult.model_validate(payload)
            returned = {
                scene.scene_id: scene.model_copy(update={"image_prompt": _normalize_image_prompt(scene.image_prompt)})
                for scene in rewrite.scenes
            }
            if set(returned) != set(pending):
                raise _PlanRejected(f"返回的分镜必须与本次待写的分镜一一对应（期望 {pending}，实际 {list(returned)}）")
            problems: list[str] = []
            for scene_id in pending:
                scene = returned[scene_id]
                reason = _validate_one_scene(scene, by_id[scene_id], registered, allowed_by_scene, durations)
                if not reason and scene_id == hook_scene_id:
                    reason = _fragment_problem(scene.image_prompt, fragment_scenes)
                if reason:
                    problems.append(f"{scene_id}：{reason}")
                else:
                    accepted[scene_id] = scene
            if problems:
                pending = [scene_id for scene_id in pending if scene_id not in accepted]
                raise _PlanRejected("；".join(problems))
        except ValueError as exc:
            rejection = str(exc)
            round_ = _quality_gate_round(
                gate=GATE_STORYBOARD_REWRITE,
                episode=episode,
                attempt=attempt,
                outcome="rejected",
                elapsed_ms=elapsed_ms,
                reason=rejection,
            )
            await _report_round(on_round, round_)
            rounds.append(round_)
            continue
        round_ = _quality_gate_round(
            gate=GATE_STORYBOARD_REWRITE,
            episode=episode,
            attempt=attempt,
            outcome="passed",
            elapsed_ms=elapsed_ms,
        )
        await _report_round(on_round, round_)
        if len(accepted) == len(requested):
            return SceneRewriteResult(scenes=[accepted[scene_id] for scene_id, _beats in requested])
    raise _PlanRejected(rejection or "AI 返回的分镜重写不可用", rounds)


def _asset_context(project: dict[str, Any], parsed: ParsedEpisode) -> dict[str, list[str]]:
    """本集源稿提到、且已登记的资产名字清单；只给名字与登记状态，不喂外观描述（D-10）。

    外观一致性由参考图体系承载：喂了外观，模型就会把它整份抄进提示词正文。外观事实改由
    :func:`_asset_descriptions` 以「本集事实卡」的形式进入规划 prompt——那是事实边界，不是正文，
    格式规则仍禁止把外观抄进 image_prompt。
    """
    mentioned = {
        "character": {name for block in parsed.blocks for name in block.characters},
        "scene": {location for block in parsed.blocks for location in block.locations if location},
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


def _asset_descriptions(
    project: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """项目资产已登记的描述（角色 / 场景 / 道具），空描述不返回。

    规划模型只拿到 beats 与资产名时，会按字面补道具形状、补穿戴位置（「白绫盘成同心结」
    「旧红绳在掌心」都是这么来的）。这里把已登记的事实交给 :func:`build_episode_brief` 拼成
    事实卡，让画面有据可依；卡片不进正文，格式规则仍禁止整份抄外观。
    """

    def _collect(bucket_name: str) -> dict[str, str]:
        bucket = project.get(bucket_name) or {}
        collected: dict[str, str] = {}
        if isinstance(bucket, Mapping):
            for name, record in bucket.items():
                description = record.get("description") if isinstance(record, Mapping) else None
                if isinstance(name, str) and isinstance(description, str) and description.strip():
                    collected[str(name)] = description.strip()
        return collected

    return _collect("characters"), _collect("scenes"), _collect("props")


def _plan_prompt(
    *,
    beats: list[dict[str, Any]],
    assets: dict[str, list[str]],
    planning_strength: int,
    rejection: str | None,
    markdown: bool = False,
    brief: str = "",
    declared_seconds: int = 0,
) -> str:
    """一次规划请求的提示词；返修轮把上一轮被拒原因一起喂回去。

    ``markdown=True`` 给 json_schema 不可靠的供应商：只要求轻量 Markdown 标题与提示词，
    由本地 :func:`_parse_markdown_plan` 转成同一份 ``ScenePlanResult``。
    """
    if markdown:
        lines = [
            "你是专业短剧摄影分镜师。对下面给出的全部 beats 规划分镜，严格按 Markdown 输出。",
            "输出格式（每个分镜一段；不要 JSON、不要代码围栏、不要额外解释）：",
            "【分镜1｜beats 1-8｜14秒】",
            "描述：一句画面描述",
            "【镜头1｜中景】平视机位，缓推。……",
            "【镜头2｜近景】……",
            "硬约束：",
            "1. 标题里的 beats 必须从 1 开始、按顺序连续覆盖每一个 beat，不重不漏；"
            "绝不劈开一个动作行或一句台词；【场景】行若是全集清单（后文 beat 才出现地点切换），"
            "按叙事语义（说话人更替、地点切换、情绪转折）切分，不要按字数均分；"
            "若 [场景] 块内确有 beat，再优先按块边界切分。",
            "2. 每个标题里的时长取 12–15 的整数。",
            "3. “描述：”只写镜头可读的画面（具体动作 + 神态 + 环境），不写台词。",
            "4. 每个分镜至少一个【镜头N｜景别】块；每个分镜至少用一个「场景名@[登记场景名]」"
            "指认本幕场景（取 assets.scene 的登记名），token 只写本幕 beats 里出现的资产，"
            "其余已登记资产写裸名。台词不归你改写，画面必须仍承载输入 beats 的剧情。",
            *_IMAGE_PROMPT_FORMAT_RULES,
            "6. 只输出上述 Markdown；不要输出 JSON、代码围栏、characters_in_scene、video_prompt 或解释。"
            "严禁输出「台词：」「对白：」「画外音：」行——台词只由 utterances 承载。",
            "7. " + _CUT_RULE,
            "8. " + _FACT_RULE,
            "9. " + _CONTINUITY_RULE,
            "10. " + _DIALOGUE_RULE,
            "11. " + _FRAME_RULE,
            f"优化强度={planning_strength}。{_PLANNING_STRENGTH_HINTS[planning_strength]}",
            *_context_lines(brief, declared_seconds),
            f"assets={json.dumps(assets, ensure_ascii=False)}（已登记的出场资产名字清单，引用 token 只能写这些名字）",
            f"beats={json.dumps(beats, ensure_ascii=False)}",
        ]
    else:
        lines = [
            "你是专业短剧摄影分镜师。对下面给出的全部 beats 一次性输出 scenes 数组。",
            "硬约束：",
            "1. scene.beat_ids：从 1 开始、按顺序连续覆盖每一个 beat，不重不漏；"
            "绝不劈开一个动作行或一句台词；【场景】行若是全集清单（后文 beat 才出现地点切换），"
            "按叙事语义（说话人更替、地点切换、情绪转折）切分，不要按字数均分；"
            "若 [场景] 块内确有 beat，再优先按块边界切分。",
            "2. scene.duration_seconds：每幕取 12–15 的整数。",
            "3. scene.scene_description：把这些拍改写成镜头可读的画面（具体动作 + 神态 + 环境），"
            "只写画面看得见的内容，不写台词。",
            "4. scene.utterances：逐字照抄输入拍里的台词（dialogue 带 speaker，画外音 voiceover），不得改字、不得增删。",
            *_IMAGE_PROMPT_FORMAT_RULES,
            "6. scene.scenes：只填本幕场景的登记名，不得沿用最后一个 [场景] 块；"
            "characters_in_scene / props 由系统从本幕 beats 派生，可省略；"
            "image_prompt 严禁出现「台词：」「对白：」「画外音：」行。video_prompt 一律不输出。",
            "7. " + _CUT_RULE,
            "8. " + _FACT_RULE,
            "9. " + _CONTINUITY_RULE,
            "10. " + _DIALOGUE_RULE,
            "11. " + _FRAME_RULE,
            f"优化强度={planning_strength}。{_PLANNING_STRENGTH_HINTS[planning_strength]}",
            *_context_lines(brief, declared_seconds),
            f"assets={json.dumps(assets, ensure_ascii=False)}（已登记的出场资产名字清单，引用 token 只能写这些名字）",
            f"beats={json.dumps(beats, ensure_ascii=False)}",
        ]
    prompt = "\n".join(lines)
    if rejection:
        suffix = "完整 Markdown" if markdown else "完整 scenes"
        prompt += f"\n上一轮输出未通过校验：{rejection}。请修正后重新输出{suffix}。"
    return prompt


def _scene_repair_assets_payload(allowed: SceneAssets) -> dict[str, list[str]]:
    """局部修复 prompt 里的本幕资产清单：只给本幕允许的 token 名字。"""
    return {
        "scene": list(allowed.scenes),
        "character": list(allowed.characters_in_scene),
        "prop": list(allowed.props),
    }


def _scene_repair_prompt(
    *,
    scene_index: int,
    group: Sequence[int],
    duration: int,
    scene_description: str,
    image_prompt: str,
    allowed: SceneAssets,
    beats: list[dict[str, Any]],
    rejection: str | None,
    markdown: bool,
    brief: str = "",
) -> str:
    """单 scene 局部修复 prompt：固定 beat 范围与时长，只回传本幕 beats/资产/旧稿/原因。"""
    start, end = group[0], group[-1]
    scene_beats = [beats[value - 1] for value in group]
    assets_json = json.dumps(_scene_repair_assets_payload(allowed), ensure_ascii=False)
    beats_json = json.dumps(scene_beats, ensure_ascii=False)
    common = [
        f"只修复第 {scene_index} 个分镜；其他分镜保持不动，不要重发整集。",
        f"本幕 beat 分组固定为 {start}-{end}（共 {len(group)} 拍），时长固定为 {duration} 秒，"
        "不得增删 beat、不得改名、不得改时长。",
        *_IMAGE_PROMPT_FORMAT_RULES,
        "严禁在 image_prompt 中输出「台词：」「对白：」「画外音：」行；台词只由 utterances 承载。",
        _FACT_RULE,
        _CONTINUITY_RULE,
        _DIALOGUE_RULE,
        _FRAME_RULE,
        *_context_lines(brief),
        f"本幕允许引用的资产={assets_json}；token 只能写本幕允许的登记名，其他已登记资产写裸名。",
        f"本幕 beats={beats_json}",
        f"旧 scene_description={scene_description}",
        f"旧 image_prompt=\n{image_prompt}",
    ]
    if markdown:
        lines = [
            "你是专业短剧摄影分镜师。只重写一个不合格分镜，严格按 Markdown 输出。",
            "输出格式（只输出这一个分镜块；不要 JSON、代码围栏或解释）：",
            f"【分镜{scene_index}｜beats {start}-{end}｜{duration}秒】",
            "描述：一句画面描述",
            "【镜头1｜中景】平视机位，缓推。……",
            *common,
            "只输出上述一个 Markdown 分镜块，不要输出其它分镜。",
        ]
    else:
        lines = [
            "你是专业短剧摄影分镜师。只重写一个不合格分镜。",
            "只输出一个 JSON 对象（不是数组、不要代码围栏、不要解释），字段："
            "beat_ids、duration_seconds、scene_description、image_prompt。",
            f"beat_ids 固定为 {json.dumps(list(group))}；duration_seconds 固定为 {duration}。",
            *common,
            "只输出这一个 scene 对象，不要输出 scenes 数组或其它分镜。",
        ]
    prompt = "\n".join(lines)
    if rejection:
        prompt += f"\n该分镜上一轮未通过校验：{rejection}。请只修正这一个分镜。"
    return prompt


def _parse_scene_repair(text: str, *, markdown: bool) -> ScenePlan:
    """解析单 scene 修复稿：Markdown 只接受一个分镜块，JSON 只接受一个 scene 对象。"""
    if markdown:
        result = _parse_markdown_plan(text)
        if len(result.scenes) != 1:
            raise ValueError("局部修复只允许返回一个分镜块")
        return result.scenes[0]
    payload = json.loads(strip_json_code_fences(text))
    if isinstance(payload, list):
        if len(payload) != 1:
            raise ValueError("局部修复只允许返回一个 scene 对象")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise ValueError("局部修复必须返回一个 scene 对象")
    return ScenePlan.model_validate(payload)


def _validate_scene_repair(
    scene: ScenePlan,
    *,
    group: Sequence[int],
    duration: int,
    registered: Mapping[str, Collection[str]],
    allowed: SceneAssets,
) -> ScenePlan:
    """局部修复稿必须保持 beat_ids/duration 完全不变，并通过本幕 lint。"""
    if scene.beat_ids != list(group):
        raise ValueError(f"beat_ids 必须保持 {list(group)} 不变，实际 {scene.beat_ids}")
    if scene.duration_seconds != duration:
        raise ValueError(f"duration_seconds 必须保持 {duration} 不变，实际 {scene.duration_seconds}")
    if scene.scenes and set(scene.scenes) != set(allowed.scenes):
        raise ValueError(f"scene.scenes 必须保持本幕场景 {list(allowed.scenes)} 不变，实际 {scene.scenes}")
    normalized = scene.model_copy(update={"image_prompt": _normalize_image_prompt(scene.image_prompt)})
    names = {name for bucket in registered.values() for name in bucket}
    if problem := lint_shot_prompt(
        normalized.image_prompt,
        registered_names=names,
        allowed_names=allowed.allowed_names(),
        character_names=registered.get("character", ()),
    ):
        raise ValueError(problem)
    return normalized


def _replace_plan_scene(plan: ScenePlanResult, scene_index: int, scene: ScenePlan) -> ScenePlanResult:
    """用局部修复稿替换候选计划的一幕，其他 scene 对象原样保留。"""
    scenes = list(plan.scenes)
    scenes[scene_index] = scene
    return plan.model_copy(update={"scenes": scenes})


async def _request_plan(
    *,
    project_name: str,
    episode: int,
    parsed: ParsedEpisode,
    beats: list[dict[str, Any]],
    assets: dict[str, list[str]],
    registered: Mapping[str, Collection[str]],
    planning_strength: int,
    on_round: ProgressReporter | None = None,
    brief: str = "",
    declared_seconds: int = 0,
) -> ScenePlanResult:
    """两级返修：全局结构失败才全量重生成；仅单 scene lint 失败时只回传该 scene。

    每轮（全量或局部修复）都消耗 ``_PLAN_ATTEMPTS`` 的一次模型调用预算，并写一条
    ``quality_gate_round``；最终成功返回的候选计划已通过完整覆盖与全 scene lint。
    """
    generator = await TextGenerator.create(
        TextTaskType.SCRIPT, project_name=project_name, purpose=CallPurpose.SCRIPT_GENERATION
    )
    markdown = _use_markdown_storyboards(generator)
    rejection: str | None = None
    rounds: list[QualityGateRound] = []
    plan: ScenePlanResult | None = None
    scene_assets: list[SceneAssets] | None = None
    pending_scene: int | None = None  # 0-based；仅当候选计划全局有效时存在
    repair_failures = 0

    for attempt in range(1, _PLAN_ATTEMPTS + 1):
        repairing = plan is not None and scene_assets is not None and pending_scene is not None
        started = time.monotonic()
        if repairing:
            assert plan is not None
            assert scene_assets is not None
            assert pending_scene is not None
            scene_index = pending_scene
            target = plan.scenes[scene_index]
            group = list(target.beat_ids)
            generated = await generator.generate(
                BackendTextGenerationRequest(
                    prompt=_scene_repair_prompt(
                        scene_index=scene_index + 1,
                        group=group,
                        duration=target.duration_seconds,
                        scene_description=target.scene_description,
                        image_prompt=target.image_prompt,
                        allowed=scene_assets[scene_index],
                        beats=beats,
                        rejection=rejection,
                        markdown=markdown,
                        brief=brief,
                    ),
                    response_schema=None if markdown else ScenePlan,
                    max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
                ),
                project_name=project_name,
                require_complete=True,
            )
        else:
            generated = await generator.generate(
                BackendTextGenerationRequest(
                    prompt=_plan_prompt(
                        beats=beats,
                        assets=assets,
                        planning_strength=planning_strength,
                        rejection=rejection,
                        markdown=markdown,
                        brief=brief,
                        declared_seconds=declared_seconds,
                    ),
                    response_schema=None if markdown else ScenePlanResult,
                    max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
                ),
                project_name=project_name,
                require_complete=True,
            )
        elapsed_ms = int((time.monotonic() - started) * 1000)

        if repairing:
            assert plan is not None
            assert scene_assets is not None
            assert pending_scene is not None
            scene_index = pending_scene
            target = plan.scenes[scene_index]
            group = list(target.beat_ids)
            try:
                replacement = _parse_scene_repair(generated.text, markdown=markdown)
                replacement = _validate_scene_repair(
                    replacement,
                    group=group,
                    duration=target.duration_seconds,
                    registered=registered,
                    allowed=scene_assets[scene_index],
                )
                # 局部修复只改描述与提示词；segment_break 等未要求模型输出的字段沿用旧 scene。
                replacement = replacement.model_copy(update={"segment_break": target.segment_break})
            except ValueError as exc:
                rejection = f"分镜 {scene_index + 1} 局部修复不合格：{exc}"
                repair_failures += 1
                if repair_failures >= _SCENE_REPAIR_CONSECUTIVE_LIMIT:
                    rejection = f"{rejection}；连续 {repair_failures} 次局部修复失败，回退全量重生成"
                    plan = None
                    scene_assets = None
                    pending_scene = None
                    repair_failures = 0
                round_ = _quality_gate_round(
                    gate=GATE_STORYBOARD_PLAN,
                    episode=episode,
                    attempt=attempt,
                    outcome="rejected",
                    elapsed_ms=elapsed_ms,
                    reason=rejection,
                )
                await _report_round(on_round, round_)
                rounds.append(round_)
                continue

            candidate = _replace_plan_scene(plan, scene_index, replacement)
            try:
                _validate_plan_coverage(candidate, beats, declared_seconds=declared_seconds)
                candidate_assets = _resolve_plan_assets(candidate, parsed, registered)
            except ValueError as exc:
                rejection = f"分镜 {scene_index + 1} 局部修复后全局校验失败：{exc}；回退全量重生成"
                plan = None
                scene_assets = None
                pending_scene = None
                repair_failures = 0
                round_ = _quality_gate_round(
                    gate=GATE_STORYBOARD_PLAN,
                    episode=episode,
                    attempt=attempt,
                    outcome="rejected",
                    elapsed_ms=elapsed_ms,
                    reason=rejection,
                )
                await _report_round(on_round, round_)
                rounds.append(round_)
                continue

            problems = _plan_lint_problems(candidate, candidate_assets, registered)
            if problems:
                next_scene, reason = problems[0]
                same_scene = next_scene - 1 == scene_index
                if same_scene:
                    # 本幕替换稿仍不合格：保留修复前的候选计划，下一轮继续用旧稿重试本幕。
                    repair_failures += 1
                    pending_scene = scene_index
                else:
                    # 本幕已修好，其他幕仍不合格：保留替换稿，继续修下一幕。
                    plan = candidate
                    scene_assets = candidate_assets
                    pending_scene = next_scene - 1
                    repair_failures = 0
                rejection = reason
                if repair_failures >= _SCENE_REPAIR_CONSECUTIVE_LIMIT:
                    rejection = f"{rejection}；连续 {repair_failures} 次局部修复失败，回退全量重生成"
                    plan = None
                    scene_assets = None
                    pending_scene = None
                    repair_failures = 0
                round_ = _quality_gate_round(
                    gate=GATE_STORYBOARD_PLAN,
                    episode=episode,
                    attempt=attempt,
                    outcome="rejected",
                    elapsed_ms=elapsed_ms,
                    reason=rejection,
                )
                await _report_round(on_round, round_)
                rounds.append(round_)
                continue

            plan = candidate
            scene_assets = candidate_assets
            round_ = _quality_gate_round(
                gate=GATE_STORYBOARD_PLAN,
                episode=episode,
                attempt=attempt,
                outcome="passed",
                elapsed_ms=elapsed_ms,
            )
            await _report_round(on_round, round_)
            return plan

        try:
            if markdown:
                candidate_plan = _parse_markdown_plan(generated.text)
            else:
                payload = json.loads(strip_json_code_fences(generated.text))
                candidate_plan = ScenePlanResult.model_validate(payload)
            candidate_plan = _normalize_plan(candidate_plan)
            _validate_plan_coverage(candidate_plan, beats, declared_seconds=declared_seconds)
            candidate_assets = _resolve_plan_assets(candidate_plan, parsed, registered)
        except ValueError as exc:
            rejection = str(exc)
            plan = None
            scene_assets = None
            pending_scene = None
            repair_failures = 0
            round_ = _quality_gate_round(
                gate=GATE_STORYBOARD_PLAN,
                episode=episode,
                attempt=attempt,
                outcome="rejected",
                elapsed_ms=elapsed_ms,
                reason=rejection,
            )
            await _report_round(on_round, round_)
            rounds.append(round_)
            continue

        problems = _plan_lint_problems(candidate_plan, candidate_assets, registered)
        if problems:
            scene_index, reason = problems[0]
            plan = candidate_plan
            scene_assets = candidate_assets
            pending_scene = scene_index - 1
            rejection = reason
            repair_failures = 0
            round_ = _quality_gate_round(
                gate=GATE_STORYBOARD_PLAN,
                episode=episode,
                attempt=attempt,
                outcome="rejected",
                elapsed_ms=elapsed_ms,
                reason=rejection,
            )
            await _report_round(on_round, round_)
            rounds.append(round_)
            continue

        plan = candidate_plan
        scene_assets = candidate_assets
        round_ = _quality_gate_round(
            gate=GATE_STORYBOARD_PLAN,
            episode=episode,
            attempt=attempt,
            outcome="passed",
            elapsed_ms=elapsed_ms,
        )
        await _report_round(on_round, round_)
        return plan

    raise _PlanRejected(rejection or "AI 返回的分镜不可用", rounds)


def _read_cached_plan(
    cache_path: Path,
    beats: list[dict[str, Any]],
    parsed: ParsedEpisode,
    registered: Mapping[str, Collection[str]],
    *,
    declared_seconds: int = 0,
) -> ScenePlanResult | None:
    """读规划缓存；读不出或已不合当前校验口径就当没缓存。"""
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        plan = ScenePlanResult.model_validate(data)
        _validate_plan_coverage(plan, beats, declared_seconds=declared_seconds)
        _validate_plan_quality(plan, parsed, registered)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return plan


def _write_cached_plan(cache_path: Path, plan: ScenePlanResult) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(cache_path, plan.model_dump())


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
    declared_seconds: int | None = None
    actual_seconds: int | None = None
    duration_shortfall: bool | None = None
    planning_strength: int = 2
    planning_cached: bool = False
    ai_authoring: bool = True
    #: 本次按分镜重写（部分重生成）的分镜数；整体分镜为 None。
    regenerated_scenes: int | None = None
    #: 本次质量闸跑过的逐轮结果（缓存命中时为空，不谎报返修）。
    planning_rounds: tuple[QualityGateRound, ...] = ()

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
        if self.planning_rounds:
            result["planning_rounds"] = [round_.as_dict() for round_ in self.planning_rounds]
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


#: 机械切分：每拍预估秒数（动作拍）与目标幕时长。
_MECH_ACTION_SECONDS = 2
_MECH_TARGET_SCENE_SECONDS = 13.5


def _beat_seconds(beat: Mapping[str, Any]) -> int:
    """一拍大约占几秒：台词按语速算，动作拍按固定值。"""
    if str(beat.get("kind")) == "dialogue":
        return max(1, round(estimate_spoken_seconds(str(beat.get("text") or ""))))
    return _MECH_ACTION_SECONDS


def _mechanical_groups(beats: Sequence[Mapping[str, Any]], total_seconds: int) -> list[tuple[list[int], int]]:
    """把 beats 机械切成 12–15 秒的幕：先定幕数，再把 beat 均分，切点对齐动作拍。

    切点规则：台词不单独成幕首（台词跟它前面的动作拍同幕）；碎片拍一律做幕首。时长按
    整集秒数均分，保证总和与 declared_seconds 对得上——组幕是算术，不该交给模型。
    """
    low, high = STORY_SCENE_WINDOW_SECONDS
    count = max(1, min(len(beats), round(total_seconds / _MECH_TARGET_SCENE_SECONDS)))
    while count > 1 and total_seconds / count < low:
        count -= 1
    while total_seconds / count > high:
        count += 1
    cuts: list[int] = []
    for k in range(1, count):
        position = round(k * len(beats) / count)
        position = max(1, min(len(beats) - 1, position))
        while position in cuts:
            position += 1
        # 切点后一条不能是台词：台词要跟它前面的动作拍留在同一幕。
        while position < len(beats) and str(beats[position].get("kind")) == "dialogue":
            position += 1
        if position >= len(beats) or position in cuts:
            continue
        cuts.append(position)
    bounds = [0, *sorted(cuts), len(beats)]
    seconds = _distribute_seconds(total_seconds, len(bounds) - 1)
    return [
        (list(range(start + 1, end + 1)), duration)
        for (start, end), duration in zip(pairwise(bounds), seconds, strict=True)
    ]


def _distribute_seconds(total_seconds: int, count: int) -> list[int]:
    """把整集秒数均分到 count 幕，限制在 12–15 秒窗口内。"""
    low, high = STORY_SCENE_WINDOW_SECONDS
    if count <= 0:
        return []
    base, remainder = divmod(max(total_seconds, count * low), count)
    return [max(low, min(high, base + (1 if index < remainder else 0))) for index in range(count)]


async def _request_mechanical_plan(
    *,
    project_name: str,
    episode: int,
    parsed: ParsedEpisode,
    beats: list[dict[str, Any]],
    assets: dict[str, list[str]],
    registered: Mapping[str, Collection[str]],
    planning_strength: int,
    on_round: ProgressReporter | None,
    brief: str,
    declared_seconds: int,
) -> ScenePlanResult:
    """机械切幕 + 模型只写提示词：组幕与时长由代码给定，模型负责这一幕怎么拍。"""

    groups = _mechanical_groups(beats, declared_seconds)
    ids = [f"E{episode}S{index:02d}" for index in range(1, len(groups) + 1)]
    durations = {scene_id: seconds for scene_id, (_group, seconds) in zip(ids, groups, strict=True)}
    # 提示词还没写，无法从 token 判幕；这里放全部登记名，token 闸仍挡未登记资产，
    # 真正的本幕场景由 convert_planned_scenes 按写好的提示词判定。
    all_names = {name for bucket in registered.values() for name in bucket}
    allowed = dict.fromkeys(ids, all_names)
    requested = [
        (scene_id, [beats[index - 1] for index in group])
        for scene_id, (group, _seconds) in zip(ids, groups, strict=True)
    ]
    rewrite = await _request_rewrite(
        project_name=project_name,
        episode=episode,
        requested=requested,
        assets=assets,
        registered=registered,
        allowed_by_scene=allowed,
        planning_strength=planning_strength,
        previous_prompts={},
        on_round=on_round,
        brief=brief,
        hook_scene_id=ids[0],
        fixed_durations=durations,
        new_scenes=True,
    )
    scenes = [
        ScenePlan(
            beat_ids=list(group),
            duration_seconds=seconds,
            scene_description=result.scene_description,
            image_prompt=result.image_prompt,
        )
        for (group, seconds), result in zip(groups, rewrite.scenes, strict=True)
    ]
    return ScenePlanResult(scenes=scenes)


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
    registered: Mapping[str, Collection[str]],
    on_round: ProgressReporter | None = None,
    brief: str = "",
    fragment_scenes: Sequence[str] = (),
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
    allowed_by_scene = {
        scene_id: _scene_allowed_names(current["scenes"][indexed[scene_id]]) for scene_id in regenerate_scene_ids
    }
    assets = _asset_context(dict(project), parsed)
    for name in fragment_scenes:
        if name not in assets["scene"]:
            assets["scene"].append(name)
    try:
        rewrites = await _request_rewrite(
            project_name=project_name,
            episode=episode,
            requested=requested,
            assets=assets,
            registered=registered,
            allowed_by_scene=allowed_by_scene,
            planning_strength=planning_strength,
            previous_prompts=previous_prompts,
            on_round=on_round,
            brief=brief,
            hook_scene_id=str(current["scenes"][0].get("scene_id") or ""),
            fragment_scenes=fragment_scenes,
        )
    except _PlanRejected as exc:
        # 无机械回落：重写稿不合格就报错，绝不拿旧稿或骨架冒充重写结果。
        raise ShortDramaWorkflowError(
            "short_drama_ai_planning_failed",
            str(exc),
            params={"rounds": [round_.as_dict() for round_ in exc.rounds]},
        ) from exc
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
    # 重写只动提示词，但本幕的在场资产可能因此变了（例如提示词里第一次出现角色）。
    # 按新提示词重算选中分镜的元数据，否则角色参考图会一直缺，且只能靠全量重分镜才能修。
    prompts = [_scene_prompt_text(scene) for scene in script["scenes"]]
    resolved = resolve_scene_assets(parsed, groups, prompts, registered=registered)
    for rewrite in rewrites.scenes:
        index = indexed[rewrite.scene_id]
        scene = dict(script["scenes"][index])
        assets = resolved[index]
        scene["characters_in_scene"] = list(assets.characters_in_scene)
        scene["scenes"] = list(assets.scenes)
        scene["props"] = list(assets.props)
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
    on_round: ProgressReporter | None = None,
) -> ShortDramaImportResult:
    """从已入库的工厂源稿生成 AI 分镜，不写入正文或登记资产。

    ``regenerate_scene_ids`` 非空时走「按分镜重写」：beat 分组与未选中分镜一律不动，
    只让 AI 重写选中分镜的画面描述与提示词——已生成的媒体与手动编辑因此不会丢。
    智能分镜没有机械回落：校验不通过就带原因持续返修，重跑穷尽仍不合格才报错
    （报错即「模型/流程无法运作」，不产出次品）。

    ``on_round`` 是宿主注入的逐轮进度回调（内嵌 Agent 会话用它写事件日志）；本函数
    同时把同一批轮次收集进结果 ``planning_rounds``，工具返回后主 Agent 一次看全。
    """
    rounds: list[QualityGateRound] = []

    async def _record(round_: QualityGateRound) -> None:
        rounds.append(round_)
        if on_round is not None:
            await on_round(round_)

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
        registered = _project_registered_names(project)
        character_descriptions, scene_descriptions, prop_descriptions = _asset_descriptions(project)
        fragment_scenes = fragment_scene_names(project.get("scenes") or {}, markdown, parsed=parsed)
        for name in fragment_scenes:
            if name not in assets["scene"]:
                assets["scene"].append(name)
        brief = build_episode_brief(
            parsed,
            character_descriptions=character_descriptions,
            scene_descriptions=scene_descriptions,
            prop_descriptions=prop_descriptions,
            fragment_scenes=fragment_scenes,
        )
        declared_seconds = int(getattr(parsed, "total_duration", 0) or 0)
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
            try:
                _require_preservable_script(existing, parsed, registered)
            except ValueError as exc:
                raise _preserve_requires_regeneration(str(exc)) from exc
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
                registered=registered,
                on_round=_record,
                brief=brief,
                fragment_scenes=fragment_scenes,
            )
        else:
            cache_path = (
                projects.get_project_path(project_name)
                / "drafts"
                / f"short_drama_plan_{_PLAN_CACHE_VERSION}_e{episode}_{current_source.revision}_s{planning_strength}.json"
            )
            if not force_regenerate:
                plan = await asyncio.to_thread(
                    _read_cached_plan,
                    cache_path,
                    beats,
                    parsed,
                    registered,
                    declared_seconds=declared_seconds,
                )
                cached = plan is not None
            if plan is None:
                try:
                    plan = await _request_mechanical_plan(
                        project_name=project_name,
                        episode=episode,
                        parsed=parsed,
                        beats=beats,
                        assets=assets,
                        registered=registered,
                        planning_strength=planning_strength,
                        on_round=_record,
                        brief=brief,
                        declared_seconds=declared_seconds,
                    )
                except _PlanRejected as exc:
                    raise ShortDramaWorkflowError(
                        "short_drama_ai_planning_failed",
                        str(exc),
                        params={"rounds": [round_.as_dict() for round_ in exc.rounds]},
                    ) from exc
                except Exception as exc:
                    raise ShortDramaWorkflowError("short_drama_ai_planning_failed", f"AI 分镜规划失败: {exc}") from exc
                await run_sync_transaction(_write_cached_plan, cache_path, plan)
            scene_assets = _validate_plan_quality(plan, parsed, registered)
            script = convert_planned_scenes(
                parsed,
                episode,
                [scene.beat_ids for scene in plan.scenes],
                [scene.duration_seconds for scene in plan.scenes],
                [scene.scene_description for scene in plan.scenes],
                [scene.image_prompt for scene in plan.scenes],
                scene_assets=scene_assets,
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
        planning_rounds=tuple(rounds),
    )


__all__ = [
    "ShortDramaImportResult",
    "ShortDramaWorkflowError",
    "import_short_drama_episode",
    "script_has_media_artifacts",
]
