"""故事规划的统一读取：四类文档的定位、按集收窄与资产名抽取。"""

from __future__ import annotations

from pathlib import Path

from server.services.story_plan_docs import (
    collect_story_plan,
    plan_episode_section,
    story_plan_asset_names,
    story_plan_documents,
)


def _project_dir(tmp_path: Path) -> Path:
    root = tmp_path / "project" / "source" / "short_drama"
    root.mkdir(parents=True, exist_ok=True)
    (root / "character-bible.md").write_text(
        "【角色】：林辰\n【外形】：三十岁上下，寸头\n【角色】：陈峰\n【外形】：宽脸浓眉\n",
        encoding="utf-8",
    )
    (root / "series-outline.md").write_text("第 1 集 城郊仓库对峙\n第 2 集 会所亮令\n", encoding="utf-8")
    (root / "continuity-ledger.md").write_text("林辰的旧伤在第 3 集复\n", encoding="utf-8")
    (root / "episode-plan.md").write_text(
        "第 1 集\n[人物]：陈峰\n[场景]：城郊仓库｜水泥地\n\n第 2 集\n[人物]：路人甲\n",
        encoding="utf-8",
    )
    return tmp_path / "project"


class TestDocumentLookup:
    def test_the_four_kinds_are_found_under_source(self, tmp_path: Path):
        found = story_plan_documents(_project_dir(tmp_path))

        assert sorted(found) == ["character_bible", "continuity_ledger", "episode_plan", "series_outline"]

    def test_a_project_without_source_has_no_plan_docs(self, tmp_path: Path):
        assert story_plan_documents(tmp_path) == {}


class TestEpisodeSection:
    def test_only_the_requested_episode_is_kept(self):
        text = "第 1 集\n阿岚出场\n\n第 2 集\n陈峰出场\n"

        assert plan_episode_section(text, 1) == "阿岚出场"
        assert plan_episode_section(text, 2) == "陈峰出场"

    def test_a_doc_without_episode_headers_is_kept_whole(self):
        text = "[人物]：阿岚｜白衣少女\n"

        assert plan_episode_section(text, 3) == text

    def test_an_episode_the_plan_does_not_cover_yields_nothing(self):
        assert plan_episode_section("第 1 集\n阿岚出场\n", 9) == ""


class TestCollectStoryPlan:
    def test_the_character_bible_section_is_the_role_own_paragraph(self, tmp_path: Path):
        items = collect_story_plan(_project_dir(tmp_path), "character", "林辰")

        joined = "\n".join(items)
        assert "人物圣经" in joined
        assert "三十岁上下，寸头" in joined
        assert "宽脸浓眉" not in joined  # 别的角色那一段不算

    def test_the_episode_plan_is_scoped_to_the_episode(self, tmp_path: Path):
        first = "\n".join(collect_story_plan(_project_dir(tmp_path), "character", "陈峰", episode=1))
        second = "\n".join(collect_story_plan(_project_dir(tmp_path), "character", "陈峰", episode=2))

        assert "陈峰" in first
        assert "路人甲" not in first
        # 分集规划按集收窄：第 2 集那节里没有陈峰，只剩项目级的人物圣经那一段。
        assert "分集规划" not in second
        assert "人物圣经" in second

    def test_the_outline_and_ledger_lines_are_collected(self, tmp_path: Path):
        scene = "\n".join(collect_story_plan(_project_dir(tmp_path), "scene", "城郊仓库"))
        character = "\n".join(collect_story_plan(_project_dir(tmp_path), "character", "林辰"))

        # 全剧大纲里提到该场景的行、连续性台账里提到该角色的行都进第二优先级块。
        assert "全剧大纲" in scene
        assert "城郊仓库对峙" in scene
        assert "连续性台账" in character


class TestStoryPlanAssetNames:
    def test_bible_roles_and_marker_lines_are_grouped_by_type(self, tmp_path: Path):
        names = story_plan_asset_names(_project_dir(tmp_path), episode=1)

        assert names["character"] == ["林辰", "陈峰"]
        assert names["scene"] == ["城郊仓库"]
        assert names["prop"] == []

    def test_another_episodes_plan_names_are_not_this_episodes(self, tmp_path: Path):
        names = story_plan_asset_names(_project_dir(tmp_path), episode=2)

        assert "路人甲" in names["character"]
        assert names["scene"] == []

    def test_a_project_without_plan_docs_yields_no_names(self, tmp_path: Path):
        assert story_plan_asset_names(tmp_path, episode=1) == {"character": [], "scene": [], "prop": []}
