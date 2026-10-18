"""视频提示词的逐段时间段：``0-2秒：…`` 的切分与回写（11.8）。"""

from __future__ import annotations

from lib.script.grid.keyframe_parser import (
    ActionSegment,
    join_action_segments,
    split_action_segments,
)


class TestSplitActionSegments:
    def test_segments_follow_the_markers_and_the_duration(self) -> None:
        action = "0-2秒：手腕先沉后拧。\n2-4秒：肩背带起上身。\n4-6秒：右腿蹬直。\n6-8秒：泥浆砸落。"

        segments = split_action_segments(action, 8, 4)

        assert segments == [
            ActionSegment(start=0, end=2, text="手腕先沉后拧。"),
            ActionSegment(start=2, end=4, text="肩背带起上身。"),
            ActionSegment(start=4, end=6, text="右腿蹬直。"),
            ActionSegment(start=6, end=8, text="泥浆砸落。"),
        ]

    def test_continuation_lines_stay_in_their_segment(self) -> None:
        action = "0-3秒：手腕先沉后拧，\n刃口在砖缝里压实。\n3-6秒：肩背带起上身。"

        segments = split_action_segments(action, 6, 2)

        assert [segment.text for segment in segments or []] == [
            "手腕先沉后拧，\n刃口在砖缝里压实。",
            "肩背带起上身。",
        ]

    def test_more_segments_than_cells_merge_into_the_last_one(self) -> None:
        action = "0-1秒：甲。\n1-2秒：乙。\n2-3秒：丙。"

        segments = split_action_segments(action, 4, 2)

        assert segments is not None
        assert len(segments) == 2
        assert segments[1].text == "乙。\n丙。"
        assert (segments[1].start, segments[1].end) == (2, 4)

    def test_fewer_segments_than_cells_leave_empty_tail_segments(self) -> None:
        segments = split_action_segments("0-4秒：只有一段。", 8, 3)

        assert segments is not None
        assert [segment.text for segment in segments] == ["只有一段。", "", ""]

    def test_free_text_falls_back(self) -> None:
        assert split_action_segments("铁镐入缝，手腕先沉后拧。", 8, 4) is None
        assert split_action_segments("", 8, 4) is None
        assert split_action_segments("0-2秒：甲", 8, 0) is None

    def test_half_width_colon_and_spaces_are_accepted(self) -> None:
        segments = split_action_segments("0 - 2秒: 甲。\n2-4秒：乙。", 4, 2)

        assert segments is not None
        assert [segment.text for segment in segments] == ["甲。", "乙。"]


class TestJoinActionSegments:
    def test_windows_are_recomputed_from_the_duration(self) -> None:
        segments = [
            ActionSegment(start=0, end=2, text="甲。"),
            ActionSegment(start=2, end=4, text="乙。"),
        ]

        assert join_action_segments(segments, 6) == "0-3秒：甲。\n3-6秒：乙。"

    def test_round_trip_keeps_the_text(self) -> None:
        action = "0-2秒：手腕先沉后拧。\n2-4秒：肩背带起上身。"
        segments = split_action_segments(action, 4, 2)

        assert segments is not None
        assert join_action_segments(segments, 4) == action
