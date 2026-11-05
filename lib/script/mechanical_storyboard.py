"""机械分镜：把工厂源稿的 beats 确定性切成 12–15 秒的分镜段。

本模块只做纯计算：不调用文本模型、不读供应商配置、不写盘。同一份源稿与资产表必须得到
同一份 `MechanicalStoryboard`（分镜 ID、beat 分组、时长、资产清单、描述与原文锚逐字一致）。

规则与取舍见 ``方案设计/10. 机械分镜与分镜提示词分步方案.md``：

- 切点优先 ``[场景]`` 块边界，台词拍不单独成段首；
- 单段时长落在 :data:`STORY_SCENE_WINDOW_SECONDS`，窗口是硬约束；
- 台词按语速估算，必须给画面留出至少一个镜头的预算；
- 场景归属在没有提示词 token 可依时按「唯一场景 → beat 文本点名 → 全集唯一 → 首场景 + 告警」
  的确定性顺序判定。
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lib.project.asset_types import asset_name_comparison_key
from lib.script.short_drama_import import (
    STORY_SCENE_WINDOW_SECONDS,
    ParsedEpisode,
    SceneAssets,
    _Beat,
    _Block,
    _build_scene_description,
)
from lib.speech.speech_rate import estimate_spoken_seconds

#: 机械分镜规则版本：规则或切分口径变化时递增，使既有结构与提示词按新规则重建。
MECHANICAL_STORYBOARD_VERSION = 2

#: 单段目标时长（秒）：12–15 秒窗口内，取剧中位。
TARGET_SCENE_SECONDS = 13.5

#: 动作拍固定秒数；台词按语速估算。
ACTION_BEAT_SECONDS = 2.0

#: 台词必须给画面留出的最低预算（一个镜头）。
MIN_SHOT_SECONDS = 1.2

#: 同一个块内切分的代价：优先在 [场景] 块边界收口。
_CUT_INSIDE_BLOCK_PENALTY = 8.0
#: 以台词拍开段的代价：台词要跟它前面的动作拍同段。
_CUT_BEFORE_DIALOGUE_PENALTY = 20.0
#: 近似时长越出窗口的代价；足够大，让 DP 先保证窗口。
_WINDOW_VIOLATION_PENALTY = 1000.0
#: 单组 beat 数上限：防止一镜塞进整集；只影响搜索规模，不改变语义。
_MAX_BEATS_PER_GROUP = 80


class StoryboardSplitError(ValueError):
    """源稿无法机械切成合法分镜；消息可直接回喂给创作者。"""


@dataclass(frozen=True, slots=True)
class StoryboardWarning:
    """可肉眼修正、但不阻断落盘的结构告警（如多场景块无法确定性归属）。"""

    code: str
    scene_id: str
    detail: str
    candidates: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "scene_id": self.scene_id,
            "detail": self.detail,
            "candidates": list(self.candidates),
        }


@dataclass(frozen=True, slots=True)
class MechanicalScene:
    """一个机械分镜段：结构、时长、描述与出场资产。"""

    scene_id: str
    beat_ids: tuple[int, ...]
    duration_seconds: int
    scene_description: str
    assets: SceneAssets
    segment_break: bool = False


@dataclass(frozen=True, slots=True)
class MechanicalStoryboard:
    """一集的机械分镜产物；``declared_seconds`` 为源稿标注时长（未知时 0）。"""

    scenes: tuple[MechanicalScene, ...]
    warnings: tuple[StoryboardWarning, ...]
    declared_seconds: int
    actual_seconds: int


def _flat_beats(parsed: ParsedEpisode) -> list[tuple[_Block, _Beat]]:
    return [(block, beat) for block in parsed.blocks for beat in block.beats]


def _beat_seconds(beat: _Beat) -> float:
    """一拍的大致秒数：台词按语速估算，动作固定。"""
    if beat.kind == "dialogue":
        return max(1.0, float(estimate_spoken_seconds(beat.text)))
    return ACTION_BEAT_SECONDS


def _scene_count(total_seconds: float, beat_count: int) -> int:
    """按目标段长算段数，并让总时长尽量落在窗口内。"""
    low, high = STORY_SCENE_WINDOW_SECONDS
    count = max(1, min(beat_count, round(total_seconds / TARGET_SCENE_SECONDS) or 1))
    while count > 1 and total_seconds / count < low:
        count -= 1
    while total_seconds / count > high and count < beat_count:
        count += 1
    # 增加段数可能把单段压到窗口下限以下：宁可回到更少的段，也不要为了凑段强拆。
    while count > 1 and total_seconds / count < low:
        count -= 1
    return count


def _window_cost(duration: float) -> float:
    low, high = STORY_SCENE_WINDOW_SECONDS
    if duration < low:
        return _WINDOW_VIOLATION_PENALTY + (low - duration) * 10
    if duration > high:
        return _WINDOW_VIOLATION_PENALTY + (duration - high) * 10
    return 0.0


def _choose_cuts(
    entries: Sequence[tuple[_Block, _Beat]],
    weights: Sequence[float],
    total_seconds: float,
    count: int,
) -> list[int]:
    """动态规划选 ``count - 1`` 个切点，返回每段起始 beat 下标（含 0）。

    代价由「近似段长偏离目标」「块内切分」「台词开段」「越窗」组成；同代价取更早切点，
    保证同一输入总是同一结果。
    """
    beat_count = len(entries)
    if count <= 1:
        return [0]
    prefix = [0.0]
    for weight in weights:
        prefix.append(prefix[-1] + weight)
    total_weight = prefix[-1] or 1.0

    def group_cost(start: int, end: int) -> float:
        weight = prefix[end] - prefix[start]
        approx = total_seconds * weight / total_weight
        cost = abs(approx - TARGET_SCENE_SECONDS) + _window_cost(approx)
        if start > 0:
            if entries[start][0] is entries[start - 1][0]:
                cost += _CUT_INSIDE_BLOCK_PENALTY
            if entries[start][1].kind == "dialogue":
                cost += _CUT_BEFORE_DIALOGUE_PENALTY
        return cost

    infinity = float("inf")
    # best[g][i]：前 i 个 beat 分成 g 段的代价；cuts[g][i] 记录最后一段的起点。
    best: list[list[float]] = [[infinity] * (beat_count + 1) for _ in range(count + 1)]
    cuts: list[list[int]] = [[-1] * (beat_count + 1) for _ in range(count + 1)]
    best[0][0] = 0.0
    for groups in range(1, count + 1):
        for end in range(groups, beat_count + 1):
            low_start = max(groups - 1, end - _MAX_BEATS_PER_GROUP)
            best_cost = infinity
            best_start = -1
            for start in range(low_start, end):
                if best[groups - 1][start] == infinity:
                    continue
                candidate = best[groups - 1][start] + group_cost(start, end)
                if candidate < best_cost:
                    best_cost = candidate
                    best_start = start
            best[groups][end] = best_cost
            cuts[groups][end] = best_start
    starts = [0]
    end = beat_count
    for groups in range(count, 0, -1):
        start = cuts[groups][end]
        if start < 0:
            raise StoryboardSplitError("源稿无法切成合法的分镜段")
        starts.append(start)
        end = start
    # starts 目前是逆序的最后一段起点序列；补上 0 后去重排序。
    return sorted(set(starts))


def _allocate_durations(
    weights: Sequence[float],
    spoken: Sequence[float],
    total_seconds: float,
) -> list[int]:
    """按权重把整集时长分摊到各段，钳进窗口并用 ±1 微调校准总和。"""
    low, high = STORY_SCENE_WINDOW_SECONDS
    count = len(weights)
    if count == 0:
        return []
    if count == 1:
        duration = max(1, round(total_seconds))
        if spoken[0] > duration - MIN_SHOT_SECONDS:
            raise StoryboardSplitError(_dialogue_message(1, spoken[0], duration))
        return [duration]
    total_weight = sum(weights) or 1.0
    raw = [total_seconds * weight / total_weight for weight in weights]
    values = [round(min(max(value, low), high)) for value in raw]
    diff = round(total_seconds - sum(values))
    order = sorted(range(count), key=lambda index: (-values[index], index))
    while diff != 0:
        moved = False
        for index in order:
            if diff > 0 and values[index] + 1 <= high:
                values[index] += 1
                diff -= 1
                moved = True
            elif diff < 0 and values[index] - 1 >= low:
                values[index] -= 1
                diff += 1
                moved = True
            if diff == 0:
                break
        if not moved:
            break
    durations = list(values)
    for index, (duration, demanded) in enumerate(zip(durations, spoken, strict=True), 1):
        if demanded > duration - MIN_SHOT_SECONDS:
            raise StoryboardSplitError(_dialogue_message(index, demanded, duration))
    return durations


def _dialogue_message(index: int, spoken: float, duration: int) -> str:
    return (
        f"第 {index} 段台词念完约需 {spoken:.1f} 秒，该段 {duration} 秒里还要留至少 "
        f"{MIN_SHOT_SECONDS:.1f} 秒给画面；请延长【本集时长】而不是删台词——"
        "单纯增加分镜不增加总时长，压缩台词会损坏剧情"
    )


def _registered_map(registered: Mapping[str, Collection[str]], asset_type: str) -> dict[str, str]:
    return {
        asset_name_comparison_key(str(name)): str(name) for name in registered.get(asset_type, ()) if str(name).strip()
    }


def _mentioned(text: str, names: Mapping[str, str]) -> list[str]:
    """按出现顺序返回文本里点到的登记名；长名优先，避免「林清」命中「林清儿」。"""
    hits: list[tuple[int, str]] = []
    for key, name in names.items():
        position = text.find(name)
        if position < 0:
            position = text.lower().find(key)
        if position >= 0:
            hits.append((position, name))
    return [name for _position, name in sorted(hits, key=lambda hit: (hit[0], -len(hit[1])))]


#: 场景名常见写法「内景・地点・日」「外景/地点/夜」：按这些分隔符取地点别名。
_SCENE_SEPARATORS = ("・", "·", "|", "｜", "/", "／", "-", "—", "–", " ")
#: 只表示景别/时段的片段不能拿来指认场景，否则「内景」「夜」会命中所有段。
_SCENE_STOPWORDS = frozenset({"内景", "外景", "日", "夜", "晨", "昏", "白天", "夜晚", "傍晚", "清晨"})


def _scene_aliases(name: str) -> list[str]:
    """一个登记场景名的可匹配别名：全名、地点片段，以及片段的首尾子串。

    工厂场景名常写成「内景・萧家大厅・日」，正文却只写「大厅」；首尾子串让「大厅」
    能指认到「萧家大厅」，同时跳过「内景」「夜」这类会命中所有段的景别/时段词。
    """
    aliases = [name]
    for separator in _SCENE_SEPARATORS:
        if separator in name:
            for part in name.split(separator):
                part = part.strip()
                if len(part) < 2 or part in _SCENE_STOPWORDS:
                    continue
                if part not in aliases:
                    aliases.append(part)
                for length in range(2, len(part)):
                    for candidate in (part[:length], part[-length:]):
                        if candidate not in _SCENE_STOPWORDS and candidate not in aliases:
                            aliases.append(candidate)
    return aliases


def _scene_mentions(text: str, scenes: Mapping[str, str]) -> list[str]:
    """按出现顺序返回文本里点到的登记场景；场景名带景别/时段时按地点片段匹配。"""
    hits: list[tuple[int, int, str]] = []
    for name in scenes.values():
        position = -1
        matched_alias = ""
        for alias in _scene_aliases(name):
            found = text.find(alias)
            if found >= 0 and (position < 0 or found < position):
                position = found
                matched_alias = alias
        if position >= 0:
            hits.append((position, -len(matched_alias), name))
    return [name for _position, _length, name in sorted(hits)]


def _derive_assets(
    entries: Sequence[tuple[_Block, _Beat]],
    groups: Sequence[Sequence[int]],
    registered: Mapping[str, Collection[str]],
    episode: int,
) -> tuple[list[SceneAssets], list[StoryboardWarning]]:
    """按幕确定性解析出场资产与场景；无法判定场景时给告警而不是中断。"""
    characters = _registered_map(registered, "character")
    props = _registered_map(registered, "prop")
    scenes = _registered_map(registered, "scene")
    flat = list(entries)
    resolved: list[SceneAssets] = []
    warnings: list[StoryboardWarning] = []
    for index, group in enumerate(groups, 1):
        entries_in_group = [flat[beat_id - 1] for beat_id in group]
        beats = [beat for _block, beat in entries_in_group]
        scene_id = f"E{episode}S{index:02d}"
        found_characters: list[str] = []
        found_props: list[str] = []
        for beat in beats:
            visual_text = " ".join(part for part in (beat.text, beat.stage or "") if part)
            for name in _mentioned(visual_text, characters):
                if name not in found_characters:
                    found_characters.append(name)
            if beat.kind == "dialogue" and beat.speaker:
                speaker_key = asset_name_comparison_key(beat.speaker)
                name = characters.get(speaker_key)
                if name and name not in found_characters:
                    found_characters.append(name)
            for name in _mentioned(visual_text, props):
                if name not in found_props:
                    found_props.append(name)

        block_locations: list[str] = []
        for block, _beat in entries_in_group:
            for location in block.locations:
                if location and location not in block_locations:
                    block_locations.append(location)
        text_places: list[str] = []
        for beat in beats:
            visual_text = " ".join(part for part in (beat.text, beat.stage or "") if part)
            for name in _scene_mentions(visual_text, scenes):
                if name not in text_places:
                    text_places.append(name)

        if len(block_locations) == 1:
            scene_names = [block_locations[0]]
        elif text_places:
            scene_names = text_places
        elif len(scenes) == 1:
            scene_names = [next(iter(scenes.values()))]
        else:
            scene_names = [block_locations[0]] if block_locations else []
            candidates = tuple(block_locations or scenes.values())
            if candidates:
                warnings.append(
                    StoryboardWarning(
                        code="scene_attribution_ambiguous",
                        scene_id=scene_id,
                        detail=f"第 {index} 段无法从文本判定场景，已取首场景；请核对本段场景归属",
                        candidates=candidates,
                    )
                )

        resolved.append(
            SceneAssets(
                scenes=tuple(dict.fromkeys(scene_names)),
                characters_in_scene=tuple(found_characters),
                props=tuple(found_props),
            )
        )
    return resolved, warnings


#: 对白密集、12~15 秒一段装不下时改用的「分段优先」窗口。
SEGMENT_FIRST_MIN_SECONDS = 10.0
SEGMENT_FIRST_MAX_SECONDS = 14.0
SEGMENT_FIRST_TARGET_SECONDS = 12.0
#: 分段优先的段数上限，避免为了塞台词碎成流水账。
SEGMENT_FIRST_MAX_SEGMENTS = 12


def _partition_balanced(
    entries: Sequence[tuple[_Block, _Beat]],
    weights: Sequence[float],
    count: int,
) -> list[list[int]]:
    """把连续 beat 均分成 ``count`` 段，优先在台词前切、避免块内切。"""

    beat_count = len(entries)
    prefix = [0.0]
    for weight in weights:
        prefix.append(prefix[-1] + weight)
    target = (prefix[-1] or 1.0) / count
    infinity = float("inf")
    best: list[list[float]] = [[infinity] * (beat_count + 1) for _ in range(count + 1)]
    cuts: list[list[int]] = [[-1] * (beat_count + 1) for _ in range(count + 1)]
    best[0][0] = 0.0
    for group_index in range(1, count + 1):
        for end in range(group_index, beat_count + 1):
            for start in range(group_index - 1, end):
                if best[group_index - 1][start] == infinity:
                    continue
                group_weight = prefix[end] - prefix[start]
                cost = (group_weight - target) ** 2
                if start > 0:
                    if entries[start][0] is entries[start - 1][0]:
                        cost += _CUT_INSIDE_BLOCK_PENALTY
                    if entries[start][1].kind == "dialogue":
                        cost += _CUT_BEFORE_DIALOGUE_PENALTY
                candidate = best[group_index - 1][start] + cost
                if candidate < best[group_index][end]:
                    best[group_index][end] = candidate
                    cuts[group_index][end] = start
    if cuts[count][beat_count] < 0:
        raise StoryboardSplitError("源稿无法切成合法的分镜段")
    segments: list[list[int]] = []
    end = beat_count
    for group_index in range(count, 0, -1):
        start = cuts[group_index][end]
        segments.append(list(range(start + 1, end + 1)))
        end = start
    segments.reverse()
    return segments


def _segment_first_split(
    entries: Sequence[tuple[_Block, _Beat]],
    weights: Sequence[float],
) -> tuple[list[list[int]], list[int]]:
    """先按台词定 10~12 秒段数，再反推每段时长；段不够就多切一段，不删台词。"""

    spoken_total = sum(
        weights[beat_id - 1] for beat_id in range(1, len(entries) + 1) if entries[beat_id - 1][1].kind == "dialogue"
    )
    content_total = sum(weights)
    capacity = SEGMENT_FIRST_MAX_SECONDS - MIN_SHOT_SECONDS
    base_count = max(1, math.ceil(spoken_total / capacity))
    base_count = max(base_count, math.ceil(content_total / SEGMENT_FIRST_MAX_SECONDS))
    base_count = min(base_count, len(entries), SEGMENT_FIRST_MAX_SEGMENTS)
    last_error: StoryboardSplitError | None = None
    candidates: list[tuple[int, list[list[int]], list[int]]] = []
    for count in range(base_count, min(len(entries), SEGMENT_FIRST_MAX_SEGMENTS) + 1):
        groups = _partition_balanced(entries, weights, count)
        raw = [sum(weights[beat_id - 1] for beat_id in group) + MIN_SHOT_SECONDS for group in groups]
        target_total = round(sum(raw))
        values = [
            min(int(SEGMENT_FIRST_MAX_SECONDS), max(int(SEGMENT_FIRST_MIN_SECONDS), round(value))) for value in raw
        ]
        diff = target_total - sum(values)
        order = sorted(range(len(values)), key=lambda index: (-values[index], index))
        while diff != 0:
            moved = False
            for index in order:
                if diff > 0 and values[index] + 1 <= SEGMENT_FIRST_MAX_SECONDS:
                    values[index] += 1
                    diff -= 1
                    moved = True
                elif diff < 0 and values[index] - 1 >= SEGMENT_FIRST_MIN_SECONDS:
                    values[index] -= 1
                    diff += 1
                    moved = True
                if diff == 0:
                    break
            if not moved:
                break
        failed = False
        for index, (group, _duration) in enumerate(zip(groups, values, strict=True), 1):
            demanded = sum(weights[beat_id - 1] for beat_id in group if entries[beat_id - 1][1].kind == "dialogue")
            need = math.ceil(demanded + MIN_SHOT_SECONDS)
            if need > values[index - 1] and need <= SEGMENT_FIRST_MAX_SECONDS:
                values[index - 1] = need
            if demanded > values[index - 1] - MIN_SHOT_SECONDS:
                last_error = StoryboardSplitError(_dialogue_message(index, demanded, values[index - 1]))
                failed = True
                break
        if not failed:
            candidates.append((sum(values), groups, values))
    if candidates:
        _total, groups, values = min(candidates, key=lambda item: item[0])
        return groups, values
    if last_error is not None:
        raise last_error
    raise StoryboardSplitError("源稿无法切成合法的分镜段")


def split_episode(
    parsed: ParsedEpisode,
    episode: int,
    *,
    registered: Mapping[str, Collection[str]],
    declared_seconds: int | None = None,
) -> MechanicalStoryboard:
    """把一集源稿机械切成完整分镜；输入非法时抛 :class:`StoryboardSplitError`。"""
    entries = _flat_beats(parsed)
    if not entries:
        raise StoryboardSplitError("源稿没有可分镜的动作或台词")
    weights = [_beat_seconds(beat) for _block, beat in entries]
    declared = int(parsed.total_duration or declared_seconds or 0)
    total_seconds = float(declared) if declared > 0 else max(12.0, round(sum(weights)))
    declared_seconds = declared
    try:
        count = _scene_count(total_seconds, len(entries))
        starts = _choose_cuts(entries, weights, total_seconds, count)
        bounds = [*starts, len(entries)]
        groups = [list(range(start + 1, end + 1)) for start, end in itertools.pairwise(bounds)]
        if not groups:
            raise StoryboardSplitError("源稿无法切成合法的分镜段")
        group_weights = [sum(weights[beat_id - 1] for beat_id in group) for group in groups]
        spoken = [
            sum(weights[beat_id - 1] for beat_id in group if entries[beat_id - 1][1].kind == "dialogue")
            for group in groups
        ]
        durations = _allocate_durations(group_weights, spoken, total_seconds)
    except StoryboardSplitError:
        # 12~15 秒一段装不下时，按台词量多切 1~2 段（10~12 秒/段），总时长由段相加反推，
        # 而不是删台词把剧情压坏。
        groups, durations = _segment_first_split(entries, weights)
        declared_seconds = sum(durations)
    assets, warnings = _derive_assets(entries, groups, registered, episode)
    scenes: list[MechanicalScene] = []
    for index, (group, duration) in enumerate(zip(groups, durations, strict=True), 1):
        beats = [entries[beat_id - 1][1] for beat_id in group]
        scenes.append(
            MechanicalScene(
                scene_id=f"E{episode}S{index:02d}",
                beat_ids=tuple(group),
                duration_seconds=duration,
                scene_description=_build_scene_description(beats),
                assets=assets[index - 1],
            )
        )
    return MechanicalStoryboard(
        scenes=tuple(scenes),
        warnings=tuple(warnings),
        declared_seconds=declared_seconds,
        actual_seconds=sum(scene.duration_seconds for scene in scenes),
    )


__all__ = [
    "ACTION_BEAT_SECONDS",
    "MECHANICAL_STORYBOARD_VERSION",
    "MIN_SHOT_SECONDS",
    "TARGET_SCENE_SECONDS",
    "MechanicalScene",
    "MechanicalStoryboard",
    "StoryboardSplitError",
    "StoryboardWarning",
    "split_episode",
]
