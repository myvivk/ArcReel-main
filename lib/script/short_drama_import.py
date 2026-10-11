"""将 short-drama-factory 产出的 Markdown 剧本转换为 ArcReel 剧情演绎（drama）剧本 JSON。

本模块只负责"产物转换"（Markdown → :class:`lib.script.script_models.DramaEpisodeScript` 形状）。
short-drama-factory 的编剧方法论与模板**原样复刻**在
``agent_runtime_profile/.claude/skills/short-drama-factory/``，本模块不重述、不增删其任何规则。

转换约定（忠实保留、不增删原文内容）：

* 一集 Markdown = 一个文件；解析为一份 ``DramaEpisodeScript``，集号由调用方传入（ArcReel 剧本 JSON
  不存集号，落盘时由 ``episode`` 字段携带）。
* 每个 ``[场景]`` 块的动作行（``△ …``）与台词神态并入该幕 ``scene_description``；台词正文进
  ``utterances``（带说话人=dialogue，旁白/画外音=voiceover）。原文逐字保留在 ``source_text`` 追溯锚。
* 短剧单集 90~195 秒，超过 ArcReel 单幕 ``duration_seconds`` 上限 60 秒，故按 beat 数把一集切成若干幕，
  时长按总时长等比分配（每幕 ≤60）。这是格式对齐所需的必要拆分，不丢弃任何内容。
* 视觉层（``image_prompt`` / ``video_prompt``）留空并置 ``pending_authoring=True``，交给 ArcReel 既有的
  ``generate_episode_script``（提示词编写）补出——与 ArcReel 两段式（内容层 → 视觉层）一致。
* 短剧专属元信息（所属阶段 / 黄金前3秒钩子 / 情绪流变 / 本集结构 / 中段小钩 / 总时长）无原生字段承载，
  放进顶层命名空间键 ``short_drama_meta``（剧本顶层容忍额外字段，见 ``script_models`` 与
  ``data_validator._validate_episode_payload``）；集尾断章卡点语义对齐 ArcReel 原生 ``hook``（集尾钩子）。
"""

from __future__ import annotations

import math
import re
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
    characters: list[str] = field(default_factory=list)
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
_EPISODE_RE = re.compile(r"^第【(\d+)】集[：:]\s*(.*)$")
_SCENE_RE = re.compile(r"^\[场景\][：:]\s*(.+)$")
_PERSON_RE = re.compile(r"^\[人物\][：:]\s*(.+)$")
_ACTION_PREFIX = "△"
_DIALOGUE_RE = re.compile(r"^(?P<speaker>.+?)(?:（(?P<stage>[^）]+)）)?[：:]\s*(?P<text>.+)$")
_NARRATION_HINTS = ("旁白", "画外音", "解说", "narrator", "vo")


def _split_character_names(line: str) -> list[str]:
    """``林辰（隐忍龙王）、陈峰（嚣张富二代）`` → ``["林辰", "陈峰"]``。"""
    names: list[str] = []
    for part in re.split(r"[、,，]", line):
        part = part.strip()
        if not part:
            continue
        name = re.split(r"[（(]", part)[0].strip()
        if name:
            names.append(name)
    return names


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
            current = _Block(location=scene_match.group(1).strip())
            parsed.blocks.append(current)
            continue

        person_match = _PERSON_RE.match(line)
        if person_match:
            current.characters = _split_character_names(person_match.group(1))
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
                current.beats.append(
                    _Beat(kind="dialogue", text=text, speaker=speaker, stage=stage, raw=raw)
                )
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


def _split_counts(total: int, bins: int) -> list[int]:
    """把 ``total`` 尽量均分到 ``bins`` 份，余数给前几份。保证每份 ≥1（调用方已约束 total≥1）。"""
    base = total // bins
    remainder = total - base * bins
    return [base + (1 if i < remainder else 0) for i in range(bins)]


def _split_beat_counts(beat_count: int, bins: int) -> list[int]:
    """把 ``beat_count`` 个节拍尽量均分到 ``bins`` 幕。"""
    if beat_count == 0:
        return [0] * bins
    base = beat_count // bins
    remainder = beat_count - base * bins
    return [base + (1 if i < remainder else 0) for i in range(bins)]


def _flatten(parsed: ParsedEpisode) -> tuple[list[_Beat], list[str], list[list[str]]]:
    beats: list[_Beat] = []
    locations: list[str] = []
    char_groups: list[list[str]] = []
    for block in parsed.blocks:
        for beat in block.beats:
            beats.append(beat)
            locations.append(block.location)
            char_groups.append(block.characters)
    return beats, locations, char_groups


def _split_into_scenes(parsed: ParsedEpisode, episode: int) -> list[dict[str, Any]]:
    beats, locations, char_groups = _flatten(parsed)
    total = parsed.total_duration or 60
    beat_count = len(beats)

    scene_count = max(1, math.ceil(total / 60))  # 单幕 ≤60 秒
    if beat_count and beat_count < scene_count:
        scene_count = beat_count  # 节拍比幕少时，每幕一个节拍，避免空幕

    durations = _split_counts(total, scene_count)
    beat_counts = _split_beat_counts(beat_count, scene_count)

    scenes: list[dict[str, Any]] = []
    beat_index = 0
    for scene_index in range(scene_count):
        count = beat_counts[scene_index]
        sub_beats = beats[beat_index : beat_index + count]
        slice_start, slice_end = beat_index, beat_index + count
        beat_index = slice_end

        if sub_beats:
            location = locations[slice_end - 1]
            characters = sorted(
                {name for group in char_groups[slice_start:slice_end] for name in group}
            )
        else:
            location = locations[-1] if locations else ""
            characters = sorted({name for group in char_groups for name in group})

        scenes.append(
            {
                "scene_id": f"E{episode}S{scene_index + 1}",
                "duration_seconds": durations[scene_index],
                "segment_break": scene_index == scene_count - 1,
                "characters_in_scene": characters,
                "scenes": [location] if location else [],
                "props": [],
                "image_prompt": None,
                "video_prompt": None,
                "utterances": _build_utterances(sub_beats),
                "source_text": "\n".join(beat.raw.strip() for beat in sub_beats),
                "scene_description": _build_scene_description(sub_beats),
                "generated_assets": {},
                "pending_authoring": True,
                "note": None,
            }
        )
    return scenes


def convert_to_drama_script(parsed: ParsedEpisode, episode: int) -> dict[str, Any]:
    """把解析结果转换为 ``DramaEpisodeScript`` 兼容的 dict（含顶层 ``short_drama_meta`` 命名空间）。"""
    scenes = _split_into_scenes(parsed, episode)
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
