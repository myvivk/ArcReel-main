"""Agent 提示词入口共享工厂来源、字段顺序与覆盖准入。"""

import json
from pathlib import Path

import pytest

from lib.project.project_manager import ProjectManager
from server.text_generation import TextGenerationRequest
from tests.integration.lib.script.storyboard_support import factory_prompt_project, read_factory_script
from tests.integration.server.agent_tool_support import ToolHarness, run_declared_tool, said


def _harness(tmp_path: Path) -> ToolHarness:
    directory = factory_prompt_project(tmp_path)
    projects = ProjectManager.for_project_dir(directory)
    return ToolHarness(directory.name, projects.data_root, projects)


async def test_prompt_preview_reads_the_selected_factory_scene_without_writing(tmp_path: Path) -> None:
    ctx = _harness(tmp_path)
    script = read_factory_script(ctx.project_path)
    script["scenes"][0].update(image_prompt=None, pending_authoring=True)
    ctx.pm.save_script(ctx.project_name, script, "episode_1.json")
    path = ctx.project_path / "scripts/episode_1.json"
    before = path.read_bytes()

    outcome = await run_declared_tool(
        "generate_episode_script",
        ctx,
        {"episode_id": 1, "entry_ids": ["E1S01"], "dry_run": True, "instructions": "保留窗边停顿。"},
    )

    assert outcome.problem is None
    assert "保留窗边停顿。" in said(outcome)
    assert "本次只编写 image_prompt" in said(outcome)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("content_mode", "generation_mode"),
    [("narration", "storyboard"), ("ad", "storyboard"), ("drama", "reference_video")],
)
async def test_retired_project_routes_are_refused_without_changing_metadata(
    tmp_path: Path, content_mode: str, generation_mode: str
) -> None:
    ctx = _harness(tmp_path)
    path = ctx.project_path / "project.json"
    project = ctx.pm.load_project(ctx.project_name)
    project.update(content_mode=content_mode, generation_mode=generation_mode)
    path.write_text(json.dumps(project), encoding="utf-8")
    before = path.read_bytes()
    script_before = (ctx.project_path / "scripts/episode_1.json").read_bytes()

    outcome = await run_declared_tool("generate_episode_script", ctx, {"episode_id": 1})

    assert outcome.problem is not None
    assert outcome.problem.code == "generation_refused"
    assert path.read_bytes() == before
    assert (ctx.project_path / "scripts/episode_1.json").read_bytes() == script_before


async def test_rewrite_returns_the_current_loss_list_before_enqueue(tmp_path: Path) -> None:
    ctx = _harness(tmp_path)
    path = ctx.project_path / "scripts/episode_1.json"
    before = path.read_bytes()

    outcome = await run_declared_tool(
        "generate_episode_script",
        ctx,
        {"episode_id": 1, "entry_ids": ["E1S01"], "rewrite": True, "fields": ["image_prompt"]},
    )

    assert outcome.problem is not None
    assert outcome.problem.code == "prompt_overwrite_required"
    assert "E1S01" in said(outcome)
    assert "image_prompt" in said(outcome)
    assert path.read_bytes() == before


@pytest.mark.parametrize("episode", [0, -1, True, 1.5])
async def test_invalid_episode_is_rejected_at_the_declared_entry(tmp_path: Path, episode: object) -> None:
    ctx = _harness(tmp_path)
    outcome = await run_declared_tool("generate_episode_script", ctx, {"episode_id": episode, "dry_run": True})
    assert outcome.problem is not None
    assert outcome.problem.code == "invalid_request"


def test_prompt_layers_cannot_be_authored_together() -> None:
    with pytest.raises(ValueError, match="either image_prompt or video_prompt"):
        TextGenerationRequest(episode=1, fields=("image_prompt", "video_prompt"))
