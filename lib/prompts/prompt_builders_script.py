"""Visual prompt authoring for factory storyboards."""

from collections.abc import Mapping

from lib.project.asset_types import ALIASES_FIELD
from lib.prompts.prompt_rules.asset_appearance import iter_asset_appearances
from lib.prompts.prompt_templates.builtin import builtin_templates
from lib.script.grid.keyframe_parser import shot_scene_text
from lib.script.grid.shot_models import ShotGrid

# ---------------------------------------------------------------------------
# 两段式分层文案（见 ADR 0041）：script_plan（normalize）= 内容、prompt_authoring（drama）= 视觉。
#
# 内容抽取前移到 script_plan：分镜边界、出场资产、逐字口播 utterances、原文锚 source_text、
# 视觉改编描述 scene_description 一次定稿，并按 source_kind 切「改编 / 提取」口径。
# prompt_authoring 只补视觉层（image_prompt / video_prompt），按 scene_id 透传内容、不再识别口播、
# 不分 source_kind——故 prompt_authoring 文案无 novel/screenplay 分支。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def _neutralize_tags(value: str) -> str:
    """中和动态文本里的尖括号：novel_text / 资产名出现 </segments> 等标签序列时，避免打散
    标签化 prompt 的块结构。属 prompt 鲁棒性——prompt_authoring 输出仍由 response_schema 强制，无安全边界。
    """
    return value.replace("<", "＜").replace(">", "＞")


def render_video_prompt_inputs(scene: str, grid: ShotGrid | None) -> list[str]:
    """视频提示词编写的两块输入：分镜提示词 + 该分镜已确认宫格的逐格文本（D-15）。

    只由 ``fields == {"video_prompt"}`` 的调用方传进来：写 image_prompt 时看不到这些块，
    模型就不会把逐格文本抄回分镜提示词，多出第二份真相源。

    ``grid.cells`` 是本次出图实际用的那一份（D-3），不重新解析 scene；K 的编号就是
    ``cells`` 的下标序，与视频请求下发的参考图同源（D-17）。
    """

    lines: list[str] = []
    opening = scene.strip()
    if opening:
        lines.append(f"分镜提示词：{_neutralize_tags(opening).replace(chr(10), chr(10) + '  ')}")
    if grid is not None and grid.cells:
        lines.append(
            f"关键帧（{grid.rows}x{grid.cols}，按 K0→K{len(grid.cells) - 1} 顺序，与视频请求下发的参考图同序）："
        )
        for cell in grid.cells:
            size = f"（{cell.continuity_notes}）" if cell.continuity_notes else ""
            lines.append(f"  K{cell.index}{size}：{_neutralize_tags(cell.prompt).replace(chr(10), ' ')}")
    return lines


def render_drama_content_for_prompt_authoring(
    content_scenes: list, *, video_inputs: Mapping[str, ShotGrid] | None = None
) -> str:
    """把 script_plan 已定稿的分镜内容渲染为 prompt_authoring 视觉生成的输入块（每分镜一段）。

    口播 / 原文锚仅供 LLM 理解戏剧节奏——「不要复制进视觉字段」由 ``build_drama_prompt`` 在
    ``<shots>`` 块前一次性声明，分镜条目内不逐条重复；它们由后端按 scene_id 透传
    （见 ``ScriptGenerator._merge_visual_layer``），prompt_authoring 只产出 image_prompt / video_prompt。

    渲染结果嵌入 prompt_authoring prompt 的 ``<shots>`` 块：资产名 / utterances 字段先判 ``isinstance(_, list)``——
    降级 / 手改 script_plan 可能写成非列表值（字符串会被逐字符迭代、数字会抛 TypeError），非列表按空处理；
    列表内再按 ``isinstance(_, str)`` 过滤非字符串脏数据。所有动态文本过 ``_neutralize_tags`` 中和尖括号——
    逐字 source_text / utterances / scene_description 含 ``<...>`` 时不致打散标签块结构（与 narration 的
    ``_format_narration_script_plan_segments`` 同口径）。本函数 fail-soft：结构性 fail-loud 在上游 _load_drama_script_plan_content。
    """
    if not content_scenes:
        return "（无分镜内容）"
    blocks: list[str] = []
    for scene in content_scenes:
        if not isinstance(scene, dict):
            continue
        sid = _neutralize_tags(str(scene.get("scene_id") or "?"))
        duration = scene.get("duration_seconds")
        header = f"### {sid}" + (f"（时长 {duration} 秒）" if duration else "")
        lines = [header]
        raw_chars = scene.get("characters_in_scene")
        raw_scenes_ref = scene.get("scenes")
        raw_props_ref = scene.get("props")
        chars = [c for c in raw_chars if isinstance(c, str)] if isinstance(raw_chars, list) else []
        scenes_ref = [s for s in raw_scenes_ref if isinstance(s, str)] if isinstance(raw_scenes_ref, list) else []
        props_ref = [p for p in raw_props_ref if isinstance(p, str)] if isinstance(raw_props_ref, list) else []
        lines.append(
            f"出场资产：角色 [{_neutralize_tags(', '.join(chars) or '无')}]、"
            f"场景 [{_neutralize_tags(', '.join(scenes_ref) or '无')}]、道具 [{_neutralize_tags(', '.join(props_ref) or '无')}]"
        )
        # 视觉改编描述由内容确认转换透传进正式脚本；存量正式脚本可能不带它，缺席时不渲染这一行。
        raw_scene_desc = scene.get("scene_description")
        if raw_scene_desc:
            scene_desc = _neutralize_tags(str(raw_scene_desc)).replace("\n", "\n  ")
            lines.append(f"视觉改编：{scene_desc}")
        raw_utterances = scene.get("utterances")
        utterances = raw_utterances if isinstance(raw_utterances, list) else []
        if utterances:
            lines.append("口播：")
            for u in utterances:
                if not isinstance(u, dict):
                    continue
                text = _neutralize_tags(str(u.get("text") or "")).replace("\n", "\n    ")
                if u.get("kind") == "dialogue":
                    speaker = _neutralize_tags(str(u.get("speaker") or ""))
                    lines.append(f"  - [台词] {speaker}：{text}")
                else:
                    lines.append(f"  - [画外音] {text}")
        source_text = scene.get("source_text")
        if source_text:
            source_block = _neutralize_tags(str(source_text)).replace("\n", "\n  ")
            lines.append(f"原文锚：{source_block}")
        if video_inputs is not None:
            # 视频提示词编写才需要：分镜提示词与已确认宫格的逐格文本（D-15）。
            lines.extend(render_video_prompt_inputs(shot_scene_text(scene), video_inputs.get(sid)))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _asset_aliases(bucket: dict | None, name: str) -> list[str]:
    """资产表条目的别名；衍生引用名不是表里的键，取不到别名。"""
    entry = (bucket or {}).get(name)
    aliases = entry.get(ALIASES_FIELD) if isinstance(entry, dict) else None
    if not isinstance(aliases, list):
        return []
    return [_neutralize_tags(alias) for alias in aliases if isinstance(alias, str) and alias.strip()]


def _project_asset_appearances(characters: dict | None, scenes: dict | None, props: dict | None) -> dict:
    """资产外观、别名与引用名的键齐全投影；动态尖括号中和后作为模版数据。"""
    return {
        key: [
            {
                "name": _neutralize_tags(name),
                "aliases": _asset_aliases(bucket, name),
                "appearance": _neutralize_tags(appearance) or None,
            }
            for name, appearance in iter_asset_appearances(asset_type, bucket)
        ]
        for key, asset_type, bucket in (
            ("characters", "character", characters),
            ("scenes", "scene", scenes),
            ("props", "prop", props),
        )
    }


def build_drama_prompt(
    style: str,
    style_description: str,
    scenes_content: str,
    episode: int,
    aspect_ratio: str = "16:9",
    target_language: str = "中文",
    characters: dict | None = None,
    scenes: dict | None = None,
    props: dict | None = None,
    instructions: str | None = None,
) -> str:
    """构建剧情演绎 prompt_authoring（视觉层）prompt。

    内容抽取前移到 script_plan（见 ADR 0041）：分镜边界、出场资产、逐字口播 utterances、原文锚
    source_text、视觉改编描述均已在 script_plan 定稿，``scenes_content`` 是其渲染输入
    （``render_drama_content_for_prompt_authoring``）。prompt_authoring 仅产出本次 response_schema 要求的视觉层字段，
    LLM 输出按 scene_id 与 script_plan 内容对齐、由后端合并；不再按 source_kind 分支、不再识别口播、
    不再标注资产或时长——这些都是 script_plan 的职责。

    ``characters`` / ``scenes`` / ``props`` 注入出场资产的外观描述（project.json 各 bucket），
    供视觉字段写服装 / 材质 / 陈设细节时取材；三者都为 None 时不渲染资产块。
    """
    return builtin_templates.render(
        "text/drama_prompt_authoring",
        style=style,
        style_description=style_description,
        aspect_ratio=aspect_ratio,
        aspect_ratio_label={"9:16": "竖屏构图", "16:9": "横屏构图"}.get(aspect_ratio, f"{aspect_ratio} 构图"),
        assets=(
            _project_asset_appearances(characters, scenes, props)
            if characters is not None or scenes is not None or props is not None
            else None
        ),
        scenes_content=scenes_content,
        episode=episode,
        target_language=target_language,
        instructions=instructions,
    )
