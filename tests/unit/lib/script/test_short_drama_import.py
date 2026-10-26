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
    resolve_scene_assets,
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
    assert len(parsed.blocks) == 2  # 不再保留开头的空块
    assert parsed.blocks[0].prop_appearances == {"铁门": "锈迹斑斑，门轴下垂", "断椅": "木刺外翻，缺一条腿"}
    assert referenced_asset_descriptions(parsed) == {
        "character": {"林辰": "三十岁上下，寸头"},
        "scene": {"仓库": "水泥地，铁门单侧顶光"},
        "prop": {"铁门": "锈迹斑斑，门轴下垂", "断椅": "木刺外翻，缺一条腿"},
    }
    assert [beat["beat_id"] for beat in beat_catalog(parsed)] == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# 全集场景清单格式 + per-scene 资产解析
# ---------------------------------------------------------------------------

LIST_STYLE_MARKDOWN = """第【1】集：双景
【本集时长】：30 秒
[场景]：内景・绣楼・夜｜暖红烛火，绣榻与屏风
[场景]：外景・前厅・夜｜冷夜长阶，朱门与公案
[人物]：沈昭（主角｜藕荷色裙）、苏嬷嬷（嬷嬷｜灰布衫）、柳敬堂（老爷｜玄色官袍）、裴凛（护卫｜黑衣）
[道具]：懿旨｜明黄绢底
[道具]：白绫｜素白绸缎
[道具]：旧红绳｜编绳发黑
△ 沈昭躺在绣榻上，攥紧白绫。
沈昭：门外是谁？
△ 苏嬷嬷推门进入，双手捧着懿旨。
苏嬷嬷：懿旨到了。
△ 沈昭走进前厅。
柳敬堂：跪下接旨。
△ 柳敬堂举起懿旨。
"""

_LIST_GROUPS = [[1, 2, 3], [4, 5, 6, 7]]
_LIST_PROMPTS = [
    (
        "【镜头1｜中景】平视机位，固定。沈昭@[沈昭]躺在内景・绣楼・夜@[内景・绣楼・夜]的绣榻上，"
        "右手攥紧白绫@[白绫]，眉心紧蹙、目光警觉；苏嬷嬷@[苏嬷嬷]推门而入，手中懿旨@[懿旨]映着暖红烛火。"
    ),
    (
        "【镜头1｜中景】平视机位，固定。沈昭@[沈昭]走进外景・前厅・夜@[外景・前厅・夜]，"
        "肩背挺直、目光冷静；柳敬堂@[柳敬堂]立在公案后举起懿旨@[懿旨]，冷夜长阶上映着灯火。"
    ),
]
_LIST_REGISTERED = {
    "character": {"沈昭", "苏嬷嬷", "柳敬堂", "裴凛"},
    "scene": {"内景・绣楼・夜", "外景・前厅・夜"},
    "prop": {"懿旨", "白绫", "旧红绳"},
}


def test_list_style_scene_declarations_share_one_block_instead_of_swallowing_beats():
    """两个 [场景] 连写是全集清单：不得各建空块，更不得把 57 个 beat 全归到后一个块。"""
    parsed = parse_short_drama_episode(LIST_STYLE_MARKDOWN)

    assert len(parsed.blocks) == 1
    assert parsed.blocks[0].locations == ["内景・绣楼・夜", "外景・前厅・夜"]
    assert [beat["block_id"] for beat in beat_catalog(parsed)] == [0] * 7


def test_resolver_uses_prompt_scene_token_and_actual_beat_assets():
    parsed = parse_short_drama_episode(LIST_STYLE_MARKDOWN)
    resolved = resolve_scene_assets(
        parsed,
        _LIST_GROUPS,
        _LIST_PROMPTS,
        registered=_LIST_REGISTERED,
    )

    assert resolved[0].scenes == ("内景・绣楼・夜",)
    assert resolved[0].characters_in_scene == ("沈昭", "苏嬷嬷")
    assert resolved[0].props == ("懿旨", "白绫")
    assert resolved[1].scenes == ("外景・前厅・夜",)
    assert resolved[1].characters_in_scene == ("柳敬堂", "沈昭", "苏嬷嬷")
    assert resolved[1].props == ("懿旨",)
    # 裴凛只在 [人物] 声明、没在任何 beat 出场：不进入本集任一幕
    assert all("裴凛" not in scene.characters_in_scene for scene in resolved)


def test_convert_with_scene_assets_writes_the_gate_allowlist_into_the_script():
    parsed = parse_short_drama_episode(LIST_STYLE_MARKDOWN)
    resolved = resolve_scene_assets(parsed, _LIST_GROUPS, _LIST_PROMPTS, registered=_LIST_REGISTERED)
    script = convert_planned_scenes(
        parsed,
        1,
        _LIST_GROUPS,
        [13, 14],
        ["沈昭在绣楼惊醒。", "沈昭走进前厅受审。"],
        _LIST_PROMPTS,
        scene_assets=resolved,
    )

    first, second = script["scenes"]
    assert first["scenes"] == ["内景・绣楼・夜"]
    assert first["characters_in_scene"] == ["沈昭", "苏嬷嬷"]
    assert first["props"] == ["懿旨", "白绫"]
    assert "内景・绣楼・夜@[内景・绣楼・夜]" in first["image_prompt"]
    assert second["scenes"] == ["外景・前厅・夜"]
    assert second["characters_in_scene"] == ["柳敬堂", "沈昭", "苏嬷嬷"]
    assert "内景・绣楼・夜" not in second["scenes"]


def test_resolver_rejects_plan_scene_that_contradicts_prompt_token():
    parsed = parse_short_drama_episode(LIST_STYLE_MARKDOWN)

    with pytest.raises(ValueError, match="不一致"):
        resolve_scene_assets(
            parsed,
            _LIST_GROUPS,
            _LIST_PROMPTS,
            registered=_LIST_REGISTERED,
            plan_scenes=[{"scenes": ["外景・前厅・夜"]}, {"scenes": ["外景・前厅・夜"]}],
        )


def test_resolver_rejects_unregistered_plan_scene():
    parsed = parse_short_drama_episode(LIST_STYLE_MARKDOWN)

    with pytest.raises(ValueError, match="未登记场景"):
        resolve_scene_assets(
            parsed,
            _LIST_GROUPS,
            _LIST_PROMPTS,
            registered=_LIST_REGISTERED,
            plan_scenes=[{"scenes": ["不存在的场景"]}, {"scenes": ["外景・前厅・夜"]}],
        )


def test_resolver_reports_ambiguous_scene_without_token_or_plan():
    parsed = parse_short_drama_episode(MARKDOWN)
    prompts = ["【镜头1】近景，平视，固定。林辰@[林辰]攥住门把手，屏息。"]

    with pytest.raises(ValueError, match="无法判定本幕场景"):
        resolve_scene_assets(
            parsed,
            [[1, 2, 3, 4]],
            prompts,
            registered={
                "character": {"林辰", "陈峰"},
                "scene": {"仓库", "走廊"},
                "prop": {"铁门", "断椅"},
            },
        )


def test_resolver_falls_back_to_the_only_scene_of_a_single_scene_source():
    parsed = parse_short_drama_episode(
        "第【1】集：单景\n【本集时长】：15 秒\n[场景]：仓库｜水泥地\n[人物]：林辰（主角｜寸头）\n△ 林辰推门。\n"
    )

    resolved = resolve_scene_assets(
        parsed,
        [[1]],
        ["【镜头1｜中景】平视机位，固定。林辰@[林辰]推门，眉峰紧拧，门外冷光切入仓库。"],
        registered={"character": {"林辰"}, "scene": {"仓库"}, "prop": set()},
    )

    assert resolved[0].scenes == ("仓库",)
    assert resolved[0].characters_in_scene == ("林辰",)
