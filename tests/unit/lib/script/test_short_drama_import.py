"""AI 节拍分组构建视觉状态，原稿台词与锚点保持不变。"""

from __future__ import annotations

import pytest

from lib.script.script_models import DramaEpisodeScript
from lib.script.short_drama_import import (
    beat_catalog,
    convert_planned_scenes,
    parse_short_drama_episode,
    referenced_asset_descriptions,
    referenced_asset_names,
    scene_beat_groups,
)

MARKDOWN = """第【1】集：道具
【本集时长】：30秒
【黄金前3秒钩子】：门外异响
【本集断章卡点】：门后的身影
[场景]：仓库｜水泥地，铁门单侧顶光
[人物]：林辰（隐忍龙王｜三十岁上下，寸头）、陈峰（嚣张富二代）
[道具]：铁门｜锈迹斑斑，门轴下垂
[道具]：断椅｜木刺外翻，缺一条腿

△ 林辰推开铁门。
林辰（压声）：门后有东西。

[场景]：走廊
[人物]：林辰（隐忍龙王｜三十岁上下，寸头）
[道具]：断椅｜木刺外翻，缺一条腿

△ 林辰踢开断椅。
旁白：脚步声停止了。
"""


def _script(groups: list[list[int]] | None = None):
    selected = groups if groups is not None else [[1, 2], [3, 4]]
    return convert_planned_scenes(
        parse_short_drama_episode(MARKDOWN),
        1,
        selected,
        [13] * len(selected),
        ["林辰攥住门把手，屏息。"] * len(selected),
        ["【镜头1】近景，平视，固定。\n林辰@[林辰]攥住门把手。"] * len(selected),
    )


def test_ai_plan_preserves_metadata_and_does_not_create_video_prompts():
    script = _script()
    DramaEpisodeScript.model_validate({key: value for key, value in script.items() if key != "short_drama_meta"})
    assert script["title"] == "道具"
    assert script["hook"] == "门后的身影"
    assert script["short_drama_meta"]["opening_hook"] == "门外异响"
    assert [scene["duration_seconds"] for scene in script["scenes"]] == [13, 13]
    assert all(scene["video_prompt"] is None and scene["pending_authoring"] for scene in script["scenes"])


def test_dialogue_and_voiceover_are_derived_from_source():
    script = _script()
    assert script["scenes"][0]["utterances"] == [{"kind": "dialogue", "speaker": "林辰", "text": "门后有东西。"}]
    assert script["scenes"][1]["utterances"] == [{"kind": "voiceover", "speaker": None, "text": "脚步声停止了。"}]
    assert script["scenes"][0]["source_text"] == "△ 林辰推开铁门。\n林辰（压声）：门后有东西。"


def test_source_anchors_can_recover_exact_ai_groups():
    script = _script()
    assert scene_beat_groups(parse_short_drama_episode(MARKDOWN), script["scenes"]) == [[1, 2], [3, 4]]
    script["scenes"][0]["source_text"] = "用户拼接的其它内容"
    with pytest.raises(ValueError, match="当前源稿不对应"):
        scene_beat_groups(parse_short_drama_episode(MARKDOWN), script["scenes"])


@pytest.mark.parametrize("groups", [[], [[1, 2]], [[1, 3], [4]], [[1, 2], [2, 3, 4]], [[2, 3, 4]], [[1, 2, 3, 5]]])
def test_missing_duplicate_reordered_or_unknown_beats_are_rejected(groups):
    with pytest.raises(ValueError, match=r"scene|beat"):
        _script(groups)


def test_ai_groups_can_cross_scene_blocks_without_losing_references():
    script = _script([[1, 2, 3, 4]])
    assert script["scenes"][0]["scenes"] == ["仓库", "走廊"]
    assert script["scenes"][0]["props"] == ["断椅", "铁门"]
    assert script["scenes"][0]["characters_in_scene"] == ["林辰", "陈峰"]
    assert referenced_asset_names(script)["prop"] == ["断椅", "铁门"]


def test_prop_references_only_attach_where_source_mentions_them():
    assert [scene["props"] for scene in _script()["scenes"]] == [["铁门"], ["断椅"]]


def test_parser_keeps_appearance_punctuation_and_catalog_order():
    parsed = parse_short_drama_episode(MARKDOWN)
    assert parsed.blocks[1].prop_appearances == {"铁门": "锈迹斑斑，门轴下垂", "断椅": "木刺外翻，缺一条腿"}
    assert referenced_asset_descriptions(parsed) == {
        "character": {"林辰": "三十岁上下，寸头"},
        "scene": {"仓库": "水泥地，铁门单侧顶光"},
        "prop": {"铁门": "锈迹斑斑，门轴下垂", "断椅": "木刺外翻，缺一条腿"},
    }
    assert [beat["beat_id"] for beat in beat_catalog(parsed)] == [1, 2, 3, 4]
