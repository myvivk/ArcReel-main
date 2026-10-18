"""视频提示词的目标集合与时效（D-18 / D-20）。"""

from __future__ import annotations

from dataclasses import replace

from lib.script.grid.shot_models import ShotGrid, ShotGridCell
from lib.script.grid.video_prompt_targets import (
    grid_cells_revision,
    is_video_prompt_stale,
    plan_video_prompt_targets,
    video_prompt_basis_for,
    video_prompt_text,
)


def grid(shot_id: str = "E1S01", *, status: str = "confirmed", prompts: tuple[str, ...] = ("甲", "乙")) -> ShotGrid:
    record = ShotGrid.create(episode=1, script_file="episode_1.json", shot_id=shot_id, rows=1, cols=len(prompts))
    record.status = status
    record.cells = [ShotGridCell(index=index, prompt=text) for index, text in enumerate(prompts)]
    return record


class TestVideoPromptText:
    def test_structured_and_plain_forms_share_one_exit(self) -> None:
        assert video_prompt_text({"video_prompt": {"action": " K0 起手 "}}) == "K0 起手"
        assert video_prompt_text({"video_prompt": " K0 起手 "}) == "K0 起手"

    def test_missing_or_broken_reads_as_empty(self) -> None:
        assert video_prompt_text({}) == ""
        assert video_prompt_text({"video_prompt": None}) == ""
        assert video_prompt_text({"video_prompt": {"action": 3}}) == ""


class TestGridCellsRevision:
    def test_revision_follows_the_cell_text(self) -> None:
        first = grid()
        changed = replace(first, cells=[ShotGridCell(index=0, prompt="甲"), ShotGridCell(index=1, prompt="丙")])

        assert grid_cells_revision(first) != grid_cells_revision(changed)

    def test_revision_is_short_and_stable(self) -> None:
        revision = grid_cells_revision(grid())

        assert len(revision) == 12
        assert revision == grid_cells_revision(grid())


class TestIsVideoPromptStale:
    def test_missing_basis_is_never_stale(self) -> None:
        assert is_video_prompt_stale({"video_prompt": "K0 起手"}, grid()) is False

    def test_matching_basis_is_current(self) -> None:
        record = grid()
        item = {"generated_assets": {"video_prompt_basis": video_prompt_basis_for(record)}}

        assert is_video_prompt_stale(item, record) is False

    def test_rewritten_cells_make_the_prompt_stale(self) -> None:
        record = grid()
        item = {"generated_assets": {"video_prompt_basis": video_prompt_basis_for(record)}}
        regenerated = replace(
            record, cells=[ShotGridCell(index=0, prompt="新甲"), ShotGridCell(index=1, prompt="新乙")]
        )

        assert is_video_prompt_stale(item, regenerated) is True

    def test_another_grid_makes_the_prompt_stale(self) -> None:
        item = {"generated_assets": {"video_prompt_basis": video_prompt_basis_for(grid("E1S01"))}}

        assert is_video_prompt_stale(item, grid("E1S02")) is True

    def test_no_grid_is_not_stale(self) -> None:
        item = {"generated_assets": {"video_prompt_basis": {"grid_id": "x", "cells_revision": "y"}}}

        assert is_video_prompt_stale(item, None) is False


class TestPlanVideoPromptTargets:
    def test_targets_awaiting_and_current_are_split_by_grid_state(self) -> None:
        items = [
            {"scene_id": "E1S01", "image_prompt": {"scene": "【镜头1】甲"}, "video_prompt": {"action": ""}},
            {"scene_id": "E1S02", "video_prompt": {"action": "K0 起手"}},
            {"scene_id": "E1S03", "video_prompt": {"action": ""}},
        ]
        grids = {
            "E1S01": grid("E1S01"),
            "E1S02": grid("E1S02", status="confirmed"),
            "E1S03": grid("E1S03", status="draft"),
        }

        plan = plan_video_prompt_targets(items, id_field="scene_id", grids=grids)

        assert plan.unit_ids == ("E1S01", "E1S02", "E1S03")
        assert plan.targets == ("E1S01",)
        assert plan.awaiting_grid == ("E1S03",)
        assert plan.stale == ()

    def test_stale_prompts_are_not_targets(self) -> None:
        record = grid("E1S01")
        old_basis = video_prompt_basis_for(record)
        item = {
            "scene_id": "E1S01",
            "video_prompt": {"action": "K0 起手"},
            "generated_assets": {"video_prompt_basis": old_basis},
        }
        regenerated = replace(
            record, cells=[ShotGridCell(index=0, prompt="新甲"), ShotGridCell(index=1, prompt="新乙")]
        )

        plan = plan_video_prompt_targets([item], id_field="scene_id", grids={"E1S01": regenerated})

        assert plan.targets == ()
        assert plan.stale == ("E1S01",)

    def test_items_without_id_are_skipped(self) -> None:
        plan = plan_video_prompt_targets([{"video_prompt": ""}], id_field="scene_id", grids={})

        assert plan.unit_ids == ()
