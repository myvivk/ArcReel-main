"""K 引用边界：正文里只能引用真实存在的关键帧（D-17）。"""

from __future__ import annotations

import pytest

from lib.script.grid.keyframe_parser import keyframe_reference_problems


class TestKeyframeReferenceProblems:
    @pytest.mark.parametrize("text", ["从 K0 起手", "K3 收势", "K0→K1→K2→K3"])
    def test_references_inside_the_grid_are_fine(self, text: str) -> None:
        assert keyframe_reference_problems(text, 4) == []

    def test_out_of_range_reference_is_reported(self) -> None:
        assert keyframe_reference_problems("K4 的余势", 4) == ["K4"]

    def test_lower_case_is_the_same_reference(self) -> None:
        assert keyframe_reference_problems("k4 的余势", 4) == ["K4"]

    def test_multi_digit_reference_is_reported(self) -> None:
        assert keyframe_reference_problems("K12 与 K9", 9) == ["K12", "K9"]

    def test_duplicates_collapse_in_appearance_order(self) -> None:
        assert keyframe_reference_problems("K6 先，K4 后，K6 再", 4) == ["K6", "K4"]

    def test_text_without_references_or_digits_is_fine(self) -> None:
        assert keyframe_reference_problems("铁镐入缝，手腕先沉后拧", 4) == []
        assert keyframe_reference_problems("K 后面没有数字", 4) == []

    def test_no_cells_means_no_check(self) -> None:
        assert keyframe_reference_problems("K4", 0) == []
