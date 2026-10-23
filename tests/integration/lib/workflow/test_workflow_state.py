from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lib.artifacts.artifact_activation import (
    activate_artifact_target_state,
    register_current_artifact,
    register_current_artifact_if_provable,
    register_current_resource_artifact,
)
from lib.artifacts.artifact_manifest import ArtifactBasisDescriptor, ArtifactKey, ProjectArtifactManifestAdapter
from lib.artifacts.generation_input import InputRefused
from lib.artifacts.version_manager import MANUAL_UPLOAD_VERSION_SOURCE, VersionManager
from lib.edit_timeline import EditTimelineService, RevisionAuthor
from lib.infra.json_io import atomic_write_json
from lib.project import legacy_script_plans as script_review
from lib.project.project_manager import ProjectManager
from lib.project.project_migration_failure import (
    MIGRATION_FAILURE_CODE,
    MIGRATION_FAILURE_FILENAME,
    RETRY_MIGRATION_ACTION,
    record_migration_failure,
)
from lib.project.project_schema import CURRENT_PROJECT_SCHEMA_VERSION
from lib.project.resource_paths import resource_relative_path
from lib.project.source_revision import SourceScope, compute_source_revision
from lib.script.script_models import get_generated_assets
from lib.script.script_revision import content_fingerprint
from lib.script.short_drama_source import replace_short_drama_source
from lib.script.storyboard_sequence import get_storyboard_items
from lib.workflow.workflow_state import WorkflowStateService
from tests.factories import register_project_sources


def test_factory_source_without_visual_json_reports_assets_and_skips_native_planning(tmp_path: Path):
    pm, project_path = _make_project(tmp_path, "drama")
    pm.add_episode("demo", 1, "第一集", "scripts/episode_1.json")
    replace_short_drama_source(pm, "demo", 1, "[人物]：张三（外观）\n[场景]：客厅｜白墙\n")
    source_path = project_path / "source/short_drama/episodes/episode_01.md"
    before = source_path.read_bytes()
    status = WorkflowStateService(pm).get_status("demo", 1)
    assert status.content.script_from_short_drama
    assert status.content.formal_script == "absent"
    assert status.content.unregistered_references == ["张三", "客厅"]
    assert status.next_action.type == "prepare_episode_assets"
    assert status.next_action.args == {"episode_id": 1}
    assert status.next_alternatives == []
    assert source_path.read_bytes() == before
    assert not (project_path / "scripts/episode_1.json").exists()


def test_factory_source_without_assets_enters_ai_storyboard_planning(tmp_path: Path):
    pm, _project_path = _make_project(tmp_path, "drama")
    pm.add_episode("demo", 1, "第一集", "scripts/episode_1.json")
    replace_short_drama_source(pm, "demo", 1, "无人物的黑屏开场。\n")
    status = WorkflowStateService(pm).get_status("demo", 1)
    assert status.next_action.type == "plan_short_drama_storyboards"
    assert status.next_alternatives == []


def test_factory_registered_asset_description_is_observed_without_visual_json(tmp_path: Path):
    pm, _project_path = _make_project(tmp_path, "drama")
    pm.add_episode("demo", 1, "第一集", "scripts/episode_1.json")
    pm.add_character("demo", "张三", "")
    replace_short_drama_source(pm, "demo", 1, "[人物]：张三（外观）\n")
    status = WorkflowStateService(pm).get_status("demo", 1)
    assert status.content.unregistered_references == []
    assert status.content.referenced_assets_without_description == ["张三"]
    assert status.content.referenced_assets_without_sheet == ["张三"]
    assert status.next_action.type == "prepare_episode_assets"


def test_factory_missing_video_prompt_ignores_native_drafts_and_keeps_confirmed_materials(tmp_path: Path):
    from lib.script.grid.shot_grid_manager import ShotGridManager
    from lib.script.grid.shot_models import ShotGrid

    pm, project_path = _make_project(tmp_path, "drama")
    pm.add_episode("demo", 1, "第一集", "scripts/episode_1.json")
    source = replace_short_drama_source(pm, "demo", 1, "无人物的空镜。\n")
    script = {
        "episode": 1,
        "title": "第一集",
        "content_mode": "drama",
        "short_drama_meta": {"source_path": source.path, "source_revision": source.revision},
        "scenes": [_valid_drama_scene(video_prompt=None, pending_authoring=True)],
    }
    storyboard = resource_relative_path("storyboards", "E1S01")
    script["scenes"][0]["generated_assets"]["storyboard_image"] = storyboard
    _write_artifact(project_path, storyboard)
    _write_registered_script(project_path, script)
    _confirm_shot_grid(pm, project_path)
    newer_grid = ShotGrid.create(episode=1, script_file="episode_1.json", shot_id="E1S01", rows=3, cols=3)
    newer_grid.status = "completed"
    ShotGridManager(project_path).save(newer_grid)
    draft = project_path / "drafts/episode_1/script_plan_scenes.json"
    draft.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(draft, {"episode": 1, "scenes": []})
    quarantine = project_path / "drafts/episode_1/script_plan_normalized_script.invalid.json"
    atomic_write_json(quarantine, {})

    status = WorkflowStateService(pm).get_status("demo", 1)
    assert status.content.script_from_short_drama
    assert status.content.pending_authoring_ids == []
    assert status.content.drafts == []
    assert status.artifacts["shot_grids"]["current_ids"] == ["E1S01"]
    assert status.artifacts["shot_grids"]["missing_ids"] == []
    assert status.next_action.type == "author_video_prompts"
    assert status.next_action.requested_ids == ["E1S01"]
    assert (project_path / source.path).read_text(encoding="utf-8") == source.markdown
    assert draft.exists()


def _make_project(
    tmp_path: Path,
    mode: str,
    *,
    generation_mode: str = "storyboard",
) -> tuple[ProjectManager, Path]:
    pm = ProjectManager(tmp_path / "projects")
    pm.create_project("demo")
    project = pm.create_project_metadata("demo", "Demo", "", "drama")
    if mode != "drama" or generation_mode != "storyboard":
        project.update(content_mode=mode, generation_mode=generation_mode, grid_storyboard=False)
        if mode == "ad":
            project.update(target_duration=30, brief="")
        pm.save_project("demo", project)
    return pm, pm.get_project_path("demo")


def _write_source(pm: ProjectManager, project_path: Path, text: str = "原文") -> str:
    register_project_sources(pm, "demo", whole_source={"novel.txt": text})
    scope = SourceScope(kind="all")
    revision = compute_source_revision(project_path, pm.load_project("demo"), scope).revision
    assert revision is not None
    return revision


def _write_artifact(project_path: Path, relative_path: str) -> None:
    path = project_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"artifact")


def _register_produced_artifacts(project_path: Path) -> None:
    """把已落盘的产物按规范目标态补录进产物清单。

    产物清单是读取已生成产物的唯一口径，落盘本身不代表已登记；这里走的是生产的补录路径。
    """
    activate_artifact_target_state(project_path, bump_schema=False)


def _write_registered_script(
    project_path: Path,
    script: dict,
    *,
    episode: int = 1,
    filename: str = "episode_1.json",
) -> None:
    """写剧本并登记认领——落盘本身不进读取口径，未登记的剧本一律按 missing 处理。

    剧集剧本的取证以 script_plan 为输入，故一并登记 script_plan（广告/短片无 script_plan，登记为不可取证即跳过）。
    """
    atomic_write_json(project_path / "scripts" / filename, script)
    register_current_artifact_if_provable(project_path, ArtifactKey.episode_script_plan(episode))
    register_current_artifact(project_path, ArtifactKey.episode_script(episode))


def _edit_claimed_script(
    project_path: Path,
    payload: object,
    *,
    filename: str = "episode_1.json",
) -> None:
    """把一份已登记认领的剧本在盘上改成 payload（外部编辑造成的脏数据）。

    未登记的文件根本不进读取口径，所以脏数据要能被观察到，必须先有认领。
    """
    path = project_path / "scripts" / filename
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        atomic_write_json(path, payload)


def _write_episode_source(project_path: Path, episode: int = 1, text: str = "原文") -> None:
    """写分集原文——script_plan 的取证以它为输入，缺了 script_plan 就无法登记。"""
    path = project_path / "source" / f"episode_{episode}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit_media_version(project_path: Path, resource_type: str, resource_id: str) -> str:
    """按规范路径落一个媒体产物并提交版本记录，返回项目内相对路径。

    视频 / 旁白配音的清单取证只认版本记录里的执行事实，落盘文件本身不构成证据。
    """
    relative = resource_relative_path(resource_type, resource_id)
    current = project_path / relative
    current.parent.mkdir(parents=True, exist_ok=True)
    staged = current.parent / f".{resource_id}.upload{current.suffix}"
    staged.write_bytes(b"artifact")
    VersionManager(project_path).commit_staged_version(
        resource_type,
        resource_id,
        "",
        staged_file=staged,
        current_file=current,
        source=MANUAL_UPLOAD_VERSION_SOURCE,
    )
    return relative


def complete_episode_media(project_path: Path, resource_id: str = "E1S01") -> dict[str, str]:
    """落齐一个分镜的分镜图与视频（含版本记录），返回可写进 generated_assets 的指针。"""
    storyboard = resource_relative_path("storyboards", resource_id)
    _write_artifact(project_path, storyboard)
    return {
        "storyboard_image": storyboard,
        "video_clip": _commit_media_version(project_path, "videos", resource_id),
    }


#: 参考素材（单分镜宫格）记录的可复现 id：``shot_grid_`` + 12 位十六进制。
_SHOT_GRID_ID = "shot_grid_000000000001"


def _confirm_shot_grid(
    pm: ProjectManager,
    project_path: Path,
    *,
    shot_id: str = "E1S01",
    episode: int = 1,
    script_file: str = "episode_1.json",
    rows: int = 2,
    cols: int = 2,
) -> None:
    """登记一条已确认的单分镜宫格，让「参考素材」行有可判定的记录（ADR 0099 D-10）。

    confirm 会把整图登记成分镜图（``episode_storyboard`` 键 + 分镜图依据），记录里的
    ``composite_path`` 就指向登记在案的那一份。缺了这份记录，分镜图再齐也报缺参考素材。
    """

    from lib.artifacts.artifact_registration import register_current_resource_artifact
    from lib.artifacts.generation_input import project_input_observation, storyboard_image_input
    from lib.script.grid.shot_grid_manager import ShotGridManager
    from lib.script.grid.shot_models import ShotGrid, ShotGridCell

    project = pm.load_project("demo")
    script = json.loads((project_path / "scripts" / script_file).read_text(encoding="utf-8"))
    generation_input = storyboard_image_input(
        project,
        script,
        episode=episode,
        resource_id=shot_id,
        observation=project_input_observation(project_path),
    )
    assert not isinstance(generation_input, InputRefused), "确认参考素材前分镜图输入必须成立"
    basis = generation_input.expected_basis()
    items, id_field, _, _, _ = get_storyboard_items(script)
    item = next(entry for entry in items if str(entry.get(id_field, "")) == shot_id)
    composite = get_generated_assets(item).get("storyboard_image")
    assert isinstance(composite, str), "确认参考素材前分镜图必须是路径字符串"
    assert composite, "确认参考素材前分镜图必须已在剧本里声明"
    _write_artifact(project_path, composite)
    ShotGridManager(project_path).save(
        ShotGrid(
            id=_SHOT_GRID_ID,
            episode=episode,
            script_file=script_file,
            shot_id=shot_id,
            rows=rows,
            cols=cols,
            cells=[ShotGridCell(index=index, prompt=f"第{index + 1}格：画面") for index in range(rows * cols)],
            status="confirmed",
            composite_path=composite,
            image_basis=ArtifactBasisDescriptor.from_basis(basis).to_dict(),
        )
    )
    # 登记是幂等的：条目已在案且完全一致时返回 False，故按清单里的最终状态断言。
    register_current_resource_artifact(
        project_path,
        resource_type="storyboards",
        resource_id=shot_id,
        script_file=script_file,
        artifact_path=composite,
        basis=basis,
    )
    entry = ProjectArtifactManifestAdapter(project_path).get_entry(ArtifactKey.episode_storyboard(episode, shot_id))
    assert entry is not None
    assert entry.artifact_path == composite
    assert entry.basis_digest == basis.digest


def _valid_narration_segment(**overrides: object) -> dict:
    segment = {
        "segment_id": "E1S01",
        "duration_seconds": 4,
        "novel_text": "原文",
        "characters_in_segment": [],
        "scenes": [],
        "props": [],
        "image_prompt": "画面",
        "video_prompt": "动作",
        "generated_assets": {},
    }
    segment.update(overrides)
    return segment


def _valid_drama_scene(**overrides: object) -> dict:
    scene = {
        "scene_id": "E1S01",
        "duration_seconds": 4,
        "characters_in_scene": [],
        "scenes": [],
        "props": [],
        "image_prompt": "画面",
        "video_prompt": "动作",
        "generated_assets": {},
    }
    scene.update(overrides)
    return scene


def _valid_ad_shot(**overrides: object) -> dict:
    shot = {
        "shot_id": "E1S01",
        "duration_seconds": 4,
        "voiceover_text": "",
        "characters_in_shot": [],
        "scenes": [],
        "props": [],
        "products_in_shot": [],
        "image_prompt": "画面",
        "video_prompt": "动作",
        "generated_assets": {},
    }
    shot.update(overrides)
    return shot


def _valid_video_unit(**overrides: object) -> dict:
    unit = {
        "unit_id": "E1U01",
        "duration_seconds": 8,
        "text": "镜头",
        "generated_assets": {},
    }
    unit.update(overrides)
    return unit


def test_media_paths_must_resolve_to_project_files_before_becoming_current(tmp_path: Path) -> None:
    pm, project_path = _make_project(tmp_path, "ad")
    _write_registered_script(
        project_path,
        {
            "episode": 1,
            "title": "广告",
            "content_mode": "ad",
            "shots": [
                _valid_ad_shot(
                    generated_assets={
                        "storyboard_image": "../outside.png",
                        "video_clip": "videos/missing.mp4",
                    }
                )
            ],
        },
    )
    status = WorkflowStateService(pm).get_status("demo")

    # 越界指针是硬阻断（不是「当作没生成」），项目内但未登记的指针才是 missing。
    assert status.artifacts["storyboards"]["state"] == "blocked"
    assert status.artifacts["storyboards"]["current_ids"] == []
    assert [blocker.code for blocker in status.issues] == ["artifact_path_invalid"]
    assert "../outside.png" in status.issues[0].reason
    assert status.artifacts["videos"]["current_ids"] == []
    assert status.artifacts["videos"]["missing_ids"] == ["E1S01"]
    assert status.next_action.type == "none"


def create_edit_timeline(pm: ProjectManager, episode: int = 1) -> str:
    """按脚本机械新建一条剪辑时间线，让该集走完「剪辑」一步。"""

    readout = asyncio.run(
        EditTimelineService(pm).create_from_script(
            "demo", episode=episode, name="完整版", author=RevisionAuthor(kind="creator", user_id="u1")
        )
    )
    return readout.timeline.id


def count_source_reads(monkeypatch: pytest.MonkeyPatch, project_path: Path) -> dict[str, int]:
    """记录 ``source/`` 直下每份源文被读取的次数，字节与文本两条读法都算。"""

    counts: dict[str, int] = {}
    source_dir = (project_path / "source").resolve()
    original_read_bytes = Path.read_bytes
    original_read_text = Path.read_text

    def _record(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved.parent == source_dir:
            counts[resolved.name] = counts.get(resolved.name, 0) + 1

    def _counted_read_bytes(self: Path) -> bytes:
        _record(self)
        return original_read_bytes(self)

    def _counted_read_text(self: Path, *args: object, **kwargs: object) -> str:
        _record(self)
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", _counted_read_bytes)
    monkeypatch.setattr(Path, "read_text", _counted_read_text)
    return counts


def test_episode_next_steps_are_empty_while_the_migration_has_failed(tmp_path: Path) -> None:
    pm, project_path = _make_project(tmp_path, "narration")
    pm.update_project(
        "demo",
        lambda project: project.update(
            episodes=[{"episode": 1, "script_file": "scripts/episode_1.json", "ledger_status": "planned"}]
        ),
    )
    record_migration_failure(project_path, ValueError("step failed"), schema_version=1)

    assert WorkflowStateService(pm).get_episode_next_steps("demo") == []


@pytest.mark.parametrize(
    ("field", "value", "blocker_code"),
    [
        ("content_mode", [], "invalid_content_mode"),
        ("generation_mode", {}, "invalid_generation_mode"),
    ],
)
def test_non_string_project_mode_returns_blocker(tmp_path: Path, field: str, value: object, blocker_code: str) -> None:
    pm, _project_path = _make_project(tmp_path, "ad")
    pm.update_project("demo", lambda project: project.update({field: value}))

    status = WorkflowStateService(pm).get_status("demo")
    assert status.blockers[0].code == blocker_code
    assert status.next_action.type == "none"


def test_invalid_asset_definition_is_an_issue(tmp_path: Path) -> None:
    pm, project_path = _make_project(tmp_path, "ad")
    sheet = "characters/invalid.png"
    _write_artifact(project_path, sheet)
    pm.update_project(
        "demo",
        lambda project: project.update(characters={"坏描述角色": {"description": 3, "character_sheet": sheet}}),
    )

    status = WorkflowStateService(pm).get_status("demo")
    assert status.blockers == []
    assert status.issues[0].code == "invalid_asset_definitions"


def test_malformed_script_collection_is_an_issue_not_an_exception(tmp_path: Path) -> None:
    pm, project_path = _make_project(tmp_path, "ad")
    _write_registered_script(
        project_path, {"episode": 1, "title": "广告", "content_mode": "ad", "shots": [_valid_ad_shot()]}
    )
    _edit_claimed_script(project_path, {"episode": 1, "content_mode": "ad", "shots": {"not": "a list"}})

    status = WorkflowStateService(pm).get_status("demo")
    assert status.artifacts["script"]["state"] == "blocked"
    assert status.issues[0].code == "artifact_currency_unavailable"
    assert "shots" in status.issues[0].reason
    assert status.next_action.type == "none"


def test_non_object_script_is_an_issue_not_an_exception(tmp_path: Path) -> None:
    pm, project_path = _make_project(tmp_path, "ad")
    _write_registered_script(
        project_path, {"episode": 1, "title": "广告", "content_mode": "ad", "shots": [_valid_ad_shot()]}
    )
    _edit_claimed_script(project_path, [])

    status = WorkflowStateService(pm).get_status("demo")
    assert status.artifacts["script"]["state"] == "blocked"
    assert status.issues[0].code == "artifact_currency_unavailable"
    assert "must contain an object" in status.issues[0].reason


@pytest.mark.parametrize(
    ("mode", "script_plan_filename", "script_plan_payload", "items_key"),
    [
        ("narration", "script_plan_segments.json", {"segments": []}, "segments"),
        ("drama", "script_plan_normalized_script.json", {"scenes": []}, "scenes"),
    ],
)
def test_legacy_storyboard_script_without_duration_remains_resumable(
    tmp_path: Path,
    mode: str,
    script_plan_filename: str,
    script_plan_payload: dict,
    items_key: str,
) -> None:
    pm, project_path = _make_project(tmp_path, mode)
    _write_source(pm, project_path)
    pm.update_project(
        "demo",
        lambda project: project.update(
            episodes=[{"episode": 1, "script_file": "scripts/episode_1.json", "ledger_status": "planned"}]
        ),
    )
    draft_dir = project_path / "drafts" / "episode_1"
    draft_dir.mkdir(parents=True)
    _write_episode_source(project_path, 1)
    script_plan_path = draft_dir / script_plan_filename
    atomic_write_json(script_plan_path, script_plan_payload)
    revision = content_fingerprint(script_plan_path)
    assert revision is not None
    pm.update_project(
        "demo", lambda project: script_review.apply_confirmation(project, 1, revision, "2026-08-11T00:00:00Z")
    )
    item = (
        _valid_narration_segment(duration_seconds=None)
        if mode == "narration"
        else _valid_drama_scene(duration_seconds=None)
    )
    item.pop("duration_seconds")
    _write_registered_script(
        project_path,
        {
            "episode": 1,
            "title": "第一集",
            "content_mode": mode,
            items_key: [item],
        },
    )

    status = WorkflowStateService(pm).get_status("demo")
    assert status.artifacts["script"]["state"] == "current"
    assert not status.blockers


def test_legacy_narration_scenes_skeleton_remains_resumable(tmp_path: Path) -> None:
    pm, project_path = _make_project(tmp_path, "narration")
    source_text = "完整原文"
    _write_source(pm, project_path, source_text)

    def _plan(project: dict) -> None:
        project["episodes"] = [
            {
                "episode": 1,
                "script_file": "scripts/episode_1.json",
                "ledger_status": "planned",
            }
        ]

    pm.update_project("demo", _plan)
    draft_dir = project_path / "drafts" / "episode_1"
    draft_dir.mkdir(parents=True)
    _write_episode_source(project_path, 1)
    atomic_write_json(draft_dir / "script_plan_segments.json", {"episode": 1, "segments": []})
    _write_registered_script(
        project_path,
        {
            "episode": 1,
            "title": "第一集",
            "content_mode": "narration",
            "scenes": [_valid_drama_scene()],
        },
    )

    status = WorkflowStateService(pm).get_status("demo")
    assert status.artifacts["script"]["state"] == "current"
    assert status.next_action.requested_ids == ["E1S01"]


def test_schema8_workflow_accepts_a_registered_manual_reference_video(tmp_path: Path) -> None:
    pm, project_path = _make_project(tmp_path, "ad", generation_mode="reference_video")
    video_path = "reference_videos/E1U1.mp4"
    _write_registered_script(
        project_path,
        {
            "episode": 1,
            "title": "广告",
            "content_mode": "ad",
            "video_units": [_valid_video_unit(unit_id="E1U1", generated_assets={"video_clip": video_path})],
        },
    )
    staged = project_path / "reference_videos" / ".E1U1.upload.mp4"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"manual-video")
    VersionManager(project_path).commit_staged_version(
        "reference_videos",
        "E1U1",
        "",
        staged_file=staged,
        current_file=project_path / video_path,
        source=MANUAL_UPLOAD_VERSION_SOURCE,
    )
    register_current_resource_artifact(
        project_path, resource_type="reference_videos", resource_id="E1U1", script_file="scripts/episode_1.json"
    )

    status = WorkflowStateService(pm).get_status("demo")
    assert status.artifacts["videos"] == {
        "current_ids": ["E1U1"],
        "missing_ids": [],
        "stale_ids": [],
    }
    assert status.next_action.type == "create_edit_timeline"


def test_schema8_workflow_does_not_parse_an_unclaimed_malformed_script(tmp_path: Path) -> None:
    pm, project_path = _make_project(tmp_path, "ad")
    pm.update_project("demo", lambda project: project.update(brief="夏季新品"))
    (project_path / "scripts" / "episode_1.json").write_text("{", encoding="utf-8")

    status = WorkflowStateService(pm).get_status("demo")
    assert status.artifacts["script"] == {"state": "missing", "path": "scripts/episode_1.json"}
    assert status.blockers == []
    assert status.next_action.type == "generate_script"


def test_schema8_manifest_reports_current_stale_missing_and_blocked_without_file_fallback(tmp_path: Path) -> None:
    pm, project_path = _make_project(tmp_path, "ad")
    sheet_path = "characters/Alice.png"
    storyboard_path = "storyboards/scene_E1S01.png"
    _write_artifact(project_path, sheet_path)
    _write_artifact(project_path, storyboard_path)

    def _seed(project: dict) -> None:
        project["characters"] = {
            "Alice": {
                "description": "red coat",
                "character_sheet": sheet_path,
            }
        }

    pm.update_project("demo", _seed)
    script_path = project_path / "scripts" / "episode_1.json"
    script = {
        "episode": 1,
        "title": "广告",
        "content_mode": "ad",
        "shots": [
            _valid_ad_shot(
                image_prompt="red coat hero",
                generated_assets={"storyboard_image": storyboard_path},
            )
        ],
    }
    atomic_write_json(script_path, script)
    _register_produced_artifacts(project_path)

    current = WorkflowStateService(pm).get_status("demo")
    assert current.artifacts["script"]["state"] == "current"
    assert current.artifacts["asset_sheets"]["character"]["current_ids"] == ["Alice"]
    assert current.artifacts["storyboards"]["current_ids"] == ["E1S01"]

    pm.update_project("demo", lambda project: project["characters"]["Alice"].update(description="blue coat"))
    script["shots"][0]["image_prompt"] = "blue coat hero"
    atomic_write_json(script_path, script)
    stale = WorkflowStateService(pm).get_status("demo")
    assert stale.artifacts["asset_sheets"]["character"]["stale_ids"] == ["Alice"]
    assert stale.artifacts["storyboards"]["stale_ids"] == ["E1S01"]

    adapter = ProjectArtifactManifestAdapter(project_path)
    adapter.delete_entry(ArtifactKey.episode_storyboard(1, "E1S01"))
    missing = WorkflowStateService(pm).get_status("demo")
    assert missing.artifacts["storyboards"]["missing_ids"] == ["E1S01"]

    register_current_artifact(project_path, ArtifactKey.episode_storyboard(1, "E1S01"))
    storyboard_file = project_path / storyboard_path
    storyboard_file.unlink()
    storyboard_file.symlink_to(project_path / sheet_path)
    blocked = WorkflowStateService(pm).get_status("demo")
    assert blocked.artifacts["storyboards"]["state"] == "blocked"
    assert any(item.code == "artifact_symlink" for item in blocked.issues)


def test_blocked_final_script_is_not_reclassified_as_stale_by_provenance(tmp_path: Path) -> None:
    pm, project_path = _make_project(tmp_path, "drama")
    _write_source(pm, project_path)
    pm.update_project(
        "demo",
        lambda project: project.update(
            episodes=[{"episode": 1, "script_file": "scripts/episode_1.json", "ledger_status": "planned"}]
        ),
    )
    draft_dir = project_path / "drafts" / "episode_1"
    draft_dir.mkdir(parents=True)
    _write_episode_source(project_path, 1)
    script_plan_path = draft_dir / "script_plan_normalized_script.json"
    atomic_write_json(script_plan_path, {"scenes": []})
    revision = content_fingerprint(script_plan_path)
    assert revision is not None
    pm.update_project(
        "demo", lambda project: script_review.apply_confirmation(project, 1, revision, "2026-08-11T00:00:00Z")
    )
    _write_registered_script(
        project_path,
        {
            "episode": 1,
            "title": "第一集",
            "content_mode": "drama",
            "scenes": [_valid_drama_scene()],
        },
    )
    _edit_claimed_script(project_path, [])

    status = WorkflowStateService(pm).get_status("demo")
    assert status.artifacts["script"]["state"] == "blocked"
    assert status.issues[0].code == "artifact_currency_unavailable"
    assert "must contain an object" in status.issues[0].reason
    assert status.next_action.type == "none"


def test_workflow_status_does_not_persist_read_time_script_migrations(tmp_path: Path) -> None:
    pm, project_path = _make_project(tmp_path, "drama", generation_mode="reference_video")
    source_text = "原文"
    _write_source(pm, project_path, source_text)
    pm.update_project(
        "demo",
        lambda project: project.update(
            episodes=[{"episode": 1, "script_file": "scripts/episode_1.json", "ledger_status": "consumed"}],
        ),
    )
    draft_dir = project_path / "drafts" / "episode_1"
    draft_dir.mkdir(parents=True)
    _write_episode_source(project_path, 1)
    atomic_write_json(draft_dir / "script_plan_reference_units.json", {"units": []})
    script_path = project_path / "scripts" / "episode_1.json"
    _write_registered_script(
        project_path,
        {
            "episode": 1,
            "title": "第一集",
            "content_mode": "drama",
            "video_units": [
                {
                    "unit_id": "E1U01",
                    "text": "镜头",
                    "duration_seconds": 8,
                    "generated_assets": {},
                }
            ],
        },
    )
    before = script_path.read_bytes()

    WorkflowStateService(pm).get_status("demo")
    assert script_path.read_bytes() == before
    assert not (script_path.parent / ".episode_1.json.lock").exists()


def test_unmigrated_project_reports_only_the_migration_blocker(tmp_path: Path) -> None:
    """产物清单是读取已生成产物的唯一口径：schema 未到 8 的项目没有可读的登记，
    get_status 只报「未迁移」这一条阻断，不按文件是否在磁盘上倒推任何产物状态。"""
    pm, project_path = _make_project(tmp_path, "narration")
    _write_source(pm, project_path)
    _write_artifact(project_path, resource_relative_path("storyboards", "E1S01"))
    pm.update_project("demo", lambda project: project.update(schema_version=7))

    status = WorkflowStateService(pm).get_status("demo")
    assert [blocker.code for blocker in status.blockers] == [MIGRATION_FAILURE_CODE]
    assert status.blockers[0].path == MIGRATION_FAILURE_FILENAME
    assert status.blockers[0].reason == (
        f"project schema v7 has not been upgraded to v{CURRENT_PROJECT_SCHEMA_VERSION}; "
        "produced artifacts cannot be read until it is"
    )
    assert status.next_action.type == RETRY_MIGRATION_ACTION
    assert status.artifacts["script"] == {"state": "missing"}
    assert status.artifacts["storyboards"] == {"current_ids": [], "missing_ids": [], "stale_ids": []}


def test_workflow_status_does_not_persist_read_time_project_migrations(tmp_path: Path) -> None:
    pm, project_path = _make_project(tmp_path, "ad")
    project_path_json = project_path / "project.json"
    project = pm.load_project("demo")
    project.pop("style_template_id", None)
    project["style"] = "Anime"
    atomic_write_json(project_path_json, project)
    before = project_path_json.read_bytes()

    status = WorkflowStateService(pm).get_status("demo")

    assert status.project.content_mode == "ad"
    assert project_path_json.read_bytes() == before


def test_nested_ledger_script_path_is_an_issue_before_dispatch(tmp_path: Path) -> None:
    pm, project_path = _make_project(tmp_path, "ad")
    pm.update_project(
        "demo",
        lambda project: project.update(
            episodes=[
                {
                    "episode": 1,
                    "title": "广告",
                    "script_file": "scripts/archive/custom.json",
                    "ledger_status": "planned",
                }
            ]
        ),
    )
    nested_script = project_path / "scripts" / "archive" / "custom.json"
    nested_script.parent.mkdir(parents=True)
    atomic_write_json(
        nested_script,
        {
            "episode": 1,
            "title": "广告",
            "content_mode": "ad",
            "shots": [_valid_ad_shot()],
        },
    )

    status = WorkflowStateService(pm).get_status("demo")
    assert status.target is None
    assert status.blockers == []
    assert status.issues[0].code == "invalid_script_path"
    assert status.next_action.type == "none"


@pytest.mark.parametrize("payload", [None, b"{", b"[]", b"\xff"])
def test_unreadable_project_data_returns_only_a_project_blocker(tmp_path: Path, payload: bytes | None) -> None:
    pm, project_path = _make_project(tmp_path, "narration")
    project_file = project_path / "project.json"
    if payload is None:
        project_file.unlink()
    else:
        project_file.write_bytes(payload)

    status = WorkflowStateService(pm).get_status("demo", 1)

    assert [(blocker.code, blocker.path) for blocker in status.blockers] == [
        ("project_data_unavailable", "project.json")
    ]
    assert status.content is None
    assert status.operations == {}
    assert status.next_action.type == "none"
