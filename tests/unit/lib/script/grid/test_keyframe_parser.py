"""分镜提示词 scene 的镜头序列解析。"""

from __future__ import annotations

import pytest

from lib.script.grid.keyframe_parser import (
    ShotFrame,
    parse_scene_shots,
    shot_scene_text,
    strip_scene_dialogue_lines,
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

    def test_legacy_dialogue_lines_are_stripped_with_diagnostics(self) -> None:
        scene = (
            "台词：画外声：“沈氏即刻自尽。”\n"
            "【镜头1｜远景】小明站在院门口。\n"
            "台词：小明：“我回来了。”\n"
            "【镜头2｜中景】他抬起头。"
        )
        warnings: list[str] = []

        parsed = parse_scene_shots(scene, warnings=warnings)

        assert parsed.opening == ""
        assert [frame.text for frame in parsed.shots] == ["小明站在院门口。", "他抬起头。"]
        assert parsed.stripped_dialogue_lines == (
            "台词：画外声：“沈氏即刻自尽。”",
            "台词：小明：“我回来了。”",
        )
        assert warnings
        assert "2 行台词" in warnings[0]
        assert "台词" not in "".join(frame.text for frame in parsed.shots)

    def test_clean_scene_does_not_report_stripped_lines(self) -> None:
        warnings: list[str] = []
        parsed = parse_scene_shots("【镜头1｜远景】小明站在院门口。", warnings=warnings)
        assert parsed.stripped_dialogue_lines == ()
        assert warnings == []

    def test_strip_scene_dialogue_lines_keeps_the_rest_verbatim(self) -> None:
        clean, stripped = strip_scene_dialogue_lines(
            "【镜头1｜远景】小明站在院门口。\n对白：小明：我回来了。\n【镜头2】他抬头。"
        )
        assert stripped == ["对白：小明：我回来了。"]
        assert clean == "【镜头1｜远景】小明站在院门口。\n【镜头2】他抬头。"


class TestShotSceneText:
    def test_structured_and_plain_forms_share_one_exit(self) -> None:
        assert shot_scene_text({"image_prompt": {"scene": "村口黄昏"}}) == "村口黄昏"
        assert shot_scene_text({"image_prompt": "村口黄昏"}) == "村口黄昏"

    def test_broken_forms_read_as_empty(self) -> None:
        assert shot_scene_text({"image_prompt": None}) == ""
        assert shot_scene_text({"image_prompt": {"scene": 3}}) == ""
        assert shot_scene_text({}) == ""
