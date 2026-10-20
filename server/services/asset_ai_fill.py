"""资产字段 AI 补全：上下文搜集、提示词构造、模型回执解析与字段写回。

单资产端点（资产详情的「生成描述」）与集页一键链路（登记后补描述再出图）共用这一份，
两处不得各写一套提示词或写回规则。

来源优先级（D-35，集页视图与侧栏「全部资产」入口**同一口径，不按视图翻转**）：

- **剧本（工厂源稿）**：各集 ``[人物]/[场景]/[道具]`` 标记行——名字后面跟的就是外观，是
  最贴合、最权威的描述来源；本集剧本里该资产出场的场景与台词用于强化。
- **故事规划**：人物圣经 / 全剧大纲 / 连续性台账 / 分集规划，补标记行没写到的跨集背景。
- **题材补全**：项目标题与前提，前两级都没有时才据此泛写。

优先级文案与块序在 :func:`build_ai_fill_prompt` 内固定为「剧本（工厂源稿）> 故事规划 >
题材补全」；``episode`` 只用于把剧本摘录聚焦到本集，不再参与优先级翻转。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from lib.backends.providers import CallPurpose
from lib.backends.text_backends.base import (
    TextGenerationRequest as BackendTextGenerationRequest,
)
from lib.backends.text_backends.base import (
    TextOutputTruncatedError,
    TextTaskType,
)
from lib.backends.text_generator import TextGenerator
from lib.project.asset_types import (
    ALIASES_FIELD,
    ASSET_SPECS,
    DERIVATIVES_FIELD,
    record_asset_aliases,
    resolve_asset_key,
)
from lib.project.story_plan_docs import collect_story_plan
from lib.references.reference_catalog import split_derivative_reference

logger = logging.getLogger(__name__)

#: 本集摘录的预算；其他集只是补充，给得更少，避免把本集的定稿细节挤掉。
EPISODE_EXCERPT_LIMIT = 6
EPISODE_EXCERPT_CHARS = 6000
OTHER_EXCERPT_LIMIT = 4
OTHER_EXCERPT_CHARS = 4000

#: 批量补全时的并发上限：文本模型调用是 IO 密集，但一次补太多会打满供应商配额。
FILL_CONCURRENCY = 4


class AssetDerivativeFill(BaseModel):
    """AI 补全时为角色生成的单个外观变体。"""

    name: str
    description: str = ""


class AssetFillResponse(BaseModel):
    """AI 补全的结构化回执。空白字段返回空串/空数组，调用方只应用到尚为空的字段。"""

    description: str = ""
    voice_style: str = ""
    aliases: list[str] = Field(default_factory=list)
    derivatives: list[AssetDerivativeFill] = Field(default_factory=list)


def extract_json(text: str) -> Any:
    """从模型回执里取出 JSON 对象，容忍 ```json 围栏与前后说明文字。"""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _reference_names(values: Any) -> set[str]:
    """引用数组里的名字集合；``本体/衍生`` 同时算作本体被提到。"""
    names: set[str] = set()
    for value in values or []:
        if not isinstance(value, str):
            continue
        names.add(value.strip())
        owner = split_derivative_reference(value.strip())[0].strip()
        if owner:
            names.add(owner)
    return names


def scene_mentions(scene: dict[str, Any], asset_type: str, name: str) -> bool:
    """判断某场景是否引用了该资产（用于截取补全上下文）。

    口径与 :func:`server.routers.episode_assets.script_asset_names` 一致：各骨架的引用数组
    写法不同（``props`` / ``props_in_scene``、``dialogue`` / ``utterances`` 等），都要认。
    """
    if asset_type == "character":
        in_cast = name in _reference_names(scene.get("characters_in_scene"))
        speakers = _reference_names(
            line.get("speaker") for line in (scene.get("dialogue") or scene.get("utterances") or [])
        )
        return in_cast or name in speakers
    if asset_type == "scene":
        return (
            name in (scene.get("scene") or "")
            or name in (scene.get("scene_description") or "")
            or name in _reference_names(scene.get("scenes"))
        )
    if asset_type == "prop":
        return name in _reference_names(scene.get("props_in_scene")) or name in _reference_names(scene.get("props"))
    return False


def _short_drama_sources(project_dir: Path) -> list[Path]:
    """``source/short_drama/`` 下的工厂 Markdown（系列级参考稿 + 单集原文）。"""
    root = project_dir / "source" / "short_drama"
    return sorted(root.rglob("*.md")) if root.is_dir() else []


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _marker_lines(sources: list[Path], marker: str, name: str, limit: int = 8) -> list[str]:
    """各集 ``[marker]`` 行里出现该资产名的原文行，带文件名便于模型定位。"""
    hits: list[str] = []
    pattern = re.compile(rf"^\[{marker}\][：:]\s*(.+)$")
    for path in sources:
        for line in _read_text(path).splitlines():
            match = pattern.match(line.strip())
            if match and name in match.group(1):
                hits.append(f"{path.name}｜{line.strip()}")
                break
        if len(hits) >= limit:
            break
    return hits


def collect_script_source(project_dir: Path, asset_type: str, name: str) -> list[str]:
    """剧本（工厂源稿）里对该资产的记载：各集 ``[人物]/[场景]/[道具]`` 标记行原文。

    标记行形如 ``名称｜外观描述``——名字后面跟的就是外观，是生成描述的主素材：AI 据此**强化 /
    规范**，而不是凭空编外观。人物圣经、全剧大纲这类规划文档不算剧本，归
    :func:`server.services.story_plan_docs.collect_story_plan` 作第二优先级来源。
    """
    sources = _short_drama_sources(project_dir)
    marker = {"character": "人物", "scene": "场景", "prop": "道具"}.get(asset_type)
    if marker is None:
        return []
    return _marker_lines(sources, marker, name)


def _append_excerpts(
    project_dir: Path,
    episode_entry: Mapping[str, Any],
    asset_type: str,
    name: str,
    sink: list[dict[str, Any]],
    limit: int,
    max_chars: int,
) -> None:
    """把某集正式脚本里提到该资产的场景追加进 ``sink``，受条数与字符数双重上限约束。"""
    script_file = episode_entry.get("script_file")
    if not isinstance(script_file, str) or not script_file:
        return
    # 集元数据的 script_file 有的带 scripts/ 前缀、有的只有文件名，两种都按项目目录解析。
    relative = script_file if script_file.startswith("scripts/") else f"scripts/{script_file}"
    try:
        script = json.loads((project_dir / relative).read_text(encoding="utf-8"))
    except Exception:
        return
    total = sum(len(str(item)) for item in sink)
    for scene in script.get("scenes") or []:
        if len(sink) >= limit or total > max_chars:
            return
        if not isinstance(scene, dict) or not scene_mentions(scene, asset_type, name):
            continue
        excerpt = {
            "scene": scene.get("scene", ""),
            "scene_description": scene.get("scene_description", ""),
            "dialogue": [
                f"{line.get('speaker', '')}: {line.get('line') or line.get('text') or ''}"
                for line in (scene.get("dialogue") or scene.get("utterances") or [])
                if isinstance(line, dict) and (line.get("line") or line.get("text"))
            ],
        }
        sink.append(excerpt)
        total += len(str(excerpt))


def collect_asset_context(
    project_dir: Path,
    project: Mapping[str, Any],
    asset_type: str,
    name: str,
    entry: Mapping[str, Any],
    *,
    episode: int | str | None = None,
) -> dict[str, Any]:
    """按「剧本（工厂源稿）> 故事规划 > 题材补全」三级搜集资产生成描述的上下文。

    分组与 :func:`build_ai_fill_prompt` 的块序一一对应：``script_source``（工厂源稿标记行）、
    ``episode_excerpts``（本集剧本的场景与台词）、``story_plan``（故事规划四类文档）。
    ``episode`` 只把剧本摘录聚焦到本集，**不再翻转优先级**（D-35）。
    """
    episode_key = None if episode is None else str(episode)
    episodes = [item for item in (project.get("episodes") or []) if isinstance(item, dict)]
    episode_excerpts: list[dict[str, Any]] = []
    other_excerpts: list[dict[str, Any]] = []
    if episode_key is None:
        for item in episodes:
            _append_excerpts(
                project_dir, item, asset_type, name, episode_excerpts, EPISODE_EXCERPT_LIMIT, EPISODE_EXCERPT_CHARS
            )
    else:
        focused = [item for item in episodes if str(item.get("episode")) == episode_key]
        for item in focused:
            _append_excerpts(
                project_dir, item, asset_type, name, episode_excerpts, EPISODE_EXCERPT_LIMIT, EPISODE_EXCERPT_CHARS
            )
        for item in episodes:
            if str(item.get("episode")) == episode_key:
                continue
            _append_excerpts(
                project_dir, item, asset_type, name, other_excerpts, OTHER_EXCERPT_LIMIT, OTHER_EXCERPT_CHARS
            )
    return {
        "title": project.get("title") or "",
        "premise": project.get("premise") or project.get("logline") or "",
        "existing": {k: entry.get(k) for k in ("description", "voice_style", ALIASES_FIELD, DERIVATIVES_FIELD)},
        "episode_excerpts": episode_excerpts,
        "other_excerpts": other_excerpts,
        "script_source": collect_script_source(project_dir, asset_type, name),
        "story_plan": collect_story_plan(project_dir, asset_type, name, episode=episode),
        "focus_episode": episode_key,
    }


def _excerpt_block(excerpts: Sequence[Mapping[str, Any]]) -> str:
    return "\n\n".join(
        f"【场景】{ex.get('scene', '')}\n场景描写：{ex.get('scene_description', '')}\n"
        + "\n".join(ex.get("dialogue") or [])
        for ex in excerpts
    )


def build_ai_fill_prompt(asset_type: str, name: str, context: Mapping[str, Any]) -> str:
    """构造资产字段补全提示词：只补空白字段，给出剧本上下文与期望的 JSON 结构。"""
    type_label = {"character": "角色", "scene": "场景", "prop": "道具", "product": "商品"}.get(asset_type, asset_type)
    existing = context.get("existing") or {}
    existing_parts: list[str] = []
    if existing.get("description"):
        existing_parts.append(f"- description（已有，勿改）：{existing['description']}")
    if asset_type == "character" and existing.get("voice_style"):
        existing_parts.append(f"- voice_style（已有，勿改）：{existing['voice_style']}")
    if existing.get(ALIASES_FIELD):
        existing_parts.append(f"- aliases（已有，勿改）：{', '.join(existing[ALIASES_FIELD])}")
    if existing.get(DERIVATIVES_FIELD):
        derivs = ", ".join(f"{k}（{v.get('description', '')}）" for k, v in existing[DERIVATIVES_FIELD].items())
        existing_parts.append(f"- derivatives（已有，勿改）：{derivs}")
    existing_block = "\n".join(existing_parts) if existing_parts else "（无已有内容）"

    script_source = context.get("script_source") or []
    script_source_block = "\n".join(str(item) for item in script_source)
    story_plan = context.get("story_plan") or []
    story_plan_block = "\n\n".join(str(item) for item in story_plan)

    from lib.prompts.prompt_templates.builtin import builtin_templates

    description_rule = builtin_templates.read_partial("shared/asset_description_rule").source
    scope = (
        f"1) description：必须严格遵守以下资产描述规范，不得补充规范未支持的事实：\n{description_rule}\n"
        "2) aliases：剧本中对该资产的其他称呼（别称、外号、代称）；不要把资产名本身算作别名。\n"
    )
    if asset_type == "character":
        scope += (
            "3) voice_style：该角色的声音/台词风格（语速、语气、音色倾向），用于配音参考。\n"
            "4) derivatives：该角色可能出现的外观变体（如不同服装/年龄/状态），每条含 name 与 description；"
            "没有合适变体时返回空数组。\n"
        )
    else:
        scope += "（voice_style、derivatives 不适用，返回空字符串/空数组即可。）\n"

    episode_excerpts = context.get("episode_excerpts") or []
    other_excerpts = context.get("other_excerpts") or []
    episode_key = context.get("focus_episode")

    # D-35：块序与优先级文案固定为「剧本（工厂源稿）> 故事规划 > 题材补全」，
    # 集页视图与侧栏「全部资产」入口同一口径，不按 episode 翻转。
    episode_label = f"｜第 {episode_key} 集" if episode_key else ""
    episode_block = _excerpt_block(episode_excerpts) or "（本集剧本中未检索到该资产的直接引用）"
    other_block = _excerpt_block(other_excerpts)
    script_block = (
        f"工厂源稿标记行（名称｜外观描述，是生成描述的主素材，据此强化而非重写）：\n{script_source_block}\n"
        if script_source_block
        else "（工厂源稿里没有该资产的标记行）\n"
    )
    evidence = (
        f"【剧本（工厂源稿）{episode_label}｜最高优先级依据】\n"
        f"（该资产出场的场景与台词；与下面任何内容冲突时以剧本为准）\n{episode_block}\n\n"
        f"{script_block}\n"
        + (f"其他集剧本摘录（同属剧本，补充参考）：\n{other_block}\n\n" if other_block else "")
        + f"【故事规划｜次要依据】\n"
        f"（来源：人物圣经 / 全剧大纲 / 连续性台账 / 分集规划；补标记行没写到的跨集背景）\n"
        f"{story_plan_block or '（无故事规划记载，按剧本与题材补全）'}\n\n"
        f"【题材补全｜兜底】\n"
        f"（项目标题与前提；前两级都没有记载时才据此泛写，不得编造与剧本冲突的事实）\n"
        f"项目标题：{context.get('title') or '（未知）'}｜项目前提：{context.get('premise') or '（未知）'}\n\n"
    )
    priority = "剧本（工厂源稿）> 故事规划 > 题材补全。"

    return (
        "你是短视频创作平台的资产字段补全助手。下面给出一个项目资产的信息与剧本上下文，"
        "请补全其中尚为空白的字段。\n\n"
        f"资产类型：{type_label}\n资产名：{name}\n"
        f"项目标题：{context.get('title') or '（未知）'}\n"
        f"项目前提：{context.get('premise') or '（未知）'}\n\n"
        f"{evidence}"
        f"已有字段（保留，不要修改或重复）：\n{existing_block}\n\n"
        f"优先级：{priority}请补全以下空白字段（已有内容绝不覆盖）：\n"
        f"{scope}\n"
        "只输出一个 JSON 对象，结构如下（不输出任何解释文字）：\n"
        '{"description": "", "voice_style": "", "aliases": [], "derivatives": [{"name": "", "description": ""}]}'
    )


async def generate_asset_fill(
    project_name: str, asset_type: str, name: str, context: Mapping[str, Any]
) -> AssetFillResponse:
    """调文本模型补全该资产的空白字段；只回执结果，不写盘（写回交给调用方批量做）。"""
    prompt = build_ai_fill_prompt(asset_type, name, context)
    generator = await TextGenerator.create(
        TextTaskType.SCRIPT, project_name=project_name, purpose=CallPurpose.SCRIPT_GENERATION
    )
    raw = await generator.generate(
        BackendTextGenerationRequest(prompt=prompt, response_schema=AssetFillResponse, max_output_tokens=4000),
        project_name=project_name,
    )
    return AssetFillResponse.model_validate(extract_json(raw.text))


def apply_asset_fill(entry: dict[str, Any], asset_type: str, name: str, fill: AssetFillResponse) -> list[str]:
    """把补全结果写进资产条目，**只填空字段、绝不覆盖**；返回本次填上的字段名。"""
    spec = ASSET_SPECS.get(asset_type)
    supports_derivatives = bool(spec.supports_derivatives) if spec else False
    sheet_field = spec.sheet_field if spec else ""
    filled: list[str] = []
    if not entry.get("description") and fill.description:
        entry["description"] = fill.description
        filled.append("description")
    if asset_type == "character" and not entry.get("voice_style") and fill.voice_style:
        entry["voice_style"] = fill.voice_style
        filled.append("voice_style")
    if not (entry.get(ALIASES_FIELD) or []) and fill.aliases:
        entry[ALIASES_FIELD] = []
        record_asset_aliases(entry, list(fill.aliases), asset_name=name)
        filled.append(ALIASES_FIELD)
    if asset_type == "character" and supports_derivatives:
        table = entry.get(DERIVATIVES_FIELD) or {}
        if not table and fill.derivatives:
            for derivative in fill.derivatives:
                dname = (derivative.name or "").strip()
                if not dname:
                    continue
                dkey = resolve_asset_key(table, dname) or dname
                table[dkey] = {"description": derivative.description or "", sheet_field: ""}
            entry[DERIVATIVES_FIELD] = table
            filled.append(DERIVATIVES_FIELD)
    return filled


def assets_without_description(
    project: Mapping[str, Any], names_by_type: Mapping[str, Sequence[str]]
) -> list[tuple[str, str]]:
    """本集引用到、但描述为空的 ``(asset_type, 登记名)``；补全只针对这些条目。"""
    targets: list[tuple[str, str]] = []
    for asset_type, names in names_by_type.items():
        spec = ASSET_SPECS.get(asset_type)
        if spec is None:
            continue
        bucket = project.get(spec.bucket_key) or {}
        for name in names:
            key = resolve_asset_key(bucket, name)
            if key is None:
                continue
            entry = bucket.get(key)
            if isinstance(entry, dict) and not str(entry.get("description") or "").strip():
                targets.append((asset_type, key))
    return targets


async def fill_assets(
    project: Mapping[str, Any],
    project_dir: Path,
    targets: Sequence[tuple[str, str]],
    project_name: str,
    *,
    episode: int | str | None = None,
) -> tuple[dict[tuple[str, str], AssetFillResponse], list[dict[str, str]]]:
    """并发补全一批缺描述的资产，返回 ``(成功回执, 失败名单)``。

    只调模型、不写盘：写回由调用方在一次项目锁里批量完成，避免 N 次落盘与互相覆盖。
    """
    if not targets:
        return {}, []
    semaphore = asyncio.Semaphore(FILL_CONCURRENCY)

    async def _one(asset_type: str, name: str) -> tuple[tuple[str, str], AssetFillResponse]:
        spec = ASSET_SPECS[asset_type]
        bucket = (project.get(spec.bucket_key) or {}).get(name) or {}
        context = collect_asset_context(project_dir, project, asset_type, name, bucket, episode=episode)
        async with semaphore:
            fill = await generate_asset_fill(project_name, asset_type, name, context)
        return (asset_type, name), fill

    results = await asyncio.gather(*(_one(asset_type, name) for asset_type, name in targets), return_exceptions=True)
    filled: dict[tuple[str, str], AssetFillResponse] = {}
    failed: list[dict[str, str]] = []
    for target, result in zip(targets, results, strict=True):
        if isinstance(result, BaseException):
            reason = "truncated" if isinstance(result, TextOutputTruncatedError) else "generation_failed"
            logger.warning("Asset fill failed: project=%s %s/%s reason=%s", project_name, target[0], target[1], reason)
            failed.append({"asset_type": target[0], "name": target[1], "reason": reason})
            continue
        filled[result[0]] = result[1]
    return filled, failed


__all__ = [
    "FILL_CONCURRENCY",
    "AssetDerivativeFill",
    "AssetFillResponse",
    "apply_asset_fill",
    "assets_without_description",
    "build_ai_fill_prompt",
    "collect_asset_context",
    "collect_script_source",
    "collect_story_plan",
    "extract_json",
    "fill_assets",
    "generate_asset_fill",
    "scene_mentions",
]
