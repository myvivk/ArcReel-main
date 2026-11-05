"""Visual prompt authoring for factory storyboards."""

from collections.abc import Mapping

from lib.prompts.prompt_templates.builtin import builtin_templates

# ---------------------------------------------------------------------------
# 两段式分层文案（见 ADR 0041）：script_plan（normalize）= 内容、prompt_authoring（drama）= 视觉。
#
# 内容抽取前移到 script_plan：分镜边界、出场资产、逐字口播 utterances、原文锚 source_text、
# 视觉改编描述 scene_description 一次定稿，并按 source_kind 切「改编 / 提取」口径。
# prompt_authoring 只补唯一动态分镜提示词 image_prompt，按 scene_id 透传内容、不再识别口播、
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


def render_drama_content_for_prompt_authoring(
    content_scenes: list,
    previous_prompts: Mapping[str, str] | None = None,
) -> str:
    """把已定稿的分镜产物渲染为 prompt_authoring 视觉生成的输入块（每分镜一段）。

    分镜产物来自机械分镜：边界、时长、出场资产、口播、原文锚、视觉改编都已定稿，
    prompt_authoring 只产出 image_prompt。``previous_prompts`` 以 scene_id 为键给出
    「上一分镜的末镜提示词」——已编写过的相邻分镜才有，用于承接连续性；它只作上下文，
    不得照抄进本镜。

    口播 / 原文锚仅供 LLM 理解戏剧节奏——「不要复制进视觉字段」由 ``build_drama_prompt`` 在
    ``<shots>`` 块前一次性声明，分镜条目内不逐条重复；它们由后端按 scene_id 透传
    （见 ``ScriptGenerator._merge_visual_layer``），prompt_authoring 只产出 image_prompt。

    渲染结果嵌入 prompt_authoring prompt 的 ``<shots>`` 块：资产名 / utterances 字段先判 ``isinstance(_, list)``——
    降级 / 手改脚本可能写成非列表值（字符串会被逐字符迭代、数字会抛 TypeError），非列表按空处理；
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
        # 视觉改编描述由机械分镜确定性合成；存量正式脚本可能不带它，缺席时不渲染这一行。
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
        previous = (previous_prompts or {}).get(str(scene.get("scene_id") or ""))
        if previous:
            previous_text = _neutralize_tags(previous).replace("\n", " ")
            lines.append(f"上一分镜末镜（只读承接，不得照抄）：{previous_text}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_video_model_brief(
    spec: object,
    *,
    provider_id: str,
    model_id: str,
    durations: list[int] | None = None,
) -> str:
    """写作侧 prompt 的「目标视频模型」块：只给模型身份与时长硬事实。

    ``spec.writing_guide`` 是**视频组装阶段**的最终请求格式（H3 的 ``[Shot N]`` / ``At`` /
    ``<Picture N>`` / ``<d>`` 等），不能喂给写作模型，否则台词和包装会被提前写进
    ``image_prompt``，与 ``utterances`` 注入重复。
    """

    label = str(getattr(spec, "label", "") or model_id)
    lines = [f"本次提示词写给 {label}（{provider_id}/{model_id}），不是通用口径。"]
    if durations:
        lines.append(
            f"- 可用时长档位：{'、'.join(str(value) for value in durations)} 秒；按分镜 duration_seconds 拆段。"
        )
    lines.append(
        "- 写作阶段只写 action 变化与运镜；台词/画外音由 utterances 在视频组装阶段注入，"
        "正文不要写台词、(S1) 说话人编号、<d> 标签、<Picture N>、At/cut、overall_soundscape 或 Dialogue 段。"
    )
    lines.append(
        "- 正文引用资产写 @资产名（如 @沈昭），不要自己写「图1」；参考图模式下系统会把资产名渲染成「名称图N」。"
    )
    return "\n".join(lines)


def build_drama_prompt(
    style: str,
    style_description: str,
    scenes_content: str,
    episode: int,
    aspect_ratio: str = "16:9",
    target_language: str = "中文",
    instructions: str | None = None,
    model_facts: str | None = None,
) -> str:
    """构建剧情演绎 prompt_authoring（视觉层）prompt。

    内容抽取前移到 script_plan（见 ADR 0041）：分镜边界、出场资产、逐字口播 utterances、原文锚
    source_text、视觉改编描述均已在 script_plan 定稿，``scenes_content`` 是其渲染输入
    （``render_drama_content_for_prompt_authoring``）。prompt_authoring 仅产出本次 response_schema 要求的视觉层字段，
    LLM 输出按 scene_id 与 script_plan 内容对齐、由后端合并；不再按 source_kind 分支、不再识别口播、
    不再标注资产或时长——这些都是 script_plan 的职责。

    上下文不喂资产外观描述（D-10，2026-10-24 评审拍板）：出场资产在 ``scenes_content`` 里只有
    名字清单，外观一致性由资产参考图承载——喂了外观，模型就会把它整份抄进提示词正文。
    """
    return builtin_templates.render(
        "text/drama_prompt_authoring",
        style=style,
        style_description=style_description,
        aspect_ratio=aspect_ratio,
        aspect_ratio_label={"9:16": "竖屏构图", "16:9": "横屏构图"}.get(aspect_ratio, f"{aspect_ratio} 构图"),
        scenes_content=scenes_content,
        episode=episode,
        target_language=target_language,
        instructions=instructions,
        model_facts=model_facts,
    )
