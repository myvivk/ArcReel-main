"""工厂分镜图引用角色衍生、道具与上一分镜；裁剪实发图后仍按完整依据判 current。"""

from lib.artifacts.artifact_activation import active_artifact_currency_resolver
from lib.artifacts.artifact_manifest import ArtifactKey, ArtifactStatus
from lib.project.project_manager import ProjectManager
from server.services.tasks import generation_tasks
from tests.integration.server.services.tasks.generation_tasks_support import (
    FakeGenerator,
    _FakePM,
    fake_resolve_ctx,
    persist_active_fake_project,
    prepare_files,
    register_asset_sheet_claims,
    register_stale_visual_claim,
    seed_current_storyboard,
)

ITEM_ID = "E1S02"
DERIVATIVE_SHEET = "characters/derivatives/Alice/劲装.png"


def _project_with_derivative_prop_and_previous(project_path):
    (project_path / DERIVATIVE_SHEET).parent.mkdir(parents=True, exist_ok=True)
    (project_path / DERIVATIVE_SHEET).write_bytes(b"derivative-sheet")
    fixture = _FakePM(project_path, factory=True)
    register_asset_sheet_claims(fixture)
    fixture.project["characters"]["Alice"]["derivatives"] = {
        "劲装": {"description": "换上劲装", "character_sheet": DERIVATIVE_SHEET}
    }
    fixture.script["scenes"][1]["characters_in_scene"] = ["Alice/劲装"]
    fixture.script["scenes"][0]["generated_assets"] = {}
    seed_current_storyboard(fixture, "E1S01")
    persist_active_fake_project(fixture)
    register_stale_visual_claim(project_path, ArtifactKey.asset_sheet("character", "Alice/劲装"), DERIVATIVE_SHEET)
    return fixture


def _patch(monkeypatch, pm, generator):
    monkeypatch.setattr(generation_tasks, "get_project_manager", lambda: pm)
    monkeypatch.setattr(generation_tasks, "resolve_generation_context", fake_resolve_ctx(generator))
    monkeypatch.setattr(generation_tasks, "emit_project_change_batch", lambda *_a, **_kw: None)


async def test_generated_storyboard_with_derivative_prop_and_previous_is_current(tmp_path, monkeypatch):
    project_path = prepare_files(tmp_path)
    _project_with_derivative_prop_and_previous(project_path)
    pm = ProjectManager(tmp_path)
    generator = FakeGenerator(project_path)
    _patch(monkeypatch, pm, generator)

    await generation_tasks.execute_storyboard_task(
        "demo", ITEM_ID, {"script_file": "episode_1.json", "prompt": "queued prompt"}
    )

    sent = [reference["image"].name for reference in generator.image_calls[0]["reference_images"]]
    assert sent == ["0000-劲装.png", "0001-祠堂.png", "0002-玉佩.png", "0003-scene_E1S01.png"]
    resolver = active_artifact_currency_resolver(project_path, pm.load_project("demo"))
    comparison = resolver.compare(
        ArtifactKey.episode_storyboard(1, ITEM_ID), artifact_path=f"storyboards/scene_{ITEM_ID}.png"
    )
    assert comparison.status is ArtifactStatus.CURRENT


async def test_clamping_only_trims_what_is_sent(tmp_path, monkeypatch):
    project_path = prepare_files(tmp_path)
    _project_with_derivative_prop_and_previous(project_path)
    pm = ProjectManager(tmp_path)
    generator = FakeGenerator(project_path)
    _patch(monkeypatch, pm, generator)
    monkeypatch.setattr(
        generation_tasks, "resolve_generation_context", fake_resolve_ctx(generator, image_max_reference_images=2)
    )

    result = await generation_tasks.execute_storyboard_task(
        "demo", ITEM_ID, {"script_file": "episode_1.json", "prompt": "queued prompt"}
    )

    assert len(generator.image_calls[0]["reference_images"]) == 2
    assert [warning["key"] for warning in result["warnings"]] == ["ref_too_many_images"]
    resolver = active_artifact_currency_resolver(project_path, pm.load_project("demo"))
    comparison = resolver.compare(
        ArtifactKey.episode_storyboard(1, ITEM_ID), artifact_path=f"storyboards/scene_{ITEM_ID}.png"
    )
    assert comparison.status is ArtifactStatus.CURRENT
