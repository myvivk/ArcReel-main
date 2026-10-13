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
  不劈断台词或动作行）**，而不是把整集拍平后均分。短剧导入走剧情窗口（见
  :data:`STORY_SCENE_WINDOW_SECONDS`）：单幕时长按内容节奏落在窗口内，**不吸附任何视频供应商档位**
  ——导入阶段并不知道用户最终用哪个模型生成视频，拿档位去吸附等于用拍脑袋的值替用户做决定，
  档位对齐留给生成期（分镜路线的生成预检）。剧本只给整集总时长、不给逐拍秒数，每拍时长按均匀
  估算分摊。这是格式对齐所需的必要拆分，不丢弃任何内容。
* 视觉层（``image_prompt`` / ``video_prompt``）留空并置 ``pending_authoring=True``，交给 ArcReel 既有的
  ``generate_episode_script``（提示词编写）补出——与 ArcReel 两段式（内容层 → 视觉层）一致。
* 短剧专属元信息（所属阶段 / 黄金前3秒钩子 / 情绪流变 / 本集结构 / 中段小钩 / 总时长）无原生字段承载，
  放进顶层命名空间键 ``short_drama_meta``（剧本顶层容忍额外字段，见 ``script_models`` 与
  ``data_validator._validate_episode_payload``）；集尾断章卡点语义对齐 ArcReel 原生 ``hook``（集尾钩子）。
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
# 同时接受短剧工厂原生标题和 Web 导入弹窗的兼容格式。
_EPISODE_RE = re.compile(r"^(?:#\s*)?第\s*[【\[]?\s*(\d+)\s*[】\]]?\s*集(?:\s*[：:]?\s*(.*))?$")
_SCENE_RE = re.compile(r"^\[场景\][：:]\s*(.+)$")
_PERSON_RE = re.compile(r"^\[人物\][：:]\s*(.+)$")
_ACTION_PREFIX = "△"
_DIALOGUE_RE = re.compile(r"^(?P<speaker>.+?)(?:（(?P<stage>[^）]+)）)?[：:]\s*(?P<text>.+)$")
_NARRATION_HINTS = ("旁白", "画外音", "解说", "narrator", "vo")

#: 剧情驱动的切分窗口（秒）：单幕时长落在 [下限, 上限] 内。
#:
#: 导入阶段不读视频供应商能力（用户此时未必已选定生成模型），因此**不做档位吸附**——
#: 单幕时长由内容节奏决定，档位对齐交给生成期的预检。窗口取 12~15 秒：短剧单幕要装下
#: 一个完整的往返回合，短于 12 秒会碎成流水账，长于 15 秒单镜头信息量过载。
STORY_SCENE_WINDOW_SECONDS: tuple[int, int] = (12, 15)


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
    durations = [round(total * count / beat_total) for count in beat_counts]
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


def _build_scene(
    episode: int,
    scene_index: int,
    block: _Block,
    sub_beats: list[_Beat],
    duration: int,
    characters: list[str] | None = None,
) -> dict[str, Any]:
    """一幕的剧本载荷。``characters`` 缺省取所属块的人物；跨块合并的幕由调用方给并集。

    ``segment_break`` 恒为 False：末幕之后没有下一幕，把它标成场景切换点只是个哨兵，
    却会让整集被切成「13 + 1」两组——尾组只有一格要多出一张联合图，且断章那一格因此
    拿不到上一幕的首帧参考（见 ``storyboard_sequence.previous_storyboard_image``）。
    需要硬切的场景由创作者在界面上显式标记。
    """

    return {
        "scene_id": f"E{episode}S{scene_index + 1}",
        "duration_seconds": duration,
        "segment_break": False,
        "characters_in_scene": sorted(set(characters if characters is not None else block.characters)),
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


def _split_by_story_window(
    parsed: ParsedEpisode, episode: int, min_seconds: int, max_seconds: int
) -> list[dict[str, Any]]:
    """按剧情节奏切幕：单幕时长落在 ``[min_seconds, max_seconds]`` 窗口内。

    切点优先级仍是「【场景】块边界 > 拍与拍之间」，绝不劈断一句台词或一个动作行：

    * 已进窗口（``>= 下限``）且走到块边界，或再塞一拍会超上限 → 在此收口；
    * 还没到下限就走到块边界 → 继续往下一块合并，宁可跨块也不产出一幕碎片。

    窗口是软约束：整集拍数很少时单个拍的估算时长本身就可能超过上限，那一幕只能超出——
    劈断一个拍的代价（台词/动作被切开）远大于超出上限。
    """

    total = parsed.total_duration or 60
    flat: list[tuple[_Block, _Beat]] = [(block, beat) for block in parsed.blocks for beat in block.beats]
    if not flat:
        return [_build_scene(episode, 0, _Block(), [], total)]

    per_beat = total / len(flat)
    buckets: list[list[tuple[_Block, _Beat]]] = []
    current: list[tuple[_Block, _Beat]] = []
    accumulated = 0.0
    for block, beat in flat:
        if current:
            at_block_boundary = current[-1][0] is not block
            if accumulated >= min_seconds and (accumulated + per_beat > max_seconds or at_block_boundary):
                buckets.append(current)
                current = []
                accumulated = 0.0
        current.append((block, beat))
        accumulated += per_beat
    if current:
        buckets.append(current)

    durations = _windowed_durations([len(bucket) for bucket in buckets], total, min_seconds, max_seconds)
    scenes: list[dict[str, Any]] = []
    for index, bucket in enumerate(buckets):
        blocks = [block for block, _ in bucket]
        # 跨块合并的幕：场景名取首块（一幕一个主场景），在场角色取各块并集，画面提示词才不会漏人。
        characters = sorted({name for block in blocks for name in block.characters})
        scenes.append(
            _build_scene(
                episode,
                index,
                blocks[0],
                [beat for _, beat in bucket],
                durations[index],
                characters,
            )
        )
    return scenes


def _windowed_durations(counts: list[int], total: int, min_seconds: int, max_seconds: int) -> list[int]:
    """各幕按拍数占本集总时长的比例估算时长，钳进窗口后再校准回 ``total``。

    拍数是内容体量的唯一可算代理（剧本 Markdown 不给逐拍秒数），因此比例估算是"按篇幅分摊"；
    钳进窗口后总和会偏离 ``total``，交给 :func:`_fit_total` 按 ±1 微调拉回——偏差大小由
    导入回执的 ``duration_shortfall`` 报给用户。
    """

    beat_total = sum(counts)
    if beat_total == 0:
        return [total] if counts else []
    raw = [round(total * count / beat_total) for count in counts]
    return _fit_within_window(
        [min(max(value, min_seconds), max_seconds) for value in raw], total, min_seconds, max_seconds
    )


def _fit_within_window(values: list[int], target: int, min_seconds: int, max_seconds: int) -> list[int]:
    """在窗口内尽力把各幕时长凑回 ``target``：只按 ±1 挪，且挪完不能越出窗口。

    窗口是硬约束：宁可让整集总时长对不上（偏差由导入回执的 ``duration_shortfall`` 报出），
    也不为了凑数把某一幕挪到 12~15 秒之外。动不了就停手，不反复试。
    """

    if not values:
        return values
    diff = target - sum(values)
    order = sorted(range(len(values)), key=lambda i: -values[i])
    while diff != 0:
        moved = False
        for i in order:
            if diff > 0 and values[i] + 1 <= max_seconds:
                values[i] += 1
                diff -= 1
                moved = True
            elif diff < 0 and values[i] - 1 >= min_seconds:
                values[i] -= 1
                diff += 1
                moved = True
            if diff == 0:
                break
        if not moved:
            break
    return values


def _split_into_scenes(
    parsed: ParsedEpisode, episode: int, supported_durations: list[int] | None = None
) -> list[dict[str, Any]]:
    """按剧本结构切幕：以【场景】块边界为首选切点，块内按拍细分到不超过单幕上限。

    切点优先级是「场景切换 > 拍与拍之间」，绝不劈断一句台词或一个动作行——剧本 Markdown
    只给整集总时长，不给逐拍秒数，故每拍时长取均匀估算（``total / 总拍数``）。
    """
    tiers = sorted({int(d) for d in (supported_durations or []) if int(d) > 0})
    # 单幕上限取调用方给出的规划时长；None 保留给兼容调用方的 60 秒旧口径。
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
    return [
        # 一段幕只属于一个【场景】块，场景名与在场角色直接取所属块，不用跨块并集。
        _build_scene(episode, index, block, beats, durations[index])
        for index, ((block, beats)) in enumerate(buckets)
    ]


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


def convert_to_drama_script(
    parsed: ParsedEpisode,
    episode: int,
    supported_durations: list[int] | None = None,
    scene_window: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """把解析结果转换为 ``DramaEpisodeScript`` 兼容的 dict（含顶层 ``short_drama_meta`` 命名空间）。

    两种切分口径二选一：

    * ``scene_window``：按剧情窗口切幕（短剧导入走这条），单幕时长由内容节奏决定，不做档位吸附；
    * ``supported_durations``：按调用方给出的时长档位切幕并吸附到档位（旧口径，None 沿用 60 秒兼容上限）。
    """
    scenes = (
        _split_by_story_window(parsed, episode, *scene_window)
        if scene_window is not None
        else _split_into_scenes(parsed, episode, supported_durations)
    )
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
