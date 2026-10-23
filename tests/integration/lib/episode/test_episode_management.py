"""集管理的服务命令：新建、调序与删除，在真实的临时项目目录上只经公开命令验证账本与盘上文件。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.episode.episode_deletion import (
    EpisodeBatchDeletionConfirmationRequired,
    EpisodeBatchDeletionResult,
    EpisodeDeletionConfirmationRequired,
    delete_episode,
    delete_episodes,
    render_episode_batch_deletion_text,
    render_episode_deletion_text,
)
from lib.episode.episode_management import (
    EpisodeManagementError,
    create_episode,
    move_episode,
)
from lib.episode.episode_sources import discover_sources, planning_start
from lib.i18n import _ as i18n_message
from lib.project.project_manager import ProjectManager

A = "第一章。少年下山。遇见老人。第二章。城里起火。人群四散。第三章。夜雨。"
CH2 = A.index("第二章")
CH3 = A.index("第三章")


def _cut(episode: int, start: int, end: int, **fields) -> dict:
    return {
        "episode": episode,
        "title": f"集{episode}",
        "script_file": f"scripts/episode_{episode}.json",
        "source_origin": "whole_source",
        "source_range": {"source_file": "source/a.txt", "start": start, "end": end},
        "ledger_status": "planned",
        **fields,
    }


def _own(episode: int) -> dict:
    return {
        "episode": episode,
        "title": f"番外{episode}",
        "script_file": f"scripts/episode_{episode}.json",
        "source_origin": "own",
    }


def _none(episode: int) -> dict:
    return {"episode": episode, "title": "", "script_file": f"scripts/episode_{episode}.json", "source_origin": "none"}


def _project_dir(tmp_path: Path, episodes: list[dict], *, high_water: int | None = None) -> Path:
    project_dir = tmp_path / "projects" / "demo"
    (project_dir / "source").mkdir(parents=True)
    (project_dir / "source" / "a.txt").write_text(A, encoding="utf-8")
    project = {
        "schema_version": 3,
        "title": "测试项目",
        "content_mode": "narration",
        "generation_mode": "storyboard",
        "style": "国漫",
        "characters": {},
        "scenes": {},
        "props": {},
        "whole_source_files": [{"source_file": "source/a.txt"}],
        "episodes": episodes,
        "episode_id_high_water": high_water
        if high_water is not None
        else max([e["episode"] for e in episodes], default=0),
    }
    (project_dir / "project.json").write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
    for entry in episodes:
        source_range = entry.get("source_range")
        if source_range:
            text = A[source_range["start"] : source_range["end"]]
            (project_dir / "source" / f"episode_{entry['episode']}.txt").write_text(text, encoding="utf-8")
        elif entry.get("source_origin") == "own":
            (project_dir / "source" / f"episode_{entry['episode']}.txt").write_text("番外原文。", encoding="utf-8")
    return project_dir


def _pm(project_dir: Path) -> ProjectManager:
    return ProjectManager.for_project_dir(project_dir)


def _load(project_dir: Path) -> dict:
    return json.loads((project_dir / "project.json").read_text(encoding="utf-8"))


def _order(project_dir: Path) -> list[int]:
    return [entry["episode"] for entry in _load(project_dir)["episodes"]]


def _entry(project_dir: Path, episode: int) -> dict:
    return next(entry for entry in _load(project_dir)["episodes"] if entry["episode"] == episode)


class TestCreate:
    def test_new_episode_goes_to_the_end_by_default_with_a_fresh_id(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_cut(1, 0, CH2), _own(2)], high_water=7)

        episode = create_episode(_pm(project_dir), "demo")

        assert episode == 8
        assert _order(project_dir) == [1, 2, 8]
        entry = _entry(project_dir, 8)
        assert entry["source_origin"] == "none"
        # 标题留空：界面按播出位置显示「第 N 集」，插入或调序后仍然连续
        assert entry["title"] == ""
        assert not (project_dir / "source" / "episode_8.txt").exists()

    def test_inserted_after_the_second_episode_shifts_the_later_positions(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_cut(1, 0, CH2), _cut(2, CH2, CH3), _own(3)])

        episode = create_episode(_pm(project_dir), "demo", after=2, title="  插曲 ", hook=" 钩子 ")

        assert _order(project_dir) == [1, 2, episode, 3]
        entry = _entry(project_dir, episode)
        assert entry["title"] == "插曲"
        assert entry["hook"] == "钩子"

    def test_can_sit_between_two_cut_episodes(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_cut(1, 0, CH2), _cut(2, CH2, CH3)])

        episode = create_episode(_pm(project_dir), "demo", after=1)

        assert _order(project_dir) == [1, episode, 2]

    def test_blank_episode_preserves_an_unregistered_old_source_file(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_cut(1, 0, CH2)])
        (project_dir / "source" / "episode_2.txt").write_text("没有登记的旧文件", encoding="utf-8")

        episode = create_episode(_pm(project_dir), "demo")

        assert episode == 2
        assert _entry(project_dir, 2)["source_origin"] == "none"
        assert (project_dir / "source" / "episode_2.txt").read_text(encoding="utf-8") == "没有登记的旧文件"

    def test_unknown_anchor_is_rejected_without_writing(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_own(1)])
        before = _load(project_dir)

        with pytest.raises(EpisodeManagementError) as exc:
            create_episode(_pm(project_dir), "demo", after=9)

        assert exc.value.code == "episode_not_found"
        assert _load(project_dir) == before


class TestMove:
    def test_own_and_no_source_episodes_can_move_between_cut_episodes(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_cut(1, 0, CH2), _cut(2, CH2, CH3), _own(3), _none(4)])

        move_episode(_pm(project_dir), "demo", 3, after=1)
        move_episode(_pm(project_dir), "demo", 4, after=None)

        assert _order(project_dir) == [4, 1, 3, 2]

    def test_cut_episode_can_move_past_episodes_of_other_origins(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_cut(1, 0, CH2), _own(3), _cut(2, CH2, CH3), _none(4)])

        move_episode(_pm(project_dir), "demo", 1, after=3)
        move_episode(_pm(project_dir), "demo", 2, after=4)

        assert _order(project_dir) == [3, 1, 4, 2]

    def test_cut_episodes_cannot_break_source_order(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_cut(1, 0, CH2), _cut(2, CH2, CH3), _own(3)])
        before = _load(project_dir)

        with pytest.raises(EpisodeManagementError) as exc:
            move_episode(_pm(project_dir), "demo", 1, after=2)

        assert exc.value.code == "cut_order_locked"
        assert _load(project_dir) == before

    def test_moving_an_episode_after_itself_keeps_the_order(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_own(1), _own(2), _own(3)])

        move_episode(_pm(project_dir), "demo", 2, after=2)

        assert _order(project_dir) == [1, 2, 3]

    def test_unknown_episode_or_anchor_is_rejected(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_own(1), _own(2)])

        with pytest.raises(EpisodeManagementError) as missing:
            move_episode(_pm(project_dir), "demo", 9, after=None)
        with pytest.raises(EpisodeManagementError) as anchor:
            move_episode(_pm(project_dir), "demo", 1, after=9)

        assert missing.value.code == "episode_not_found"
        assert anchor.value.code == "episode_not_found"


def _give_script(project_dir: Path, episode: int, *, storyboards: int = 0) -> None:
    (project_dir / "scripts").mkdir(exist_ok=True)
    (project_dir / "storyboards").mkdir(exist_ok=True)
    segments = []
    for index in range(1, storyboards + 1):
        segment_id = f"E{episode}S{index:02d}"
        image = f"storyboards/scene_{segment_id}.png"
        (project_dir / image).write_bytes(b"png")
        segments.append({"segment_id": segment_id, "generated_assets": {"storyboard_image": image}})
    script = {"episode": episode, "content_mode": "narration", "segments": segments}
    (project_dir / "scripts" / f"episode_{episode}.json").write_text(json.dumps(script), encoding="utf-8")


def _confirm_delete(project_dir: Path, episode: int):
    preview = delete_episode(_pm(project_dir), "demo", episode)
    assert isinstance(preview, EpisodeDeletionConfirmationRequired)
    return preview.impact, delete_episode(_pm(project_dir), "demo", episode, revision=preview.impact.revision)


class TestDelete:
    def test_delete_removes_short_drama_markdown_source(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_none(1)])
        source = project_dir / "source" / "short_drama" / "episode_1.md"
        source.parent.mkdir(parents=True)
        source.write_text("第【1】集：测试", encoding="utf-8")

        _confirm_delete(project_dir, 1)

        assert not source.exists()

    def test_deleting_a_middle_cut_episode_leaves_a_gap(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_cut(1, 0, CH2), _cut(2, CH2, CH3), _cut(3, CH3, len(A))])

        _confirm_delete(project_dir, 2)

        project = _load(project_dir)
        assert planning_start(project, discover_sources(project_dir, project)) == ("source/a.txt", len(A))
        assert [entry["episode"] for entry in project["episodes"]] == [1, 3]
        assert [(entry["source_range"]["start"], entry["source_range"]["end"]) for entry in project["episodes"]] == [
            (0, CH2),
            (CH3, len(A)),
        ]

    def test_episode_with_products_lists_what_is_lost_and_removes_it(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_cut(1, 0, CH2), _own(2)])
        _give_script(project_dir, 2, storyboards=2)
        (project_dir / "drafts" / "episode_2").mkdir(parents=True)
        (project_dir / "drafts" / "episode_2" / "script_plan_segments.json").write_text("{}", encoding="utf-8")
        (project_dir / "edit_timelines" / "episode_2").mkdir(parents=True)
        (project_dir / "edit_timelines" / "episode_2" / "tl_1.json").write_text("{}", encoding="utf-8")
        (project_dir / "storyboards" / "scene_E1S01.png").write_bytes(b"other episode")

        impact, _ = _confirm_delete(project_dir, 2)

        assert impact.recoverable is False
        assert impact.origin == "own"
        assert impact.has_script
        assert impact.has_script_plan
        assert impact.storyboard_count == 2
        assert impact.edit_timeline_count == 1
        assert _order(project_dir) == [1]
        assert not (project_dir / "source" / "episode_2.txt").exists()
        assert not (project_dir / "scripts" / "episode_2.json").exists()
        assert not (project_dir / "drafts" / "episode_2").exists()
        assert not (project_dir / "edit_timelines" / "episode_2").exists()
        assert not (project_dir / "storyboards" / "scene_E2S01.png").exists()
        assert (project_dir / "storyboards" / "scene_E1S01.png").exists()

    def test_loss_text_names_every_kind_of_product(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_own(2)])
        _give_script(project_dir, 2, storyboards=3)
        project = _load(project_dir)

        preview = delete_episode(_pm(project_dir), "demo", 2)
        assert isinstance(preview, EpisodeDeletionConfirmationRequired)
        text = render_episode_deletion_text(preview.impact, project, i18n_message)

        assert text == (
            "删除「番外2」后无法恢复，以下内容会一并删除：\n"
            "集原文（5 字）、正式脚本、分镜图 3 张\n"
            "这些产物的版本历史一并清除。"
        )

    def test_recoverable_text_for_a_cut_episode_in_the_middle(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_cut(1, 0, CH2), _cut(2, CH2, CH3), _cut(3, CH3, len(A))])
        project = _load(project_dir)

        preview = delete_episode(_pm(project_dir), "demo", 2)
        assert isinstance(preview, EpisodeDeletionConfirmationRequired)

        assert render_episode_deletion_text(preview.impact, project, i18n_message) == (
            "「集2」还没有产物。删除后，它的原文变回未切分的原文，留在「集1」与「集3」之间；"
            "一键规划不会回填这段原文，可以在「分集」视图里规划这段未切分的原文。"
        )

    def test_a_stale_confirmation_writes_nothing(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_own(1), _own(2)])
        preview = delete_episode(_pm(project_dir), "demo", 2)
        assert isinstance(preview, EpisodeDeletionConfirmationRequired)
        _give_script(project_dir, 2, storyboards=1)
        before = _load(project_dir)

        again = delete_episode(_pm(project_dir), "demo", 2, revision=preview.impact.revision)

        assert isinstance(again, EpisodeDeletionConfirmationRequired)
        assert again.impact.storyboard_count == 1
        assert _load(project_dir) == before
        assert (project_dir / "scripts" / "episode_2.json").exists()

    def test_the_only_episode_of_an_ad_project_cannot_be_deleted(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_none(1)])
        project = _load(project_dir)
        project["content_mode"] = "ad"
        (project_dir / "project.json").write_text(json.dumps(project), encoding="utf-8")

        with pytest.raises(EpisodeManagementError) as exc:
            delete_episode(_pm(project_dir), "demo", 1)

        assert exc.value.code == "ad_episode_locked"


class TestBatchDelete:
    def test_batch_delete_two_cut_episodes_in_one_confirmation(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_cut(1, 0, CH2), _cut(2, CH2, CH3), _cut(3, CH3, len(A))])

        preview = delete_episodes(_pm(project_dir), "demo", [1, 2])
        assert isinstance(preview, EpisodeBatchDeletionConfirmationRequired)
        assert preview.episodes == [1, 2]
        result = delete_episodes(_pm(project_dir), "demo", [1, 2], revision=preview.revision)

        assert isinstance(result, EpisodeBatchDeletionResult)
        assert _order(project_dir) == [3]
        assert not (project_dir / "source" / "episode_1.txt").exists()
        assert not (project_dir / "source" / "episode_2.txt").exists()
        assert (project_dir / "source" / "episode_3.txt").exists()

    def test_batch_delete_is_all_or_nothing_in_one_lock(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_own(1), _own(2), _own(3)])

        preview = delete_episodes(_pm(project_dir), "demo", [1, 2, 3])
        assert isinstance(preview, EpisodeBatchDeletionConfirmationRequired)
        result = delete_episodes(_pm(project_dir), "demo", [1, 2, 3], revision=preview.revision)

        assert isinstance(result, EpisodeBatchDeletionResult)
        assert _order(project_dir) == []

    def test_a_stale_batch_confirmation_writes_nothing(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_own(1), _own(2)])
        preview = delete_episodes(_pm(project_dir), "demo", [1, 2])
        assert isinstance(preview, EpisodeBatchDeletionConfirmationRequired)
        before = _load(project_dir)
        _give_script(project_dir, 2, storyboards=1)

        again = delete_episodes(_pm(project_dir), "demo", [1, 2], revision=preview.revision)

        assert isinstance(again, EpisodeBatchDeletionConfirmationRequired)
        assert again.impacts[1].storyboard_count == 1
        assert _load(project_dir) == before
        assert (project_dir / "scripts" / "episode_2.json").exists()

    def test_batch_text_aggregates_losses(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_own(1), _own(2)])
        _give_script(project_dir, 1, storyboards=1)
        _give_script(project_dir, 2, storyboards=1)

        preview = delete_episodes(_pm(project_dir), "demo", [1, 2])
        assert isinstance(preview, EpisodeBatchDeletionConfirmationRequired)
        project = _load(project_dir)
        text = render_episode_batch_deletion_text(preview.impacts, project, i18n_message)

        assert text.startswith("将删除 2 集：")
        assert "集原文（2 集" in text
        assert "分镜图 2 张" in text

    def test_dedupes_and_sorts_the_requested_episodes(self, tmp_path: Path):
        project_dir = _project_dir(tmp_path, [_own(1), _own(2), _own(3)])

        preview = delete_episodes(_pm(project_dir), "demo", [3, 1, 1, 2])

        assert isinstance(preview, EpisodeBatchDeletionConfirmationRequired)
        assert preview.episodes == [1, 2, 3]
