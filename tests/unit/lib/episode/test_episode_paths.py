"""script_plan / episode 路径单一真相源的行为测试。

只测外部可观察契约：结构化 script_plan 文件名解析、旧版 .md 兼认边界、episode 剧本路径，
以及"新增 content_mode 登记一处即被 gate / web / Agent 写盘共同覆盖"这一收敛不变量。
"""

from __future__ import annotations

from pathlib import Path

from lib.episode import episode_paths
from lib.project import legacy_script_plans


def test_script_plan_filename_by_content_mode():
    assert episode_paths.script_plan_filename("drama") == "script_plan_normalized_script.json"
    assert episode_paths.script_plan_filename("narration") == "script_plan_segments.json"
    # ad 不走结构化 script_plan
    assert episode_paths.script_plan_filename("ad") is None
    assert episode_paths.script_plan_filename("unknown") is None


def test_script_plan_read_candidates_includes_legacy_md():
    assert episode_paths.script_plan_read_candidates("narration") == (
        "script_plan_segments.json",
        "script_plan_segments.md",
    )
    assert episode_paths.script_plan_read_candidates("drama") == (
        "script_plan_normalized_script.json",
        "script_plan_normalized_script.md",
    )
    assert episode_paths.script_plan_read_candidates("ad") == ()


def test_episode_script_paths():
    assert episode_paths.episode_script_filename(3) == "episode_3.json"
    assert episode_paths.episode_script_relpath(3) == "scripts/episode_3.json"


def test_episode_drafts_dir():
    assert episode_paths.episode_drafts_dir(Path("/p"), 2) == Path("/p/drafts/episode_2")






def test_gate_only_json_and_web_also_md():
    """gate 只认结构化 .json；web 读取兼认旧版 .md（既有语义差异，见 ADR 0041）。"""
    # gate 的登记表不含任何 .md
    assert all(name.endswith(".json") for name in episode_paths.SCRIPT_PLAN_FILENAMES.values())
    # web 读取候选含旧 .md
    assert "script_plan_segments.md" in episode_paths.script_plan_read_candidates("narration")
    assert "script_plan_normalized_script.md" in episode_paths.script_plan_read_candidates("drama")


def test_script_plan_path_follows_reference_video_across_content_modes(tmp_path):
    """rv 是跨 content_mode 的 generation_mode 维度：narration/drama 项目挂 rv 后，gate 路径都改落
    rv 专属结构化文件名，而非各自 content_mode 对应名——该集脚本进度的产物态探测同走这条路径。
    """
    for content_mode in ("narration", "drama"):
        project = {"content_mode": content_mode, "generation_mode": "reference_video", "episodes": [{"episode": 1}]}
        assert legacy_script_plans.script_plan_path(tmp_path, project, 1) == (
            episode_paths.episode_drafts_dir(tmp_path, 1) / episode_paths.REFERENCE_VIDEO_SCRIPT_PLAN_FILENAME
        )
    # 未挂 rv 的项目沿用 content_mode 既有文件名，不受影响
    assert legacy_script_plans.script_plan_path(tmp_path, {"content_mode": "narration", "episodes": [{"episode": 1}]}, 1) == (
        episode_paths.episode_drafts_dir(tmp_path, 1) / episode_paths.script_plan_filename("narration")
    )
    # ad 优先于 generation_mode：即便挂 rv 也无结构化 script_plan（与 gate/web 同口径，见上一测试）
    assert (
        legacy_script_plans.script_plan_path(
            tmp_path,
            {"content_mode": "ad", "generation_mode": "reference_video", "episodes": [{"episode": 1}]},
            1,
        )
        is None
    )
