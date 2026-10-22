from lib.script.grid.shot_grid_manager import ShotGridManager, select_shot_grids
from lib.script.grid.shot_models import ShotGrid


def test_reading_absent_grids_does_not_create_a_directory(tmp_path):
    manager = ShotGridManager(tmp_path)
    assert manager.list_all() == []
    assert manager.find(script_file="episode_1.json", shot_id="E1S1") is None
    assert not (tmp_path / "shot_grids").exists()


def test_confirmed_grid_is_selected_before_a_newer_completed_grid(tmp_path):
    manager = ShotGridManager(tmp_path)
    confirmed = ShotGrid.create(episode=1, script_file="episode_1.json", shot_id="E1S1", rows=2, cols=2)
    confirmed.status = "confirmed"
    confirmed.updated_at = "2026-10-20T00:00:00Z"
    completed = ShotGrid.create(episode=1, script_file="episode_1.json", shot_id="E1S1", rows=3, cols=3)
    completed.status = "completed"
    completed.updated_at = "2026-10-22T00:00:00Z"
    manager.save(completed)
    manager.save(confirmed)
    assert manager.find(script_file="episode_1.json", shot_id="E1S1").id == confirmed.id
    assert select_shot_grids(manager.list_all())["E1S1"].id == confirmed.id


def test_bound_grid_wins_and_a_missing_binding_cannot_select_another_grid():
    older = ShotGrid.create(episode=1, script_file="episode_1.json", shot_id="E1S1", rows=2, cols=2)
    newer = ShotGrid.create(episode=1, script_file="episode_1.json", shot_id="E1S1", rows=3, cols=3)
    older.status = newer.status = "confirmed"
    older.updated_at = "2026-10-20T00:00:00Z"
    newer.updated_at = "2026-10-22T00:00:00Z"
    item = {"scene_id": "E1S1", "generated_assets": {"shot_grid": {"grid_id": older.id}}}
    assert select_shot_grids([older, newer], items=[item])["E1S1"].id == older.id
    assert select_shot_grids([newer], items=[item]) == {}
