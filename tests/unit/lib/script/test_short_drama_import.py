"""短剧导入的剧情窗口切幕：单幕时长按内容节奏落在 12~15 秒，不做视频供应商档位吸附。"""

from __future__ import annotations

from lib.script.short_drama_import import (
    STORY_SCENE_WINDOW_SECONDS,
    convert_to_drama_script,
    parse_short_drama_episode,
)
from lib.script.storyboard_sequence import group_scenes_by_segment_break

MIN_S, MAX_S = STORY_SCENE_WINDOW_SECONDS


def _md(beats_per_block: list[int], total: int = 120) -> str:
    """按每块的拍数拼一份剧本 Markdown：拍越多内容越多，切分应跟着内容走。"""

    lines = [f"第【1】集：测试\n【本集时长】：{total}秒\n【本集断章卡点】：卡点\n"]
    for index, count in enumerate(beats_per_block):
        lines.append(f"\n[场景]：场景{index + 1}\n[人物]：林辰\n")
        lines.append("△ 林辰推开门。\n")
        lines.extend(f"林辰：第{beat + 1}句台词。\n" for beat in range(max(0, count - 1)))
    return "".join(lines)


def _durations(beats_per_block: list[int], total: int = 120) -> list[int]:
    script = convert_to_drama_script(
        parse_short_drama_episode(_md(beats_per_block, total)),
        1,
        scene_window=STORY_SCENE_WINDOW_SECONDS,
    )
    return [scene["duration_seconds"] for scene in script["scenes"]]


def test_story_window_is_12_to_15_seconds() -> None:
    assert (MIN_S, MAX_S) == (12, 15)


def test_each_scene_stays_inside_the_window() -> None:
    # 12 个场景块、每块 3 拍：整集 36 拍 120 秒，每拍约 3.3 秒 → 每幕 4 拍左右 ≈ 13 秒。
    durations = _durations([3] * 12)
    assert len(durations) >= 6
    for value in durations:
        assert MIN_S <= value <= MAX_S


def test_long_blocks_are_split_instead_of_one_huge_scene() -> None:
    # 一块 30 拍 ≈ 100 秒：必须切成多幕，不能落成单幕 100 秒。
    durations = _durations([30])
    assert len(durations) >= 6
    for value in durations:
        assert MIN_S <= value <= MAX_S


def test_short_blocks_are_merged_instead_of_fragmenting() -> None:
    # 12 个各 1 拍的碎块：每块单独成幕只有 10 秒，须跨块合并进窗口。
    durations = _durations([1] * 12)
    assert len(durations) < 12
    for value in durations:
        assert MIN_S <= value <= MAX_S


def test_total_stays_close_to_declared_duration() -> None:
    durations = _durations([3] * 12, total=120)
    assert abs(sum(durations) - 120) <= len(durations)


def test_a_single_beat_cannot_be_split_and_keeps_the_window() -> None:
    # 全集只有一拍：劈断一个拍比超出窗口更糟，因此这一幕被钳在上限，差额由回执报出。
    assert _durations([1], total=120) == [MAX_S]


def test_no_content_still_yields_one_scene() -> None:
    script = convert_to_drama_script(
        parse_short_drama_episode("第【1】集：空集\n【本集时长】：90秒\n"),
        1,
        scene_window=STORY_SCENE_WINDOW_SECONDS,
    )
    assert len(script["scenes"]) == 1


def test_no_scene_is_marked_as_segment_break() -> None:
    # 末幕之后没有下一幕，标成切分点只是哨兵。
    script = convert_to_drama_script(
        parse_short_drama_episode(_md([3] * 5, total=120)),
        1,
        scene_window=STORY_SCENE_WINDOW_SECONDS,
    )
    assert [scene["segment_break"] for scene in script["scenes"]] == [False] * len(script["scenes"])


def test_episode_forms_one_grid_group_instead_of_trailing_single() -> None:
    # 整集不因末幕被切成「13 + 1」：尾组只有一格要多出一张联合图，断章还丢了首帧参考。
    script = convert_to_drama_script(
        parse_short_drama_episode(_md([3] * 5, total=120)),
        1,
        scene_window=STORY_SCENE_WINDOW_SECONDS,
    )
    groups = group_scenes_by_segment_break(script["scenes"], "scene_id")
    assert len(groups) == 1
    assert len(groups[0]) == len(script["scenes"])


def test_legacy_tier_path_is_untouched() -> None:
    # 旧口径（给 supported_durations）仍按档位吸附，不进剧情窗口。
    script = convert_to_drama_script(
        parse_short_drama_episode(_md([3] * 6, total=120)),
        1,
        supported_durations=[8],
    )
    assert [scene["duration_seconds"] for scene in script["scenes"]] == [8] * len(script["scenes"])
