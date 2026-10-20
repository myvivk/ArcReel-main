"""本集引用资产的唯一计算点：抽取输入 = 剧本（工厂源稿 / 正式脚本）+ 故事规划。"""

from __future__ import annotations

from lib.project.episode_asset_references import episode_referenced_assets

SOURCE_MARKDOWN = "第 1 集\n[人物]：陈峰｜宽脸浓眉\n[场景]：城郊仓库｜水泥地\n[道具]：玉佩\n"


def _project() -> dict:
    return {
        "characters": {"陈峰": {"description": "宽脸浓眉"}},
        "scenes": {"城郊仓库": {"description": "水泥地"}},
        "props": {},
    }


class TestFactorySourceMarkers:
    def test_marker_names_count_the_registered_ones(self):
        # 只有工厂源稿、还没有正式脚本时，标记行点名的已登记资产照样是本集引用。
        referenced = episode_referenced_assets(_project(), None, source_markdown=SOURCE_MARKDOWN)

        assert {(asset.asset_type, asset.name) for asset in referenced} == {
            ("character", "陈峰"),
            ("scene", "城郊仓库"),
        }

    def test_an_unregistered_marker_name_is_not_a_reference(self):
        project = _project()
        project["props"] = {"玉佩": {"description": ""}}
        referenced = episode_referenced_assets(project, None, source_markdown=SOURCE_MARKDOWN)

        assert ("prop", "玉佩") in {(asset.asset_type, asset.name) for asset in referenced}

    def test_no_source_and_no_script_yields_nothing(self):
        assert episode_referenced_assets(_project(), None) == frozenset()


class TestStoryPlanNames:
    def test_plan_defined_names_count_as_referenced(self):
        project = _project()
        project["props"] = {"玉佩": {"description": "祖传玉佩"}}
        referenced = episode_referenced_assets(
            project,
            None,
            story_plan_names={"character": ["陈峰"], "prop": ["玉佩"]},
        )

        assert {(asset.asset_type, asset.name) for asset in referenced} == {
            ("character", "陈峰"),
            ("prop", "玉佩"),
        }


class TestFormalScriptStillCounts:
    def test_reference_arrays_are_still_the_main_source(self):
        script = {"scenes": [{"characters_in_scene": ["陈峰"], "scenes": ["城郊仓库"], "props": []}]}

        referenced = episode_referenced_assets(_project(), script)

        assert {(asset.asset_type, asset.name) for asset in referenced} == {
            ("character", "陈峰"),
            ("scene", "城郊仓库"),
        }
