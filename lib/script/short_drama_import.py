"""解析短剧工厂源稿，并按 AI 规划的节拍分组构建视觉制作状态。

动作、台词与原文锚始终来自 Markdown；分组、时长、画面描述和分镜提示词由 AI 提供。
本模块不生产机械分镜。视频提示词保持为空，待参考素材确认后独立编写。
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from lib.project.asset_types import asset_name_comparison_key, resolve_asset_key
from lib.script.shot_prompt_standard import reference_tokens

# ---------------------------------------------------------------------------
# 解析：Markdown → 中间结构
# ---------------------------------------------------------------------------


@dataclass
class _Beat:
    """一个内容节拍：动作行或一句台词。"""

    kind: str  # "action" | "dialogue"
    text: str  # action: 动作描述；dialogue: 台词正文
    speaker: str | None = None  # 仅 dialogue
    stage: str | None = None  # dialogue 的神态/动作修饰
    raw: str = ""  # 原始行，用于 source_text 追溯


@dataclass
class _Block:
    #: 一个解析块里的场景清单。工厂源稿常见「先列全部 [场景] 再写 beat」：
    #: 连续出现的 [场景] 行属于同一块的场景清单，而不是各建一个空块。
    locations: list[str] = field(default_factory=list)
    scene_appearances: dict[str, str] = field(default_factory=dict)
    characters: list[str] = field(default_factory=list)
    character_appearances: dict[str, str] = field(default_factory=dict)
    props: list[str] = field(default_factory=list)
    prop_appearances: dict[str, str] = field(default_factory=dict)
    beats: list[_Beat] = field(default_factory=list)

    @property
    def location(self) -> str:
        """首场景（兼容只认单场景的旧调用方）。"""
        return self.locations[0] if self.locations else ""

    @property
    def scene_appearance(self) -> str:
        return self.scene_appearances.get(self.location, "")


@dataclass
class ParsedEpisode:
    episode_number: int
    title: str
    stage: str = ""
    opening_hook: str = ""
    emotion_flow: str = ""
    total_duration: int = 0
    structure: str = ""
    mid_hook: str = ""
    cliffhanger: str = ""
    blocks: list[_Block] = field(default_factory=list)


_HEADER_PATTERNS: dict[str, re.Pattern[str]] = {
    "stage": re.compile(r"^【所属阶段】[：:]\s*(.+)$"),
    "opening_hook": re.compile(r"^【黄金前3秒钩子】[：:]\s*(.+)$"),
    "emotion_flow": re.compile(r"^【情绪流变】[：:]\s*(.+)$"),
    "total_duration": re.compile(r"^【本集时长】[：:]\s*(\d+)\s*秒"),
    "structure": re.compile(r"^【本集结构】[：:]\s*(.+)$"),
    "mid_hook": re.compile(r"^【本集中段小钩】[：:]\s*(.+)$"),
    "cliffhanger": re.compile(r"^【本集断章卡点】[：:]\s*(.+)$"),
}
# 同时接受短剧工厂原生标题和 Web 导入弹窗的兼容格式。
_EPISODE_RE = re.compile(r"^(?:#\s*)?第\s*[【\[]?\s*(\d+)\s*[】\]]?\s*集(?:\s*[：:]?\s*(.*))?$")
_SCENE_RE = re.compile(r"^\[场景\][：:]\s*(.+)$")
_PERSON_RE = re.compile(r"^\[人物\][：:]\s*(.+)$")
_PROP_RE = re.compile(r"^\[道具\][：:]\s*(.+)$")
_ACTION_PREFIX = "△"
_DIALOGUE_RE = re.compile(r"^(?P<speaker>.+?)(?:（(?P<stage>[^）]+)）)?[：:]\s*(?P<text>.+)$")
#: 工厂模板的两行台词：说话人行只有「角色（神态）：」，台词在下一行引号里。
_SPEAKER_ONLY_RE = re.compile(r"^(?P<speaker>.+?)(?:（(?P<stage>[^）]+)）)?[：:]\s*$")
_QUOTED_LINE_RE = re.compile(r"^[\"“「]")
_NARRATION_HINTS = ("旁白", "画外音", "解说", "narrator", "vo")

#: 剧情驱动的切分窗口（秒）：单幕时长落在 [下限, 上限] 内。
#:
#: 导入阶段不读视频供应商能力（用户此时未必已选定生成模型），因此**不做档位吸附**——
#: 单幕时长由内容节奏决定，档位对齐交给生成期的预检。窗口取 12~15 秒：短剧单幕要装下
#: 一个完整的往返回合，短于 12 秒会碎成流水账，长于 15 秒单镜头信息量过载。
STORY_SCENE_WINDOW_SECONDS: tuple[int, int] = (12, 15)


def _split_outside_parens(text: str) -> tuple[str, str, str]:
    """按**括号外**的第一个 ``｜``（半角 ``|`` 亦可）切成 ``(前, 分隔符, 后)``。

    外观描述里常有竖线，只有括号外的那个才是「名字 | 外观」的分隔。
    """

    depth = 0
    for index, ch in enumerate(text):
        if ch in "（(":
            depth += 1
        elif ch in "）)":
            depth = max(depth - 1, 0)
        elif ch in "｜|" and depth == 0:
            return text[:index], ch, text[index + 1 :]
    return text, "", ""


def _parse_character_entry(token: str) -> tuple[str, str]:
    """解析单个 ``名｜外观`` 或 ``名（状态标签｜外观）`` 片段，返回 ``(名字, 外观)``。

    两种写法都成立（工厂源稿两种都在用）：``陈峰｜宽脸浓眉`` 的竖线在括号外，
    ``沈昭（冷醒·重生者｜藕荷色西装套裙）`` 的竖线在括号内。名字恒在最前、括号之前。
    """

    token = token.strip()
    head, separator, tail = _split_outside_parens(token)
    name = re.split(r"[（(]", head, maxsplit=1)[0].strip()
    if not name:
        return (name, "")
    if separator:
        return (name, tail.strip())
    appearance = ""
    match = re.search(r"[（(]([^）)]*)[）)]", head)
    if match:
        inner = match.group(1).strip()
        for sep in ("｜", "|"):
            if sep in inner:
                appearance = inner.split(sep, 1)[1].strip()
                break
    return (name, appearance)


def _parse_prop_entry(line: str) -> tuple[str, str]:
    """解析一行 ``名称｜外观`` 道具声明；有意不按描述中的顿号拆分。"""
    name, separator, appearance = line.partition("｜")
    if not separator:
        name, separator, appearance = line.partition("|")
    return name.strip(), appearance.strip() if separator else ""


def _split_character_entries(line: str) -> list[tuple[str, str]]:
    """``林辰（隐忍龙王｜鹅蛋脸、杏眼、青衣）、陈峰（嚣张富二代）`` →
    ``[("林辰", "鹅蛋脸、杏眼、青衣"), ("陈峰", "")]``；``陈峰｜宽脸、浓眉`` 同样给
    ``("陈峰", "宽脸、浓眉")``。

    括号内用 ``｜``（半角 ``|`` 亦可）分隔「状态标签」与可选的「外观描述」；
    无 ``｜`` 时整段视作状态标签，外观留空。名字恒在括号之前。

    按括号深度切分，避免把括号内的顿号（外观描写里常见）误判为角色分隔符；括号外的
    ``｜`` 之后整段是这条的外观，同样不再按顿号切。
    """
    entries: list[tuple[str, str]] = []
    depth = 0
    current = ""
    for index, ch in enumerate(line):
        if ch in "（(":
            depth += 1
            current += ch
        elif ch in "）)":
            depth -= 1
            current += ch
        elif ch in "、,，" and depth == 0:
            if current.strip():
                entries.append(_parse_character_entry(current))
            current = ""
        elif ch in "｜|" and depth == 0:
            # 括号外的竖线起，剩下的全是这一条的外观描述（含其中的顿号）。
            current += line[index:]
            break
        else:
            current += ch
    if current.strip():
        entries.append(_parse_character_entry(current))
    return entries


def _join_two_line_dialogue(markdown: str) -> list[str]:
    """工厂模板的台词写成「角色（神态）：」换行「"台词。"」，解析前并成一行。

    只在这一行确实是说话人行、下一非空行以引号开头时合并，避免把普通「……：」行误当
    台词；``△`` 动作行与 ``[`` / ``【`` 结构行不参与。
    """

    lines = markdown.splitlines()
    merged: list[str] = []
    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if stripped and not stripped.startswith(("[", "【", _ACTION_PREFIX)) and _SPEAKER_ONLY_RE.match(stripped):
            lookahead = index + 1
            while lookahead < len(lines) and not lines[lookahead].strip():
                lookahead += 1
            if lookahead < len(lines) and _QUOTED_LINE_RE.match(lines[lookahead].strip()):
                merged.append(stripped + lines[lookahead].strip())
                index = lookahead + 1
                continue
        merged.append(raw)
        index += 1
    return merged


def parse_short_drama_episode(markdown: str) -> ParsedEpisode:
    """把一份单集 Markdown 解析为 :class:`ParsedEpisode`。"""
    parsed = ParsedEpisode(episode_number=0, title="")
    current = _Block()
    parsed.blocks.append(current)

    for raw in _join_two_line_dialogue(markdown):
        line = raw.strip()
        if not line:
            continue

        episode_match = _EPISODE_RE.match(line)
        if episode_match:
            parsed.episode_number = int(episode_match.group(1))
            parsed.title = episode_match.group(2).strip().strip("【】").strip()
            continue

        header_key: str | None = None
        for key, pattern in _HEADER_PATTERNS.items():
            header_match = pattern.match(line)
            if header_match:
                header_key = key
                if key == "total_duration":
                    parsed.total_duration = int(header_match.group(1))
                else:
                    setattr(parsed, key, header_match.group(1).strip())
                break
        if header_key is not None:
            continue

        scene_match = _SCENE_RE.match(line)
        if scene_match:
            location = scene_match.group(1).strip()
            appearance = ""
            for sep in ("｜", "|"):
                if sep in location:
                    location, appearance = location.split(sep, 1)
                    location = location.strip()
                    appearance = appearance.strip()
                    break
            # 连续 [场景] 行是全集场景清单：当前块还没有 beat / 人物 / 道具时并入同一块。
            # 一旦块内已有 beat，就仍是「新场景块」，保持原有一块多场景的解析能力。
            if current.beats or current.characters or current.props:
                current = _Block()
                parsed.blocks.append(current)
            if location:
                current.locations.append(location)
                if appearance:
                    current.scene_appearances.setdefault(location, appearance)
            continue

        person_match = _PERSON_RE.match(line)
        if person_match:
            for name, appearance in _split_character_entries(person_match.group(1)):
                if not name:
                    continue
                current.characters.append(name)
                if appearance:
                    current.character_appearances.setdefault(name, appearance)
            continue

        prop_match = _PROP_RE.match(line)
        if prop_match:
            name, appearance = _parse_prop_entry(prop_match.group(1))
            if name:
                current.props.append(name)
                if appearance:
                    current.prop_appearances.setdefault(name, appearance)
            continue

        if line.startswith(_ACTION_PREFIX):
            current.beats.append(_Beat(kind="action", text=line[1:].strip(), raw=raw))
            continue

        if not line.startswith(("[", "【")):
            dialogue_match = _DIALOGUE_RE.match(line)
            if dialogue_match:
                speaker = dialogue_match.group("speaker").strip()
                stage = (dialogue_match.group("stage") or "").strip() or None
                text = dialogue_match.group("text").strip().strip("\"'“”")
                current.beats.append(_Beat(kind="dialogue", text=text, speaker=speaker, stage=stage, raw=raw))
                continue
            # 非结构化的其它行（动作续写/环境描写）并入当前块的动作
            current.beats.append(_Beat(kind="action", text=line, raw=raw))

    return parsed


def _declared_names(parsed: ParsedEpisode) -> dict[str, list[str]]:
    """源稿里声明的场景 / 人物 / 道具名，按类型去重保序。"""
    names: dict[str, list[str]] = {"scene": [], "character": [], "prop": []}
    for block in parsed.blocks:
        for key, values in (
            ("scene", block.locations),
            ("character", block.characters),
            ("prop", block.props),
        ):
            for name in values:
                if name and name not in names[key]:
                    names[key].append(name)
    return names


def _description_lookup(descriptions: Mapping[str, str] | None) -> dict[str, str]:
    """按资产比对坐标系建描述索引；空描述不参与。"""
    lookup: dict[str, str] = {}
    for name, text in (descriptions or {}).items():
        cleaned = str(text).strip()
        if cleaned:
            lookup[asset_name_comparison_key(str(name))] = cleaned
    return lookup


def scene_place_name(name: str) -> str:
    """场景登记名里的地点部分：``内景・冷宫・夜`` → ``冷宫``；不含分隔符时原样返回。"""
    parts = [part for part in re.split(r"[・·]", str(name)) if part.strip()]
    return parts[1].strip() if len(parts) >= 2 else str(name).strip()


def fragment_scene_names(
    registered_scenes: Collection[str],
    markdown: str,
    *,
    parsed: ParsedEpisode | None = None,
) -> list[str]:
    """本集出现、但未用 ``[场景]`` 声明的地点（碎片空间）。

    工厂源稿的 ``[场景]`` 行只声明「完整场景」且受 ≤2 的机检约束；回忆 / 钩子里的地点
    （冷宫这类）不该占名额，却仍然要上画面。判定不靠白名单：凡已登记的场景，其地点名
    出现在本集**叙事部分**（钩子、情绪流变、动作与台词）、又没有写进 ``[场景]``，就按碎片
    空间处理——换一部剧、换一个地点同样成立。

    只扫叙事部分、不扫 ``[人物]`` / ``[道具]`` 声明行，避免「沈府老嬷嬷」这类描述里的
    「沈府」把同名场景误判成碎片；并先抹掉已声明场景的整名，避免更大的地点名靠子串命中。
    """
    episode = parsed if parsed is not None else parse_short_drama_episode(markdown)
    declared = {name for block in episode.blocks for name in block.locations}
    narrative = "\n".join(line for line in markdown.splitlines() if not line.strip().startswith(("[人物]", "[道具]")))
    for name in sorted(declared, key=len, reverse=True):
        narrative = narrative.replace(name, " ")
    found: list[str] = []
    for name in registered_scenes:
        if not isinstance(name, str) or not name.strip() or name in declared:
            continue
        place = scene_place_name(name)
        if place and place in narrative:
            found.append(name)
    return sorted(found)


def build_episode_brief(
    parsed: ParsedEpisode,
    *,
    character_descriptions: Mapping[str, str] | None = None,
    scene_descriptions: Mapping[str, str] | None = None,
    prop_descriptions: Mapping[str, str] | None = None,
    fragment_scenes: Sequence[str] = (),
) -> str:
    """本集事实卡：钩子、情绪流变、场景 / 人物 / 道具的已登记外形与来路。

    规划模型只拿到 beats 与资产名时，会按字面自行补主语、补空间、补道具形状；把已登记的
    ``description`` 一并给它，画面才有事实可依（例如白绫的同心结在末端、旧红绳戴在腕上）。
    卡片只写事实、不写镜头：描述优先取项目资产登记，缺失时回落到源稿标记行里写明的外观。

    ``fragment_scenes`` 是本集出现、却未用 ``[场景]`` 声明的地点（回忆 / 钩子里的旧地）。
    它们仍然上画面，但只能作碎片——这一节把「可以出现、不能当主空间」讲清楚，避免模型把
    发生在那里的动作搬进本幕。
    """
    declared = _declared_names(parsed)
    from_source = referenced_asset_descriptions(parsed)
    head = [f"第{parsed.episode_number}集" + (f"《{parsed.title}》" if parsed.title else "")]
    if parsed.total_duration > 0:
        head.append(f"本集时长 {parsed.total_duration} 秒")
    if parsed.stage:
        head.append(parsed.stage)
    lines: list[str] = ["【本集】" + "｜".join(head)]
    if parsed.opening_hook:
        lines.append(f"【黄金钩子】{parsed.opening_hook}")
    if parsed.emotion_flow:
        lines.append(f"【情绪流变】{parsed.emotion_flow}")
    cards = (
        ("完整场景", declared["scene"], scene_descriptions, from_source["scene"]),
        ("人物", declared["character"], character_descriptions, from_source["character"]),
        ("道具", declared["prop"], prop_descriptions, from_source["prop"]),
    )
    for label, names, provided, fallback in cards:
        if not names:
            continue
        lookup = _description_lookup(provided)
        lines.append(f"【{label}卡】")
        for name in names:
            text = lookup.get(asset_name_comparison_key(name)) or fallback.get(name, "")
            lines.append(f"- {name}：{text}" if text else f"- {name}")
    if fragment_scenes:
        lookup = _description_lookup(scene_descriptions)
        lines.append("【碎片空间】以下已登记地点在本集出现，但未用 [场景] 声明：")
        for name in fragment_scenes:
            text = lookup.get(asset_name_comparison_key(name)) or from_source["scene"].get(name, "")
            lines.append(f"- {name}：{text}" if text else f"- {name}")
        lines.append("碎片空间只许以光、声、叠影出现；不得当作本幕主空间，也不得把发生在那里的动作搬进本幕空间。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 转换：中间结构 → DramaEpisodeScript 形状
# ---------------------------------------------------------------------------


def _build_utterances(beats: list[_Beat]) -> list[dict[str, Any]]:
    utterances: list[dict[str, Any]] = []
    for beat in beats:
        if beat.kind != "dialogue":
            continue
        speaker = beat.speaker
        if speaker is not None and any(hint in speaker for hint in _NARRATION_HINTS):
            utterances.append({"kind": "voiceover", "speaker": None, "text": beat.text})
        else:
            utterances.append({"kind": "dialogue", "speaker": speaker, "text": beat.text})
    return utterances


def _build_scene_description(beats: list[_Beat]) -> str:
    parts: list[str] = []
    for beat in beats:
        if beat.kind == "action":
            parts.append(beat.text)
        else:
            # 台词的神态作为舞台提示并入视觉描述，台词正文已进 utterances，不重复。
            label = beat.speaker or ""
            if beat.stage:
                label = f"{label}（{beat.stage}）"
            if label:
                parts.append(label)
    return "\n".join(part for part in parts if part)


def _build_scene(
    episode: int,
    scene_index: int,
    block: _Block,
    sub_beats: list[_Beat],
    duration: int,
    image_prompt: str,
    characters: list[str] | None = None,
    props: list[str] | None = None,
    scene_assets: SceneAssets | None = None,
) -> dict[str, Any]:
    """一幕的剧本载荷。给了 ``scene_assets`` 就用按幕解析结果，否则回落到块声明。

    ``segment_break`` 恒为 False：末幕之后没有下一幕，把它标成场景切换点只是个哨兵，
    却会让整集被切成「13 + 1」两组——尾组只有一格要多出一张联合图，且断章那一格因此
    拿不到上一幕的首帧参考（见 ``storyboard_sequence.previous_storyboard_image``）。
    需要硬切的场景由创作者在界面上显式标记。
    """

    source_text = "\n".join(beat.raw for beat in sub_beats)
    if scene_assets is not None:
        locations = list(scene_assets.scenes)
        character_names = list(scene_assets.characters_in_scene)
        prop_names = list(scene_assets.props)
    else:
        locations = [block.location] if block.location else []
        character_names = sorted(set(characters if characters is not None else block.characters))
        prop_names = sorted({name for name in (props if props is not None else block.props) if name in source_text})
    scene_description = _build_scene_description(sub_beats)
    return {
        "scene_id": f"E{episode}S{scene_index + 1}",
        "duration_seconds": duration,
        "segment_break": False,
        "characters_in_scene": character_names,
        "scenes": locations,
        "props": prop_names,
        "image_prompt": image_prompt,
        "video_prompt": None,
        "utterances": _build_utterances(sub_beats),
        "source_text": source_text,
        "scene_description": scene_description,
        "generated_assets": {},
        "pending_authoring": True,
        "note": None,
    }


def beat_catalog(parsed: ParsedEpisode) -> list[dict[str, Any]]:
    """返回稳定编号的原稿节拍，供模型只规划分组、不改写内容。"""
    catalog: list[dict[str, Any]] = []
    for block_index, block in enumerate(parsed.blocks):
        for beat in block.beats:
            catalog.append(
                {
                    "beat_id": len(catalog) + 1,
                    "block_id": block_index,
                    "kind": beat.kind,
                    "speaker": beat.speaker,
                    "text": beat.text,
                    "stage": beat.stage,
                }
            )
    return catalog


@dataclass(frozen=True, slots=True)
class SceneAssets:
    """一幕实际出场 / 使用的资产清单（参考图与 token 闸共用同一份真相源）。"""

    scenes: tuple[str, ...]
    characters_in_scene: tuple[str, ...]
    props: tuple[str, ...]

    def allowed_names(self) -> set[str]:
        """本幕提示词允许出现的 token 名字（场景 + 角色 + 道具）。"""
        return {*self.scenes, *self.characters_in_scene, *self.props}


def _registered_set(registered: Mapping[str, Collection[str]], asset_type: str) -> set[str]:
    return {str(name) for name in registered.get(asset_type, ()) if str(name).strip()}


def _beat_visual_text(beat: _Beat) -> str:
    """一个 beat 里用于资产 mention 判定的文本：动作正文 + 台词舞台提示。"""
    return " ".join(part for part in (beat.text, beat.stage or "") if part)


def _scene_from_tokens(image_prompt: str, registered_scenes: Collection[str]) -> list[str]:
    """从提示词的 ``@[登记名]`` 里取本幕场景；只认登记场景，按出现序去重。"""
    registered = {asset_name_comparison_key(name): name for name in registered_scenes}
    found: list[str] = []
    for token in reference_tokens(image_prompt):
        key = asset_name_comparison_key(token)
        if key in registered and registered[key] not in found:
            found.append(registered[key])
    return found


def resolve_scene_assets(
    parsed: ParsedEpisode,
    groups: Sequence[Sequence[int]],
    image_prompts: Sequence[str],
    *,
    registered: Mapping[str, Collection[str]],
    plan_scenes: Sequence[Mapping[str, Any]] | None = None,
) -> list[SceneAssets]:
    """按幕解析 scenes / characters_in_scene / props，转换与质量闸共用这一份结果。

    口径（D-2 / D-10）：

    - ``scenes``：优先规划显式给的场景（``plan_scenes[i]["scenes"]``，必须为登记名），
      否则取本幕 ``image_prompt`` 里的登记场景 token；都没有时，仅当本幕所有 beat 所属块
      的场景候选唯一才用该场景，多候选即报可回喂的歧义原因；
    - ``characters_in_scene``：本幕 action beat 文本里出现的登记角色 + 台词 speaker；
    - ``props``：本幕全部 beat 文本里出现的登记道具；
    - 只在块/``[人物]``/``[道具]`` 声明、却没在本幕 beat 出场的资产不会进入本幕清单。

    这解释了「两个 [场景] 连写」的全集清单格式：场景由每幕提示词 token 指认，而不是
    沿用最后一个场景块。
    """

    if len(groups) != len(image_prompts) or not groups:
        raise ValueError("scene groups and image prompts must be non-empty and aligned")
    flat = [(block, beat) for block in parsed.blocks for beat in block.beats]
    registered_chars = _registered_set(registered, "character")
    registered_scenes = _registered_set(registered, "scene")
    registered_props = _registered_set(registered, "prop")
    scene_candidates = {name for block in parsed.blocks for name in block.locations if name}
    resolved: list[SceneAssets] = []
    for index, (group, image_prompt) in enumerate(zip(groups, image_prompts, strict=True)):
        entries = []
        for value in group:
            if isinstance(value, bool) or value < 1 or value > len(flat):
                raise ValueError(f"scene {index + 1} 含非法 beat id: {value}")
            entries.append(flat[value - 1])
        beats = [beat for _block, beat in entries]
        plan = plan_scenes[index] if plan_scenes is not None and index < len(plan_scenes) else {}
        planned_scenes = plan.get("scenes") if isinstance(plan, Mapping) else None
        scene_by_key = {asset_name_comparison_key(name): name for name in registered_scenes}
        planned: list[str] = []
        if isinstance(planned_scenes, Sequence) and not isinstance(planned_scenes, str):
            for name in planned_scenes:
                text = str(name).strip()
                if not text:
                    continue
                registered_name = scene_by_key.get(asset_name_comparison_key(text))
                if registered_name is None:
                    raise ValueError(f"scene {index + 1} 声明了未登记场景「{text}」；先完成资产步骤或改用登记名")
                planned.append(registered_name)
        token_scenes = _scene_from_tokens(image_prompt, registered_scenes)
        if planned and token_scenes and set(planned) != set(token_scenes):
            raise ValueError(
                f"scene {index + 1} 的场景声明 {planned} 与提示词里的场景 token {token_scenes} 不一致；"
                "请让二者指认同一场景，或删掉不一致的一方"
            )
        scenes = planned or token_scenes
        if not scenes:
            block_locations = {location for block, _beat in entries for location in block.locations if location}
            if len(block_locations) == 1:
                scenes = [next(iter(block_locations))]
            elif len(scene_candidates) == 1:
                scenes = [next(iter(scene_candidates))]
            else:
                candidates = "、".join(sorted(block_locations or scene_candidates)) or "<空>"
                raise ValueError(
                    f"scene {index + 1} 无法判定本幕场景：提示词需用「场景名@[登记场景名]」指认本幕场景"
                    f"（候选：{candidates}）；不要在提示词里沿用最后一个 [场景] 块"
                )

        characters: set[str] = set()
        props: set[str] = set()
        prompt_text = image_prompt if isinstance(image_prompt, str) else ""
        for beat in beats:
            visual_text = _beat_visual_text(beat)
            if beat.kind == "action":
                characters.update(name for name in registered_chars if name in visual_text)
            if beat.kind == "dialogue" and beat.speaker:
                speaker_key = asset_name_comparison_key(beat.speaker)
                characters.update(name for name in registered_chars if asset_name_comparison_key(name) == speaker_key)
            props.update(name for name in registered_props if name in visual_text)
        # 提示词里点名的登记角色/道具同样算在场：beat 常缺主语（「白绫收紧贴住喉」）、
        # 也常只用代称（「朱漆盘落妆台，同心结朝上」）。不补的话参考图会漏掉它们，
        # 而分镜提示词一写 token 就会被判「清单外资产」。
        characters.update(name for name in registered_chars if name in prompt_text)
        props.update(name for name in registered_props if name in prompt_text)
        resolved.append(
            SceneAssets(
                scenes=tuple(dict.fromkeys(scenes)),
                characters_in_scene=tuple(sorted(characters)),
                props=tuple(sorted(props)),
            )
        )
    return resolved


def _episode_document(parsed: ParsedEpisode, episode: int, scenes: list[dict[str, Any]]) -> dict[str, Any]:
    """将源稿元信息与已经规划好的分镜组装为视觉制作状态。"""
    return {
        "episode": episode,
        "title": parsed.title or f"第{episode}集",
        "content_mode": "drama",
        "hook": parsed.cliffhanger or None,
        "scenes": scenes,
        "short_drama_meta": {
            "stage": parsed.stage,
            "opening_hook": parsed.opening_hook,
            "emotion_flow": parsed.emotion_flow,
            "structure": parsed.structure,
            "mid_hook": parsed.mid_hook,
            "duration_seconds": parsed.total_duration,
        },
    }


def convert_planned_scenes(
    parsed: ParsedEpisode,
    episode: int,
    groups: list[list[int]],
    durations: list[int],
    scene_descriptions: list[str],
    image_prompts: list[str],
    *,
    scene_assets: Sequence[SceneAssets] | None = None,
) -> dict[str, Any]:
    """由模型规划的 1-based beat id 分组构建剧本，内容字段始终从 Markdown 原稿派生。

    ``scene_assets`` 由 :func:`resolve_scene_assets` 按幕解析而来；与质量闸传入同一份结果，
    保证「落盘字段 = 闸门允许的 token 清单」。省略时保留旧的块级并集口径，仅供直接调用兼容。
    """
    flat = [(block, beat) for block in parsed.blocks for beat in block.beats]
    if len(groups) != len(durations) or not groups:
        raise ValueError("scene groups and durations must be non-empty and aligned")
    if len(scene_descriptions) != len(groups):
        raise ValueError("scene descriptions must align with scene groups")
    if len(image_prompts) != len(groups) or any(not prompt.strip() for prompt in image_prompts):
        raise ValueError("image prompts must align with scene groups")
    if scene_assets is not None and len(scene_assets) != len(groups):
        raise ValueError("scene assets must align with scene groups")
    used: list[int] = []
    scenes: list[dict[str, Any]] = []
    for index, (group, duration) in enumerate(zip(groups, durations, strict=True)):
        if not group or any(isinstance(value, bool) or value < 1 or value > len(flat) for value in group):
            raise ValueError(f"invalid beat ids in scene {index + 1}")
        if group != list(range(group[0], group[-1] + 1)):
            raise ValueError(f"scene {index + 1} beat ids must be contiguous")
        if used and group[0] != used[-1] + 1:
            raise ValueError("scene groups must cover beats in source order without gaps")
        used.extend(group)
        entries = [flat[value - 1] for value in group]
        beats = [beat for _, beat in entries]
        first_block = entries[0][0]
        resolved = scene_assets[index] if scene_assets is not None else None
        scene = _build_scene(
            episode,
            index,
            first_block,
            beats,
            duration,
            image_prompts[index],
            scene_assets=resolved,
            characters=None
            if resolved is not None
            else sorted({name for block, _ in entries for name in block.characters}),
            props=None if resolved is not None else sorted({name for block, _ in entries for name in block.props}),
        )
        if resolved is None:
            # 直接调用的旧口径：跨块合并的幕保留块并集场景。
            scene["scenes"] = list(dict.fromkeys(block.location for block, _beat in entries if block.location))
        scene["scene_description"] = scene_descriptions[index]
        scenes.append(scene)
    if used != list(range(1, len(flat) + 1)):
        raise ValueError("scene groups must cover every source beat exactly once")
    return _episode_document(parsed, episode, scenes)


def scene_beat_groups(parsed: ParsedEpisode, script_scenes: list[Mapping[str, Any]]) -> list[list[int]]:
    """把现有正式脚本各幕的 ``source_text`` 映射回 1-based beat id 分组。

    正式脚本的 ``source_text`` 逐字来自同一份源稿的 beat 原行（见 :func:`_build_scene`），
    因此映射是确定性的：按序消费 beat 原行即可。映射不上说明现有分镜不是由这份源稿
    生成（源稿改过未重导、或被手工拼过），调用方应要求整体重新分镜而不是局部重写。
    """
    raws = [beat.raw.strip() for block in parsed.blocks for beat in block.beats]
    groups: list[list[int]] = []
    cursor = 0
    for index, scene in enumerate(script_scenes):
        lines = [line for line in str(scene.get("source_text") or "").splitlines() if line.strip()]
        if not lines:
            raise ValueError(f"第 {index + 1} 个分镜缺少内容锚（source_text），请整体重新分镜")
        group: list[int] = []
        for line in lines:
            if cursor >= len(raws) or raws[cursor] != line:
                raise ValueError("现有分镜与当前源稿不对应，请整体重新分镜")
            cursor += 1
            group.append(cursor)
        groups.append(group)
    if cursor != len(raws):
        raise ValueError("现有分镜未覆盖源稿全部内容，请整体重新分镜")
    return groups


def assets_missing_description(project: Mapping[str, Any], markdown: str) -> list[dict[str, str]]:
    """分镜前置检查：本集源稿引用的资产里，缺外观描述的清单。

    未登记或描述为空均阻断；源稿写明外观不能替代资产步骤的登记与描述规范化。AI 分镜入口与
    提示词编写入口（D-10 第 0 步）用同一判定，不提供第二个口径。
    """
    parsed = parse_short_drama_episode(markdown)
    mentioned = {
        "character": sorted({name for block in parsed.blocks for name in block.characters}),
        "scene": sorted({location for block in parsed.blocks for location in block.locations if location}),
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


def referenced_asset_names(script: Mapping[str, Any]) -> dict[str, list[str]]:
    """转换结果里引用到的角色 / 场景 / 道具名，按类型去重保序。

    短剧导入不走「内容确认」，原生流程在那里登记的新增资产这路上没人做，资产表会一直是空的，
    后面资产图与生成入口会因"引用了未登记的名字"拒绝。导入侧据此把引用到的名字补登记。
    """
    collected: dict[str, list[str]] = {"character": [], "scene": [], "prop": []}
    fields = (("character", "characters_in_scene"), ("scene", "scenes"), ("prop", "props"))
    for scene in script.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        for asset_type, reference_field in fields:
            names = scene.get(reference_field)
            if not isinstance(names, list):
                continue
            for name in names:
                if isinstance(name, str) and name and name not in collected[asset_type]:
                    collected[asset_type].append(name)
    return collected


def referenced_asset_descriptions(parsed: ParsedEpisode) -> dict[str, dict[str, str]]:
    """从解析结果抽取已写明的角色 / 场景外观描述（落在被导入的 Markdown 内）。

    与 :func:`referenced_asset_names` 互补：名字用于登记，描述用于填充登记条目的
    ``description``。工厂没写外观时对应项不出现在本字典里，调用方回落到空串。

    角色外观来自每集 ``[人物]`` 行括号内的 ``状态标签｜外观``；场景外观来自 ``[场景]``
    行 ``地点｜场景视觉描写``。两者都只在被导入的那份单集 Markdown 里才抽得到——
    工厂的人物圣经是独立工作文档，不在此范围内，要把外观带进导入就得写在这两份标记行里。
    """
    characters: dict[str, str] = {}
    scenes: dict[str, str] = {}
    props: dict[str, str] = {}
    for block in parsed.blocks:
        for name, appearance in block.character_appearances.items():
            if appearance and name not in characters:
                characters[name] = appearance
        for location in block.locations:
            appearance = block.scene_appearances.get(location, "")
            if appearance and location not in scenes:
                scenes[location] = appearance
        for name, appearance in block.prop_appearances.items():
            if appearance and name not in props:
                props[name] = appearance
    return {"character": characters, "scene": scenes, "prop": props}
