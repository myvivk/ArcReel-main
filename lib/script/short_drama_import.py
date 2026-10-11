"""将 short-drama-factory 产出的 Markdown 剧本转换为 ArcReel 剧情演绎（drama）剧本 JSON。

本模块只负责"产物转换"（Markdown → :class:`lib.script.script_models.DramaEpisodeScript` 形状）。
short-drama-factory 的编剧方法论与模板**原样复刻**在
``agent_runtime_profile/.claude/skills/short-drama-factory/``，本模块不重述、不增删其任何规则。

转换约定（忠实保留、不增删原文内容）：

* 一集 Markdown = 一个文件；解析为一份 ``DramaEpisodeScript``，集号由调用方传入（ArcReel 剧本 JSON
  不存集号，落盘时由 ``episode`` 字段携带）。
* 每个 ``[场景]`` 块的动作行（``△ …``）与台词神态并入该幕 ``scene_description``；台词正文进
  ``utterances``（带说话人=dialogue，旁白/画外音=voiceover）。原文逐字保留在 ``source_text`` 追溯锚。
* 短剧单集 90~195 秒，超过 ArcReel 单幕 ``duration_seconds`` 上限，故切成若干幕。**切点按剧本结构
  取：以 ``[场景]`` 块边界为首选项，场景本身超上限时才在块内按 beat 细分（切点仍在 beat 边界上，
  不劈断台词或动作行）**，而不是把整集拍平后均分。单幕上限取所选视频模型的最大时长档位（档位
  未知时退回 60 秒静态上限）；剧本只给整集总时长、不给逐拍秒数，每拍时长按均匀估算分摊，
  每幕时长再吸附到最近的合法档位——生成期拿不到档位的时长会被 backend 拒，落盘前必须对齐。
  这是格式对齐所需的必要拆分，不丢弃任何内容。
* 视觉层（``image_prompt`` / ``video_prompt``）留空并置 ``pending_authoring=True``，交给 ArcReel 既有的
  ``generate_episode_script``（提示词编写）补出——与 ArcReel 两段式（内容层 → 视觉层）一致。
* 短剧专属元信息（所属阶段 / 黄金前3秒钩子 / 情绪流变 / 本集结构 / 中段小钩 / 总时长）无原生字段承载，
  放进顶层命名空间键 ``short_drama_meta``（剧本顶层容忍额外字段，见 ``script_models`` 与
  ``data_validator._validate_episode_payload``）；集尾断章卡点语义对齐 ArcReel 原生 ``hook``（集尾钩子）。
"""

from __future__ import annotations

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


def _fit_total(values: list[int], target: int) -> list[int]:
    """把整数时长列表微调到合计等于 ``target``：±1 级增减，优先动时长最大的幕。

    只在没有档位（ComfyUI 类端点，时长不由 ArcReel 驱动）时兜底；有档位时时长必须
    落在档位上，交给 :func:`_align_to_tiers` 按整档处理。
    """
    if not values:
        return values
    diff = target - sum(values)
    order = sorted(range(len(values)), key=lambda i: -values[i])
    while diff != 0:
        moved = False
        for i in order:
            if diff > 0:
                values[i] += 1
                diff -= 1
                moved = True
            elif diff < 0 and values[i] > 1:
                values[i] -= 1
                diff += 1
                moved = True
            if diff == 0:
                break
        if not moved:
            break
    return values


def _estimate_durations(beat_counts: list[int], total: int, tiers: list[int]) -> list[int]:
    """各幕按拍数占本集总时长的比例估算时长，再吸附到合法档位并校准回 ``total``。

    逐拍时长取均匀值（剧本 Markdown 不给逐拍秒数），因此这是"按篇幅分摊"而非真实卡点；
    有了档位就把每幕吸附到档位并把取整与吸附的偏差一起摊回，保证落盘时长模型一定收得了。
    """
    beat_total = sum(beat_counts)
    if beat_total == 0:
        return [total] if beat_counts else []
    durations = [int(round(total * count / beat_total)) for count in beat_counts]
    if tiers:
        return _align_to_tiers(durations, tiers, target=total)
    return _fit_total(durations, total)


def _snap_to_tier(value: int, tiers: list[int]) -> int:
    """把任意时长吸附到最近的合法档位；距离并列取较小档。``tiers`` 升序且非空。"""
    return min(tiers, key=lambda tier: (abs(tier - value), tier))


def _align_to_tiers(values: list[int], tiers: list[int], target: int) -> list[int]:
    """把估算时长逐个吸附到合法档位，再把偏差摊回各幕，让合计尽量贴近 ``target``。

    偏差有两个来源：逐幕按比例取整（180 秒摊 15 幕会涨到 186），以及吸附本身
    （如 20+19 吸附成 15+15）。两者一次算清：``target`` 是本集总时长，每幕只能
    上/下移整档，动不出更近档时停手。返回值每项都是合法档位。
    """
    snapped = [_snap_to_tier(value, tiers) for value in values]
    diff = target - sum(snapped)
    index = 0
    while diff != 0:
        before = diff
        for i, current in enumerate(snapped):
            if diff > 0:
                higher = [tier for tier in tiers if tier > current]
                if higher and higher[0] - current <= diff:
                    diff -= higher[0] - current
                    snapped[i] = higher[0]
            elif diff < 0:
                lower = [tier for tier in tiers if tier < current]
                if lower and current - lower[-1] <= -diff:
                    diff += current - lower[-1]
                    snapped[i] = lower[-1]
            if diff == 0:
                break
        if diff == before:
            break  # 各幕都动不出了，取已达的最接近总和
        index += 1
        if index > len(snapped) * len(tiers):
            break
    return snapped


def _split_into_scenes(
    parsed: ParsedEpisode, episode: int, supported_durations: list[int] | None = None
) -> list[dict[str, Any]]:
    """按剧本结构切幕：以【场景】块边界为首选切点，块内按拍细分到不超过单幕上限。

    切点优先级是「场景切换 > 拍与拍之间」，绝不劈断一句台词或一个动作行——剧本 Markdown
    只给整集总时长，不给逐拍秒数，故每拍时长取均匀估算（``total / 总拍数``）。
    """
    tiers = sorted({int(d) for d in (supported_durations or []) if int(d) > 0})
    # 单幕上限取所选视频模型的最大时长档位；能力未知时退回 60 秒静态上限（旧行为）。
    max_duration = tiers[-1] if tiers else 60
    total = parsed.total_duration or 60
    beat_total = sum(len(block.beats) for block in parsed.blocks)
    per_beat = (total / beat_total) if beat_total else 0.0

    # 每个【场景】块优先整块成幕；估算时长超上限才在块内按拍细分，切点仍在拍边界上。
    buckets: list[tuple[_Block, list[_Beat]]] = []
    for block in parsed.blocks:
        if not block.beats:
            continue  # 只有场景头、没有任何内容的空块不单独成幕
        current: list[_Beat] = []
        accumulated = 0.0
        for beat in block.beats:
            if current and accumulated + per_beat > max_duration:
                buckets.append((block, current))
                current = []
                accumulated = 0.0
            current.append(beat)
            accumulated += per_beat
        if current:
            buckets.append((block, current))

    if not buckets:
        # 全集没有任何带内容的【场景】块：退化成单一空幕，仍带整集时长，避免产出 0 幕。
        buckets = [(_Block(), [beat for block in parsed.blocks for beat in block.beats])]

    durations = _estimate_durations([len(beats) for _, beats in buckets], total, tiers)

    scenes: list[dict[str, Any]] = []
    for scene_index, (block, sub_beats) in enumerate(buckets):
        scenes.append(
            {
                "scene_id": f"E{episode}S{scene_index + 1}",
                "duration_seconds": durations[scene_index],
                "segment_break": scene_index == len(buckets) - 1,
                # 一段幕只属于一个【场景】块，场景名与在场角色直接取所属块，不用跨块并集。
                "characters_in_scene": sorted(set(block.characters)),
                "scenes": [block.location] if block.location else [],
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


def convert_to_drama_script(
    parsed: ParsedEpisode, episode: int, supported_durations: list[int] | None = None
) -> dict[str, Any]:
    """把解析结果转换为 ``DramaEpisodeScript`` 兼容的 dict（含顶层 ``short_drama_meta`` 命名空间）。

    ``supported_durations`` 是所选视频模型的合法时长档位；给了就按它决定幕数并把每幕时长
    吸附到合法档位，没给（能力解析失败/未知模型）退回 60 秒静态上限。
    """
    scenes = _split_into_scenes(parsed, episode, supported_durations)
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
