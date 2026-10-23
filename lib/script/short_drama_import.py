"""解析短剧工厂源稿，并按 AI 规划的节拍分组构建视觉制作状态。

动作、台词与原文锚始终来自 Markdown；分组、时长、画面描述和分镜提示词由 AI 提供。
本模块不生产机械分镜。视频提示词保持为空，待参考素材确认后独立编写。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

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
    location: str = ""
    scene_appearance: str = ""
    characters: list[str] = field(default_factory=list)
    character_appearances: dict[str, str] = field(default_factory=dict)
    props: list[str] = field(default_factory=list)
    prop_appearances: dict[str, str] = field(default_factory=dict)
    beats: list[_Beat] = field(default_factory=list)


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


def parse_short_drama_episode(markdown: str) -> ParsedEpisode:
    """把一份单集 Markdown 解析为 :class:`ParsedEpisode`。"""
    parsed = ParsedEpisode(episode_number=0, title="")
    current = _Block()
    parsed.blocks.append(current)

    for raw in markdown.splitlines():
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
            current = _Block(location=location, scene_appearance=appearance)
            parsed.blocks.append(current)
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
) -> dict[str, Any]:
    """一幕的剧本载荷。``characters`` 缺省取所属块的人物；跨块合并的幕由调用方给并集。

    ``segment_break`` 恒为 False：末幕之后没有下一幕，把它标成场景切换点只是个哨兵，
    却会让整集被切成「13 + 1」两组——尾组只有一格要多出一张联合图，且断章那一格因此
    拿不到上一幕的首帧参考（见 ``storyboard_sequence.previous_storyboard_image``）。
    需要硬切的场景由创作者在界面上显式标记。
    """

    source_text = "\n".join(beat.raw for beat in sub_beats)
    prop_names = props if props is not None else block.props
    scene_description = _build_scene_description(sub_beats)
    return {
        "scene_id": f"E{episode}S{scene_index + 1}",
        "duration_seconds": duration,
        "segment_break": False,
        "characters_in_scene": sorted(set(characters if characters is not None else block.characters)),
        "scenes": [block.location] if block.location else [],
        "props": sorted({name for name in prop_names if name and name in source_text}),
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
) -> dict[str, Any]:
    """由模型规划的 1-based beat id 分组构建剧本，内容字段始终从 Markdown 原稿派生。"""
    flat = [(block, beat) for block in parsed.blocks for beat in block.beats]
    if len(groups) != len(durations) or not groups:
        raise ValueError("scene groups and durations must be non-empty and aligned")
    if len(scene_descriptions) != len(groups):
        raise ValueError("scene descriptions must align with scene groups")
    if len(image_prompts) != len(groups) or any(not prompt.strip() for prompt in image_prompts):
        raise ValueError("image prompts must align with scene groups")
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
        characters = sorted({name for block, _ in entries for name in block.characters})
        props = sorted({name for block, _ in entries for name in block.props})
        scene = _build_scene(episode, index, first_block, beats, duration, image_prompts[index], characters, props)
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
        if block.location and block.scene_appearance:
            scenes[block.location] = block.scene_appearance
        for name, appearance in block.prop_appearances.items():
            if appearance and name not in props:
                props[name] = appearance
    return {"character": characters, "scene": scenes, "prop": props}
