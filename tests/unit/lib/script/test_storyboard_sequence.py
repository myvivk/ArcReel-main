from pathlib import Path

import pytest

from lib.script.storyboard_sequence import (
    build_storyboard_dependency_plan,
    get_storyboard_items,
    resolve_previous_storyboard_path,
    resolve_storyboard_video_inputs,
    resolve_storyboard_video_slots,
)


class TestStoryboardSequence:
    def test_resolve_storyboard_video_inputs_uses_grid_start_and_end_cells(self, tmp_path: Path):
        project_path = tmp_path / "demo"
        storyboard_dir = project_path / "storyboards"
        storyboard_dir.mkdir(parents=True)
        cells = []
        for index in range(9):
            path = storyboard_dir / f"grid-{index}.png"
            path.write_bytes(b"png")
            cells.append(f"storyboards/grid-{index}.png")

        start, end = resolve_storyboard_video_inputs(
            project_path=project_path,
            resource_id="E1S01",
            item={
                "generated_assets": {
                    "storyboard_image": cells[0],
                    "shot_grid": {
                        "cell_paths": cells,
                        "start_cell": 0,
                        "end_cell": 8,
                        "video_reference_mode": "start_end_cells",
                    },
                }
            },
        )

        assert start == storyboard_dir / "grid-0.png"
        assert end == storyboard_dir / "grid-8.png"

    def test_resolve_previous_storyboard_path_continues_across_segment_break(self, tmp_path: Path):
        project_path = tmp_path / "demo"
        (project_path / "end_frames").mkdir(parents=True)
        previous_path = project_path / "end_frames" / "scene_E1S01.png"
        previous_path.write_bytes(b"png")
        second_path = project_path / "end_frames" / "scene_E1S02.png"
        second_path.write_bytes(b"second")

        items = [
            {
                "segment_id": "E1S01",
                "segment_break": False,
                "end_frame_image": "end_frames/scene_E1S01.png",
            },
            {"segment_id": "E1S02", "segment_break": False, "end_frame_image": "end_frames/scene_E1S02.png"},
            {"segment_id": "E1S03", "segment_break": True},
        ]

        assert resolve_previous_storyboard_path(project_path, items, "segment_id", "E1S01") is None
        assert resolve_previous_storyboard_path(project_path, items, "segment_id", "E1S02") == previous_path
        assert resolve_previous_storyboard_path(project_path, items, "segment_id", "E1S03") == second_path

    def test_resolve_previous_storyboard_path_does_not_backtrack(self, tmp_path: Path):
        project_path = tmp_path / "demo"
        (project_path / "storyboards").mkdir(parents=True)
        (project_path / "storyboards" / "scene_E1S01.png").write_bytes(b"png")

        items = [
            {
                "segment_id": "E1S01",
                "segment_break": False,
                "generated_assets": {"storyboard_image": "storyboards/scene_E1S01.png"},
            },
            {
                "segment_id": "E1S02",
                "segment_break": False,
                "generated_assets": {"storyboard_image": "storyboards/scene_E1S02.png"},
            },
            {"segment_id": "E1S03", "segment_break": False},
        ]

        # E1S02 的登记指针指向尚未落盘的文件：E1S03 只看紧邻的前一项，不回溯到 E1S01。
        assert resolve_previous_storyboard_path(project_path, items, "segment_id", "E1S03") is None

    def test_previous_storyboard_requires_an_explicit_binding(self, tmp_path: Path):
        project_path = tmp_path / "demo"
        (project_path / "storyboards").mkdir(parents=True)
        previous_path = project_path / "storyboards" / "scene_E1S01.png"
        previous_path.write_bytes(b"residue")
        items = [
            {"segment_id": "E1S01", "generated_assets": {}},
            {"segment_id": "E1S02", "generated_assets": {}},
        ]

        # 磁盘上有同名残留文件，但没有登记指针就不认。
        assert resolve_previous_storyboard_path(project_path, items, "segment_id", "E1S02") is None

        items[0]["generated_assets"] = {"storyboard_image": "storyboards/scene_E1S01.png"}
        assert resolve_previous_storyboard_path(project_path, items, "segment_id", "E1S02") is None
        items[0]["generated_assets"]["shot_grid"] = {"cell_paths": ["storyboards/scene_E1S01.png"]}
        assert resolve_previous_storyboard_path(project_path, items, "segment_id", "E1S02") == previous_path

    @pytest.mark.parametrize("count", [0, 1, 4, 9])
    @pytest.mark.parametrize("previous", [False, True])
    def test_slots_assign_every_grid_cell_once(self, tmp_path, count, previous):
        storyboards = tmp_path / "storyboards"
        storyboards.mkdir()
        paths = [f"storyboards/K{index}.png" for index in range(count)]
        for path in paths or ["storyboards/opening.png"]:
            (tmp_path / path).write_bytes(path.encode())
        item = {"scene_id": "current", "generated_assets": {"storyboard_image": "storyboards/opening.png"}}
        if paths:
            item["generated_assets"]["shot_grid"] = {"cell_paths": paths}
        items = [item]
        if previous:
            (tmp_path / "videos").mkdir()
            (tmp_path / "videos/prior.mp4").write_bytes(b"video-source")
            items.insert(0, {"scene_id": "prior", "generated_assets": {"video_clip": "videos/prior.mp4"}})
        slots = resolve_storyboard_video_slots(
            project_path=tmp_path,
            items=items,
            id_field="scene_id",
            resource_id="current",
        )
        assert slots.cell_count == count
        assert slots.continues_previous == previous
        assert len({frame.path for _, frame in slots.frames}) == len(slots.frames)
        assert [
            frame.keyframe_index
            for _, frame in slots.frames
            if frame.resource_id == "current" and frame.keyframe_index is not None
        ] == list(range(count))
        assert len(slots.references) == max(0, count - (1 if previous else 2))
        assert (slots.end is not None) == (count > (0 if previous else 1))
        if previous:
            assert slots.start.kind == "video_last_frame"

    def test_continuation_priority_does_not_write_user_snapshot(self, tmp_path):
        (tmp_path / "videos").mkdir()
        (tmp_path / "end_frames").mkdir()
        (tmp_path / "storyboards").mkdir()
        video = tmp_path / "videos/prior.mp4"
        snapshot = tmp_path / "end_frames/scene_prior.png"
        cell = tmp_path / "storyboards/last.png"
        video.write_bytes(b"video")
        snapshot.write_bytes(b"chosen-snapshot")
        cell.write_bytes(b"cell")
        prior = {
            "scene_id": "prior",
            "end_frame_image": "end_frames/scene_prior.png",
            "generated_assets": {
                "video_clip": "videos/prior.mp4",
                "shot_grid": {"cell_paths": ["storyboards/last.png"]},
            },
        }
        items = [prior, {"scene_id": "current"}]
        assert resolve_previous_storyboard_path(tmp_path, items, "scene_id", "current") == video
        video.unlink()
        assert resolve_previous_storyboard_path(tmp_path, items, "scene_id", "current") == snapshot
        assert snapshot.read_bytes() == b"chosen-snapshot"
        snapshot.unlink()
        assert resolve_previous_storyboard_path(tmp_path, items, "scene_id", "current") == cell

    def test_build_storyboard_dependency_plan_groups_contiguous_ranges(self):
        items = [
            {"segment_id": "E1S01", "segment_break": False},
            {"segment_id": "E1S02", "segment_break": False},
            {"segment_id": "E1S03", "segment_break": True},
            {"segment_id": "E1S04", "segment_break": False},
            {"segment_id": "E1S05", "segment_break": False},
        ]

        plans = build_storyboard_dependency_plan(
            items,
            "segment_id",
            ["E1S01", "E1S02", "E1S03", "E1S04"],
            "episode_1.json",
        )

        assert [(plan.resource_id, plan.dependency_resource_id, plan.dependency_index) for plan in plans] == [
            ("E1S01", None, 0),
            ("E1S02", "E1S01", 1),
            ("E1S03", None, 0),
            ("E1S04", "E1S03", 1),
        ]
        assert plans[0].dependency_group == plans[1].dependency_group
        assert plans[2].dependency_group == plans[3].dependency_group
        assert plans[0].dependency_group != plans[2].dependency_group

    def test_build_storyboard_dependency_plan_starts_new_group_when_selection_has_gap(self):
        items = [
            {"scene_id": "E1S01", "segment_break": False},
            {"scene_id": "E1S02", "segment_break": False},
            {"scene_id": "E1S03", "segment_break": False},
            {"scene_id": "E1S04", "segment_break": False},
        ]

        plans = build_storyboard_dependency_plan(
            items,
            "scene_id",
            ["E1S01", "E1S03", "E1S04"],
            "episode_1.json",
        )

        assert [(plan.resource_id, plan.dependency_resource_id) for plan in plans] == [
            ("E1S01", None),
            ("E1S03", None),
            ("E1S04", "E1S03"),
        ]


class TestAdStoryboardItems:
    def test_ad_script_resolves_shots_with_chars_field(self):
        from lib.script.storyboard_sequence import get_storyboard_items

        script = {
            "content_mode": "ad",
            "shots": [{"shot_id": "E1S01", "characters_in_shot": ["主播"]}],
        }
        items, id_field, char_field, scenes_field, props_field = get_storyboard_items(script)
        assert id_field == "shot_id"
        assert char_field == "characters_in_shot"
        assert (scenes_field, props_field) == ("scenes", "props")
        assert items[0]["shot_id"] == "E1S01"


# 骨架种类 → 触发该骨架的最小剧本（取证解析逐种覆盖，含 video_units 短路）。
_SCRIPT_BY_KIND = {
    "segments": {"content_mode": "narration", "segments": [{"segment_id": "E1S01"}]},
    "scenes": {"content_mode": "drama", "scenes": [{"scene_id": "E1S01"}]},
    "shots": {"content_mode": "ad", "shots": [{"shot_id": "E1S01"}]},
    "video_units": {"video_units": [{"unit_id": "E1U01"}]},
}


class TestStoryboardSkeletonExhaustiveness:
    """穷尽性断言：get_storyboard_items 的 id_field / char_field 覆盖 SKELETONS 全部键。

    id_field / char_field 直接查 SKELETONS，第五种骨架加入时 _SCRIPT_BY_KIND 未登记即报红；
    原按模式硬编码的 narration/drama 映射断言收敛到此。
    """

    @pytest.mark.parametrize("kind", list(_SCRIPT_BY_KIND))
    def test_get_storyboard_items_fields_match_registry(self, kind):
        from lib.script.script_skeleton import SKELETONS

        # 遍历 SKELETONS 全键：新增第五种骨架而 _SCRIPT_BY_KIND 未登记即 KeyError 报红。
        assert set(_SCRIPT_BY_KIND) == set(SKELETONS)

        _items, id_field, char_field, scenes_field, props_field = get_storyboard_items(_SCRIPT_BY_KIND[kind])
        assert id_field == SKELETONS[kind].id_field
        assert char_field == SKELETONS[kind].chars_field
        # scenes / props 资产键为项目级常量，不随骨架变。
        assert (scenes_field, props_field) == ("scenes", "props")


class TestMismatchedScriptStaysEditable:
    """存量失配剧本（分镜图生视频项目下的 video_units 骨架）：生成被拒，编辑/查看仍可用。"""

    def test_storyboard_items_are_empty_without_raising(self):
        from lib.script.storyboard_sequence import get_storyboard_items

        script = {"content_mode": "narration", "video_units": [{"unit_id": "E1U01"}]}
        items, id_field, char_field, _scenes, _props = get_storyboard_items(script)
        assert items == []
        assert (id_field, char_field) == ("unit_id", None)

    def test_edit_core_still_reaches_the_units(self):
        from lib.script.script_editor import resolve_items

        script = {"content_mode": "narration", "video_units": [{"unit_id": "E1U01"}]}
        items, id_field, kind = resolve_items(script)
        assert kind == "video_units"
        assert id_field == "unit_id"
        assert [item["unit_id"] for item in items] == ["E1U01"]


class TestCorruptScriptEntries:
    """磁盘剧本 JSON 的条目数组只保证外层是 list，元素形状由访问处判定。"""

    def test_find_storyboard_item_skips_non_mapping_entries(self):
        from lib.script.storyboard_sequence import find_storyboard_item

        items = ["E1S01", None, {"segment_id": "E1S02"}]

        assert find_storyboard_item(items, "segment_id", "E1S02") == ({"segment_id": "E1S02"}, 2)
        assert find_storyboard_item(items, "segment_id", "E1S01") is None

    def test_resolve_previous_storyboard_path_skips_non_mapping_predecessor(self, tmp_path: Path):
        project_path = tmp_path / "demo"
        (project_path / "storyboards").mkdir(parents=True)

        items = ["broken", {"segment_id": "E1S02", "segment_break": False}]

        assert resolve_previous_storyboard_path(project_path, items, "segment_id", "E1S02") is None
