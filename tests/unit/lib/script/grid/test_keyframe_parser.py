"""scene 与宫格镜头提示词的镜头序列解析、写回与指纹。"""

from __future__ import annotations

import pytest

from lib.script.grid.keyframe_parser import (
    ShotFrame,
    format_grid_prompt,
    parse_grid_prompt_cells,
    parse_scene_shots,
    scene_revision_of,
    shot_scene_text,
)


class TestParseSceneShots:
    def test_opening_and_shots_come_apart(self) -> None:
        parsed = parse_scene_shots("村口黄昏，风住了。【镜头1｜远景】小明站在院门口。【镜头2】他抬起头")

        assert parsed.opening == "村口黄昏，风住了。"
        assert parsed.shots == (
            ShotFrame(number=1, shot_size="远景", text="小明站在院门口。"),
            ShotFrame(number=2, shot_size="", text="他抬起头"),
        )

    def test_numbers_renumber_by_appearance_and_duplicates_keep_the_first(self) -> None:
        parsed = parse_scene_shots("【镜头7｜全景】第一段。【镜头2｜中景】第二段。【镜头7｜近景】重复段")

        assert [frame.number for frame in parsed.shots] == [1, 2]
        assert [frame.text for frame in parsed.shots] == ["第一段。", "第二段。"]

    def test_empty_and_legacy_single_frame_forms_are_refused(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            parse_scene_shots("   ")
        with pytest.raises(ValueError, match="镜头序列"):
            parse_scene_shots("小明站在院门口，只有一句话")

    def test_marks_without_any_body_are_refused(self) -> None:
        with pytest.raises(ValueError, match="没有可用的画面描述"):
            parse_scene_shots("【镜头1｜远景】\n【镜头2｜中景】")


class TestParseGridPromptCells:
    def test_marked_lines_and_continuations(self) -> None:
        prompt = "第1格（远景）：小明站在院门口。\n补充一句环境。\n第2格：他抬起头"

        assert parse_grid_prompt_cells(prompt) == [
            ShotFrame(number=1, shot_size="远景", text="小明站在院门口。\n补充一句环境。"),
            ShotFrame(number=2, shot_size="", text="他抬起头"),
        ]

    def test_plain_lines_become_cells_without_a_shot_size(self) -> None:
        assert parse_grid_prompt_cells("第一段\n第二段") == [
            ShotFrame(number=1, shot_size="", text="第一段"),
            ShotFrame(number=2, shot_size="", text="第二段"),
        ]

    def test_empty_prompt_and_empty_cells_drop_out(self) -> None:
        assert parse_grid_prompt_cells("") == []
        assert parse_grid_prompt_cells("第1格（远景）：\n第2格：有内容") == [
            ShotFrame(number=1, shot_size="", text="有内容")
        ]

    def test_numbers_renumber_by_appearance(self) -> None:
        assert parse_grid_prompt_cells("第4格：甲\n第9格：乙") == [
            ShotFrame(number=1, shot_size="", text="甲"),
            ShotFrame(number=2, shot_size="", text="乙"),
        ]


class TestFormatGridPrompt:
    def test_round_trips_through_the_parser(self) -> None:
        frames = [
            ShotFrame(number=1, shot_size="远景", text="小明站在院门口。"),
            ShotFrame(number=2, shot_size="", text="他抬起头"),
        ]

        text = format_grid_prompt(frames)

        assert text == "第1格（远景）：小明站在院门口。\n第2格：他抬起头"
        assert parse_grid_prompt_cells(text) == frames

    def test_empty_bodies_are_dropped_and_the_rest_renumbers(self) -> None:
        frames = [
            ShotFrame(number=1, shot_size="远景", text="  "),
            ShotFrame(number=2, shot_size="中景", text="小明抬起头"),
        ]

        assert format_grid_prompt(frames) == "第1格（中景）：小明抬起头"
        assert parse_grid_prompt_cells(format_grid_prompt(frames)) == [
            ShotFrame(number=1, shot_size="中景", text="小明抬起头")
        ]

    def test_no_frames_is_empty(self) -> None:
        assert format_grid_prompt([]) == ""


class TestShotSceneText:
    def test_structured_and_plain_forms_share_one_exit(self) -> None:
        assert shot_scene_text({"image_prompt": {"scene": "村口黄昏"}}) == "村口黄昏"
        assert shot_scene_text({"image_prompt": "村口黄昏"}) == "村口黄昏"

    def test_broken_forms_read_as_empty(self) -> None:
        assert shot_scene_text({"image_prompt": None}) == ""
        assert shot_scene_text({"image_prompt": {"scene": 3}}) == ""
        assert shot_scene_text({}) == ""


class TestSceneRevision:
    def test_same_text_same_revision_and_a_change_moves_it(self) -> None:
        assert scene_revision_of("村口黄昏") == scene_revision_of("村口黄昏")
        assert scene_revision_of("村口黄昏") != scene_revision_of("村口黄昏。")
        assert len(scene_revision_of("村口黄昏")) == 12
