"""分镜图、参考素材与分镜视频的集范围批量：选目标、跳过项、整批准入与提交，落在真实项目目录与真实队列上。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.artifacts.artifact_currency import resolve_current_artifact_target
from lib.artifacts.artifact_manifest import ArtifactKey, ArtifactManifestEntry, ProjectArtifactManifestAdapter
from lib.db.base import DEFAULT_USER_ID
from lib.db.repositories.custom_provider_repo import CustomProviderRepository
from lib.generation.batch_admission import BatchAdmissionDecision
from lib.generation.generation_queue import GenerationQueue
from lib.generation.generation_result import GenerationSelectionMode
from lib.generation.video_request_facts import VideoRequestCostFacts
from lib.project.project_manager import ProjectManager
from lib.script.grid.keyframe_parser import parse_scene_shots
from lib.script.grid.shot_grid_manager import ShotGridManager
from lib.script.grid.shot_models import ShotGrid
from server.services.admission.cost_estimation import quote_video_request_from_price
from server.services.admission.storyboard_batch import (
    SHOT_GRID_BATCH_COLS,
    SHOT_GRID_BATCH_OPERATION,
    SHOT_GRID_BATCH_ROWS,
    BatchSkip,
    BatchSkipReason,
    StoryboardBatchEpisodeNotFound,
    StoryboardBatchRouteMismatch,
    estimate_storyboard_video_batch_cost,
    load_shot_grid_batch_plan,
    load_storyboard_image_batch_plan,
    load_storyboard_video_batch_plan,
    storyboard_image_prompt,
    submit_shot_grid_batch,
    submit_storyboard_image_batch,
    submit_storyboard_video_batch,
)
from server.services.admission.video_batch_admission import admit_storyboard_video_request
from tests.factories import make_video_request_facts, wav_bytes
from tests.integration.lib.script.storyboard_support import confirmed_grid

PROJECT = "demo"


def _scene(*shots: str) -> dict:
    """结构化分镜提示词：导语加若干 ``【镜头N｜景别】正文``。"""

    return {
        "scene": "村口黄昏。"
        + "".join(f"【镜头{index}｜{size}】{text}" for index, (size, text) in enumerate(shots, start=1))
    }


def _sequence(*texts: str) -> str:
    """逐格镜头文本的标准形态。"""

    return "\n".join(f"第{index}格（远景）：{text}" for index, text in enumerate(texts, start=1))


@pytest.fixture
def board_projects(tmp_path: Path) -> ProjectManager:
    manager = ProjectManager(tmp_path / "projects")
    manager.create_project(PROJECT)
    manager.create_project_metadata(PROJECT, "Demo", content_mode="narration")
    return manager


@pytest.fixture
async def board_queue(session_factory, board_projects: ProjectManager) -> GenerationQueue:
    generation_queue = GenerationQueue(session_factory=session_factory, project_manager=board_projects)
    await generation_queue.acquire_or_renew_worker_lease(name="default", owner_id="worker-a", ttl_seconds=60)
    return generation_queue


def _segment(segment_id: str, **overrides: object) -> dict:
    segment: dict = {
        "segment_id": segment_id,
        "novel_text": "黄昏时分，风吹过村口。",
        "image_prompt": "村口黄昏",
        "video_prompt": {"action": "镜头平移", "camera_motion": "Pan", "ambiance_audio": "风声"},
        "duration_seconds": 4,
        "generated_assets": {},
    }
    segment.update(overrides)
    return segment


def _write_episode(board_projects: ProjectManager, segments: list[dict]) -> None:
    scripts = board_projects.get_project_path(PROJECT) / "scripts"
    scripts.mkdir(exist_ok=True)
    script = {"episode": 1, "title": "第一集", "content_mode": "narration", "segments": segments}
    (scripts / "episode_1.json").write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    board_projects.add_episode(PROJECT, 1, "第一集", "scripts/episode_1.json")


def _write_media(board_projects: ProjectManager, relative_path: str) -> None:
    path = board_projects.get_project_path(PROJECT) / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(relative_path.encode("utf-8"))


def _claim_current(board_projects: ProjectManager, key: ArtifactKey) -> None:
    project_dir = board_projects.get_project_path(PROJECT)
    entry = resolve_current_artifact_target(project_dir, key)
    assert entry is not None
    ProjectArtifactManifestAdapter(project_dir).put_entry(key, entry)


def _claim_stale(board_projects: ProjectManager, key: ArtifactKey, relative_path: str) -> None:
    ProjectArtifactManifestAdapter(board_projects.get_project_path(PROJECT)).put_entry(
        key, ArtifactManifestEntry(artifact_path=relative_path, basis_digest=f"sha256-v1:{'a' * 64}")
    )


def _with_storyboard(board_projects: ProjectManager, segment_id: str) -> dict:
    """带一张现行分镜图的分镜：图先落盘并登记，剧本随后写入。"""

    relative_path = f"storyboards/scene_{segment_id}.png"
    _write_media(board_projects, relative_path)
    return _segment(segment_id, generated_assets={"storyboard_image": relative_path})


def _claim_storyboards(board_projects: ProjectManager, *segment_ids: str) -> None:
    for segment_id in segment_ids:
        _claim_current(board_projects, ArtifactKey.episode_storyboard(1, segment_id))


class TestStoryboardImagePrompt:
    def test_structured_prompt_carries_one_style_line(self) -> None:
        segment = {
            "segment_id": "E1S01",
            "image_prompt": {
                "scene": "村口黄昏",
                "composition": {"shot_type": "Medium Shot", "lighting": "暖光", "ambiance": "薄雾"},
            },
        }
        out = storyboard_image_prompt(segment, "真人电视剧风格", "Soft light", "segment_id")

        assert out.count("Style:") == 1
        assert out.startswith("Style: 真人电视剧风格\nVisual style: Soft light")

    def test_unstructured_prompt_keeps_one_style_line(self) -> None:
        segment = {"segment_id": "E1S02", "image_prompt": "村口黄昏的长镜头"}
        out = storyboard_image_prompt(segment, "真人电视剧风格", "", "segment_id")

        assert out.count("Style:") == 1
        assert "\n\n村口黄昏的长镜头\n\n" in out
        assert out.endswith("\n\nAvoid: 水印、多余文字、Logo")

    def test_missing_prompt_is_refused(self) -> None:
        with pytest.raises(ValueError, match="E1S03"):
            storyboard_image_prompt({"segment_id": "E1S03", "image_prompt": None}, "", "", "segment_id")


async def test_image_batch_targets_segments_without_a_usable_storyboard(
    board_projects: ProjectManager, board_queue: GenerationQueue
) -> None:
    done = _with_storyboard(board_projects, "E1S01")
    stale = _with_storyboard(board_projects, "E1S02")
    _write_episode(
        board_projects,
        [
            done,
            stale,
            _segment("E1S03"),
            _segment("E1S04", image_prompt=None),
            _segment("E1S05", characters_in_segment=["无名氏"]),
            _segment("E1S06", segment_break=True),
        ],
    )
    _claim_storyboards(board_projects, "E1S01")
    _claim_stale(board_projects, ArtifactKey.episode_storyboard(1, "E1S02"), "storyboards/scene_E1S02.png")

    _loaded, plan = await load_storyboard_image_batch_plan(
        board_projects, board_queue, project_name=PROJECT, episode_id=1, user_id=DEFAULT_USER_ID
    )

    assert plan.new_task_ids == ["E1S03", "E1S06"]
    assert plan.skips == (
        BatchSkip("E1S04", BatchSkipReason.MISSING_PROMPT),
        BatchSkip("E1S05", BatchSkipReason.REFERENCE_UNAVAILABLE),
    )
    # E1S03 接在上一张分镜图之后，走图生图；章节切分点另起一链，没有参考图时走文生图。
    assert dict(plan.lanes) == {"E1S03": "i2i", "E1S06": "t2i"}


async def test_image_batch_reports_a_segment_already_generating_and_submits_the_rest(
    board_projects: ProjectManager, board_queue: GenerationQueue
) -> None:
    _write_episode(board_projects, [_segment("E1S01"), _segment("E1S02"), _segment("E1S03", segment_break=True)])
    await board_queue.enqueue_task(
        project_name=PROJECT,
        task_type="storyboard",
        media_type="image",
        resource_id="E1S01",
        payload={"prompt": "村口黄昏"},
        script_file="episode_1.json",
        source="webui",
        user_id=DEFAULT_USER_ID,
    )

    _loaded, plan = await load_storyboard_image_batch_plan(
        board_projects, board_queue, project_name=PROJECT, episode_id=1, user_id=DEFAULT_USER_ID
    )
    assert plan.new_task_ids == ["E1S02", "E1S03"]
    assert plan.skips == (BatchSkip("E1S01", BatchSkipReason.GENERATING),)

    batch, enqueued, failures = await submit_storyboard_image_batch(
        plan, project_name=PROJECT, queue=board_queue, source="webui", user_id=DEFAULT_USER_ID
    )

    assert failures == []
    assert {task.resource_id for task in enqueued} == {"E1S01", "E1S02", "E1S03"}
    assert next(task for task in enqueued if task.resource_id == "E1S01").deduped
    assert [member.unit_id for member in batch.members] == ["E1S01", "E1S02", "E1S03"]


async def test_image_batch_rejects_an_unknown_episode_and_a_reference_project(
    board_projects: ProjectManager, board_queue: GenerationQueue
) -> None:
    _write_episode(board_projects, [_segment("E1S01")])
    with pytest.raises(StoryboardBatchEpisodeNotFound):
        await load_storyboard_image_batch_plan(
            board_projects, board_queue, project_name=PROJECT, episode_id=9, user_id=DEFAULT_USER_ID
        )

    board_projects.update_project(PROJECT, lambda project: project.update({"generation_mode": "reference_video"}))
    with pytest.raises(StoryboardBatchRouteMismatch):
        await load_storyboard_image_batch_plan(
            board_projects, board_queue, project_name=PROJECT, episode_id=1, user_id=DEFAULT_USER_ID
        )


async def test_video_batch_submits_only_the_half_that_has_storyboards(
    board_projects: ProjectManager, board_queue: GenerationQueue
) -> None:
    first, second = _with_storyboard(board_projects, "E1S01"), _with_storyboard(board_projects, "E1S02")
    _write_episode(board_projects, [first, second, _segment("E1S03"), _segment("E1S04")])
    _claim_storyboards(board_projects, "E1S01", "E1S02")

    plan = await load_storyboard_video_batch_plan(
        board_projects,
        board_queue,
        project_name=PROJECT,
        episode_id=1,
        user_id=DEFAULT_USER_ID,
        video_request_facts=make_video_request_facts(),
    )

    assert plan.target_ids == ["E1S01", "E1S02"]
    assert plan.skips == (
        BatchSkip("E1S03", BatchSkipReason.MISSING_STORYBOARD),
        BatchSkip("E1S04", BatchSkipReason.MISSING_STORYBOARD),
    )
    assert plan.admission.decision is BatchAdmissionDecision.ADMITTED

    batch, enqueued, failures = await submit_storyboard_video_batch(
        plan, project_name=PROJECT, queue=board_queue, source="webui", user_id=DEFAULT_USER_ID
    )

    assert failures == []
    assert sorted(task.resource_id for task in enqueued) == ["E1S01", "E1S02"]
    assert [member.unit_id for member in batch.members] == ["E1S01", "E1S02"]
    tasks = await board_queue.get_active_tasks_for_resources(
        project_name=PROJECT,
        task_type="video",
        resource_ids=["E1S01", "E1S02", "E1S03"],
        script_file="episode_1.json",
        user_id=DEFAULT_USER_ID,
    )
    assert sorted(task["resource_id"] for task in tasks) == ["E1S01", "E1S02"]


async def test_video_batch_leaves_stale_videos_out_and_lists_prompt_and_generating_skips(
    board_projects: ProjectManager, board_queue: GenerationQueue
) -> None:
    stale = _with_storyboard(board_projects, "E1S01")
    stale["generated_assets"]["video_clip"] = "videos/scene_E1S01.mp4"
    _write_media(board_projects, "videos/scene_E1S01.mp4")
    no_prompt = _with_storyboard(board_projects, "E1S02")
    no_prompt["video_prompt"] = None
    busy = _with_storyboard(board_projects, "E1S03")
    _write_episode(board_projects, [stale, no_prompt, busy])
    _claim_storyboards(board_projects, "E1S01", "E1S02", "E1S03")
    _claim_stale(board_projects, ArtifactKey.episode_video(1, "E1S01"), "videos/scene_E1S01.mp4")
    await board_queue.enqueue_task(
        project_name=PROJECT,
        task_type="video",
        media_type="video",
        resource_id="E1S03",
        payload={"prompt": "镜头平移"},
        script_file="episode_1.json",
        source="webui",
        user_id=DEFAULT_USER_ID,
    )

    plan = await load_storyboard_video_batch_plan(
        board_projects,
        board_queue,
        project_name=PROJECT,
        episode_id=1,
        user_id=DEFAULT_USER_ID,
        video_request_facts=make_video_request_facts(),
    )

    assert plan.target_ids == []
    assert plan.skips == (
        BatchSkip("E1S02", BatchSkipReason.MISSING_PROMPT),
        BatchSkip("E1S03", BatchSkipReason.GENERATING),
    )


async def test_video_batch_is_refused_whole_when_one_target_fails_admission(
    board_projects: ProjectManager, board_queue: GenerationQueue
) -> None:
    board_projects.add_character(PROJECT, "Alice", "勇敢的少女")
    healthy = _with_storyboard(board_projects, "E1S01")
    gap = _with_storyboard(board_projects, "E1S02")
    _write_episode(board_projects, [healthy, gap])
    _claim_storyboards(board_projects, "E1S01", "E1S02")
    # 分镜图生成之后才补的引用：Alice 还没有资产图，这一条的视频不能提交。
    with board_projects.locked_script(PROJECT, "episode_1.json") as script:
        script["segments"][1]["characters_in_segment"] = ["Alice"]

    plan = await load_storyboard_video_batch_plan(
        board_projects,
        board_queue,
        project_name=PROJECT,
        episode_id=1,
        user_id=DEFAULT_USER_ID,
        video_request_facts=make_video_request_facts(),
    )

    assert plan.admission.decision is BatchAdmissionDecision.BLOCKED
    refused = {unit.unit_id: [problem.code for problem in unit.problems] for unit in plan.admission.tickets}
    assert refused["E1S02"] == ["reference_asset_missing"]
    with pytest.raises(ValueError, match="整批准入未通过"):
        await submit_storyboard_video_batch(
            plan, project_name=PROJECT, queue=board_queue, source="webui", user_id=DEFAULT_USER_ID
        )
    tasks = await board_queue.get_active_tasks_for_resources(
        project_name=PROJECT,
        task_type="video",
        resource_ids=["E1S01", "E1S02"],
        script_file="episode_1.json",
        user_id=DEFAULT_USER_ID,
    )
    assert tasks == []


async def test_video_batch_cost_sums_each_target_at_its_planned_duration(
    board_projects: ProjectManager, board_queue: GenerationQueue, session_factory
) -> None:
    short = _with_storyboard(board_projects, "E1S01")
    long = _with_storyboard(board_projects, "E1S02")
    long["duration_seconds"] = 8
    _write_episode(board_projects, [short, long])
    _claim_storyboards(board_projects, "E1S01", "E1S02")
    facts = make_video_request_facts()

    plan = await load_storyboard_video_batch_plan(
        board_projects,
        board_queue,
        project_name=PROJECT,
        episode_id=1,
        user_id=DEFAULT_USER_ID,
        video_request_facts=facts,
    )
    estimated = await estimate_storyboard_video_batch_cost(plan, session_factory)

    async with session_factory() as session:
        price = await CustomProviderRepository(session).resolve_price(facts.provider_id, facts.model_id)
    expected = [quote_video_request_from_price(VideoRequestCostFacts(facts, seconds), price) for seconds in (4, 8)]
    assert estimated == {expected[0].currency: pytest.approx(expected[0].amount + expected[1].amount)}
    assert expected[1].amount > expected[0].amount > 0


async def test_video_batch_cost_is_unknown_when_a_target_has_no_planned_duration(
    board_projects: ProjectManager, board_queue: GenerationQueue, session_factory
) -> None:
    unplanned = _with_storyboard(board_projects, "E1S01")
    unplanned.pop("duration_seconds")
    _write_episode(board_projects, [unplanned])
    _claim_storyboards(board_projects, "E1S01")

    plan = await load_storyboard_video_batch_plan(
        board_projects,
        board_queue,
        project_name=PROJECT,
        episode_id=1,
        user_id=DEFAULT_USER_ID,
        video_request_facts=make_video_request_facts(),
    )

    assert plan.target_ids == ["E1S01"]
    assert await estimate_storyboard_video_batch_cost(plan, session_factory) is None


async def test_video_keyframe_limit_blocks_the_whole_batch_before_queueing(
    board_projects: ProjectManager, board_queue: GenerationQueue
) -> None:
    healthy = _with_storyboard(board_projects, "E1S01")
    current = _with_storyboard(board_projects, "E1S02")
    grid = confirmed_grid(board_projects.get_project_path(PROJECT), "E1S02", rows=3, cols=3)
    current["generated_assets"] = {
        "storyboard_image": grid.composite_path,
        "shot_grid": {
            "grid_id": grid.id,
            "rows": grid.rows,
            "cols": grid.cols,
            "composite_path": grid.composite_path,
            "cell_paths": grid.cell_paths,
        },
    }
    _write_episode(board_projects, [healthy, current])
    _claim_storyboards(board_projects, "E1S01", "E1S02")
    plan = await load_storyboard_video_batch_plan(
        board_projects,
        board_queue,
        project_name=PROJECT,
        episode_id=1,
        user_id=DEFAULT_USER_ID,
        video_request_facts=make_video_request_facts(max_reference_images=3),
    )
    assert plan.admission.decision is BatchAdmissionDecision.BLOCKED
    refused = next(ticket for ticket in plan.admission.tickets if ticket.unit_id == "E1S02")
    assert [problem.code for problem in refused.problems] == ["video_reference_images_exceeded"]
    assert refused.problems[0].params["count"] == 7
    with pytest.raises(ValueError, match="整批准入未通过"):
        await submit_storyboard_video_batch(
            plan, project_name=PROJECT, queue=board_queue, source="webui", user_id=DEFAULT_USER_ID
        )
    assert (
        await board_queue.get_active_tasks_for_resources(
            project_name=PROJECT,
            task_type="video",
            resource_ids=["E1S01", "E1S02"],
            script_file="episode_1.json",
            user_id=DEFAULT_USER_ID,
        )
        == []
    )


async def test_admission_reprojects_previous_frame_and_count_for_a_selected_subset(
    board_projects: ProjectManager, board_queue: GenerationQueue
) -> None:
    project_path = board_projects.get_project_path(PROJECT)
    prior = _with_storyboard(board_projects, "E1S01")
    current = _with_storyboard(board_projects, "E1S02")
    grid = confirmed_grid(project_path, "E1S02")
    current["generated_assets"] = {
        "storyboard_image": grid.composite_path,
        "shot_grid": {"composite_path": grid.composite_path, "cell_paths": grid.cell_paths},
    }
    _write_episode(board_projects, [prior, current])
    _claim_storyboards(board_projects, "E1S01", "E1S02")
    facts = make_video_request_facts(max_reference_images=2)
    plan = await load_storyboard_video_batch_plan(
        board_projects,
        board_queue,
        project_name=PROJECT,
        episode_id=1,
        user_id=DEFAULT_USER_ID,
        video_request_facts=facts,
    )
    assert plan.admission.decision is BatchAdmissionDecision.ADMITTED
    spec = next(spec for spec in plan.specs if spec.resource_id == "E1S02")
    assert spec.payload["storyboard_reference_count"] == 2
    _write_media(board_projects, "end_frames/scene_E1S01.png")
    prior, current = board_projects.load_script(PROJECT, "episode_1.json")["segments"]
    prior["end_frame_image"] = "end_frames/scene_E1S01.png"
    admission = await admit_storyboard_video_request(
        project_name=PROJECT,
        project=board_projects.load_project(PROJECT),
        project_dir=project_path,
        script_file="episode_1.json",
        items=[current],
        storyboard_items=[prior, current],
        id_field="segment_id",
        specs=[spec],
        operation="generate_videos",
        selection=GenerationSelectionMode.EXPLICIT,
        extra_tickets=[],
        user_id=DEFAULT_USER_ID,
        queue=board_queue,
        video_request_facts=facts,
        content_mode="narration",
    )
    assert admission.decision is BatchAdmissionDecision.BLOCKED
    assert admission.unit_ids == ("E1S02",)
    assert admission.tickets[0].problems[0].params["count"] == 3
    assert "previous shot" in spec.payload["prompt"]
    assert spec.payload["storyboard_reference_count"] == 3


# --- 参考素材 -----------------------------------------------------------------


async def test_reference_audio_limit_blocks_every_target_before_queueing(
    board_projects: ProjectManager, board_queue: GenerationQueue
) -> None:
    project_path = board_projects.get_project_path(PROJECT)
    for name in ("Alice", "Bob"):
        board_projects.add_character(PROJECT, name, f"{name}的外观")
        audio = project_path / f"characters/refs_audio/{name}.wav"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(wav_bytes(1))
    board_projects.update_project(
        PROJECT,
        lambda project: project.update(
            content_mode="drama",
            character_voice_binding="reference_audio",
            characters={
                name: {"description": f"{name}的外观", "reference_audio": f"characters/refs_audio/{name}.wav"}
                for name in ("Alice", "Bob")
            },
        ),
    )
    scenes = [
        {
            "scene_id": scene_id,
            "duration_seconds": 4,
            "image_prompt": "门外远景",
            "video_prompt": {"action": "两人交谈" if speakers else "门外空镜", "camera_motion": "Static"},
            "characters_in_scene": [],
            "scenes": [],
            "props": [],
            "utterances": [{"kind": "dialogue", "speaker": name, "text": "走吧。"} for name in speakers],
            "generated_assets": {"storyboard_image": f"storyboards/scene_{scene_id}.png"},
        }
        for scene_id, speakers in (("E1S01", ()), ("E1S02", ("Bob", "Alice", "Bob")))
    ]
    for scene in scenes:
        _write_media(board_projects, scene["generated_assets"]["storyboard_image"])
    scripts = project_path / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "episode_1.json").write_text(
        json.dumps({"episode": 1, "content_mode": "drama", "scenes": scenes}, ensure_ascii=False), encoding="utf-8"
    )
    board_projects.add_episode(PROJECT, 1, "第一集", "scripts/episode_1.json")
    _claim_storyboards(board_projects, "E1S01", "E1S02")
    plan = await load_storyboard_video_batch_plan(
        board_projects,
        board_queue,
        project_name=PROJECT,
        episode_id=1,
        user_id=DEFAULT_USER_ID,
        video_request_facts=make_video_request_facts(voice_consistency="native", max_reference_audio_count=1),
    )
    assert plan.admission.decision is BatchAdmissionDecision.BLOCKED
    refused = next(ticket for ticket in plan.admission.tickets if ticket.unit_id == "E1S02")
    assert [problem.code for problem in refused.problems] == ["video_reference_audio_exceeded"]
    assert refused.problems[0].params["count"] == 2
    assert "Bob: use the voice in reference audio 1" in plan.specs[1].payload["prompt"]
    assert "Alice: use the voice in reference audio 2" in plan.specs[1].payload["prompt"]
    with pytest.raises(ValueError, match="整批准入未通过"):
        await submit_storyboard_video_batch(
            plan, project_name=PROJECT, queue=board_queue, source="webui", user_id=DEFAULT_USER_ID
        )
    assert (
        await board_queue.get_active_tasks_for_resources(
            project_name=PROJECT,
            task_type="video",
            resource_ids=["E1S01", "E1S02"],
            script_file="episode_1.json",
            user_id=DEFAULT_USER_ID,
        )
        == []
    )


def _save_grid(board_projects: ProjectManager, segment_id: str, **overrides: object) -> ShotGrid:
    """落一条宫格记录：默认 2×2 草稿，逐格文本从 scene 现解析出来的两段。"""

    grid = ShotGrid.create(episode=1, script_file="episode_1.json", shot_id=segment_id, rows=2, cols=2)
    grid.prompt = _sequence("小明推开院门", "小明望向远方")
    for key, value in overrides.items():
        setattr(grid, key, value)
    ShotGridManager(board_projects.get_project_path(PROJECT)).save(grid)
    return grid


async def test_shot_grid_batch_targets_shots_without_a_confirmed_grid(
    board_projects: ProjectManager, board_queue: GenerationQueue
) -> None:
    first = _segment("E1S01", image_prompt=_scene(("远景", "小明站在院门口")))
    confirmed = _segment("E1S02", image_prompt=_scene(("全景", "小明走进院子")))
    no_scene = _segment("E1S03", image_prompt={"scene": ""})
    not_sequence = _segment("E1S04", image_prompt={"scene": "小明站在院门口，只有一句话"})
    unregistered = _segment("E1S05", image_prompt=_scene(("中景", "小明抬起头")), characters_in_segment=["无名氏"])
    chained = _segment("E1S06", image_prompt=_scene(("近景", "小明伸出手")))
    _write_episode(board_projects, [first, confirmed, no_scene, not_sequence, unregistered, chained])
    _save_grid(board_projects, "E1S02", status="confirmed")

    _loaded, plan = await load_shot_grid_batch_plan(
        board_projects, board_queue, project_name=PROJECT, episode_id=1, user_id=DEFAULT_USER_ID
    )

    assert plan.new_task_ids == ["E1S01", "E1S06"]
    assert plan.skips == (
        BatchSkip("E1S02", BatchSkipReason.CONFIRMED),
        BatchSkip("E1S03", BatchSkipReason.MISSING_PROMPT),
        BatchSkip("E1S04", BatchSkipReason.MISSING_PROMPT),
        BatchSkip("E1S05", BatchSkipReason.REFERENCE_UNAVAILABLE),
    )
    # 首张没有参考图走文生图；接在上一张之后、或引用了资产走图生图。
    assert dict(plan.lanes) == {"E1S01": "t2i", "E1S06": "i2i"}


async def test_shot_grid_batch_writes_records_from_scene_and_submits_one_batch(
    board_projects: ProjectManager, board_queue: GenerationQueue
) -> None:
    first = _segment("E1S01", image_prompt=_scene(("远景", "小明站在院门口"), ("全景", "小明推开院门")))
    second = _segment("E1S02", segment_break=True, image_prompt=_scene(("特写", "门环上的锈迹")))
    _write_episode(board_projects, [first, second])

    _loaded, plan = await load_shot_grid_batch_plan(
        board_projects, board_queue, project_name=PROJECT, episode_id=1, user_id=DEFAULT_USER_ID
    )
    assert plan.target_ids == ["E1S01", "E1S02"]

    batch, enqueued, failures = await submit_shot_grid_batch(
        plan,
        project_name=PROJECT,
        project_path=board_projects.get_project_path(PROJECT),
        queue=board_queue,
        source="webui",
        user_id=DEFAULT_USER_ID,
    )

    assert failures == []
    assert batch.operation == SHOT_GRID_BATCH_OPERATION
    assert len({task.task_id for task in enqueued}) == 2
    # 一个批次、每个分镜一个宫格任务：resource_id 是宫格 id，unit_id 是分镜 id。
    assert {task.unit_id for task in enqueued} == {"E1S01", "E1S02"}
    assert len(batch.batch_id) > 0
    manager = ShotGridManager(board_projects.get_project_path(PROJECT))
    for unit_id, scene in (("E1S01", first), ("E1S02", second)):
        grid = manager.find(script_file="episode_1.json", shot_id=unit_id)
        assert grid is not None
        assert (grid.rows, grid.cols) == (SHOT_GRID_BATCH_ROWS, SHOT_GRID_BATCH_COLS)
        assert grid.status == "generating"
        assert grid.composite_path is None
        assert grid.task_id == next(task.task_id for task in enqueued if task.unit_id == unit_id)
        assert grid.image_basis is None
        # 逐格文本从 scene 现解析，留证与拼装出口读到的序列同一个。
        frames = parse_scene_shots(scene["image_prompt"]["scene"]).shots
        assert len(grid.cells) == SHOT_GRID_BATCH_ROWS * SHOT_GRID_BATCH_COLS
        assert [cell.prompt for cell in grid.cells[: len(frames)]] == [frame.text for frame in frames]
        assert [cell.continuity_notes for cell in grid.cells[: len(frames)]] == [frame.shot_size for frame in frames]
        # 镜头数不足格数时尾格延续最后一个镜头，不留空格（ADR 0098 D2 建议分支）。
        assert all("延续" in cell.continuity_notes for cell in grid.cells[len(frames) :])
    tasks = await board_queue.get_active_tasks_for_resources(
        project_name=PROJECT,
        task_type="shot_grid",
        resource_ids=[grid.id for grid in manager.list_all()],
        script_file="episode_1.json",
        user_id=DEFAULT_USER_ID,
    )
    assert len(tasks) == 2


async def test_shot_grid_batch_skips_a_running_task_without_rewriting_the_record(
    board_projects: ProjectManager, board_queue: GenerationQueue
) -> None:
    busy = _segment("E1S01", image_prompt=_scene(("远景", "小明站在院门口")))
    _write_episode(board_projects, [busy, _segment("E1S02", image_prompt=_scene(("全景", "小明望向远方")))])
    grid = _save_grid(board_projects, "E1S01", status="generating", task_id=None)
    await board_queue.enqueue_task(
        project_name=PROJECT,
        task_type="shot_grid",
        media_type="image",
        resource_id=grid.id,
        payload={"shot_grid_id": grid.id, "script_file": "episode_1.json"},
        script_file="episode_1.json",
        source="webui",
        user_id=DEFAULT_USER_ID,
    )

    _loaded, plan = await load_shot_grid_batch_plan(
        board_projects, board_queue, project_name=PROJECT, episode_id=1, user_id=DEFAULT_USER_ID
    )

    assert plan.new_task_ids == ["E1S02"]
    assert plan.skips == (BatchSkip("E1S01", BatchSkipReason.GENERATING),)

    batch, enqueued, failures = await submit_shot_grid_batch(
        plan,
        project_name=PROJECT,
        project_path=board_projects.get_project_path(PROJECT),
        queue=board_queue,
        source="webui",
        user_id=DEFAULT_USER_ID,
    )

    assert failures == []
    assert [task.unit_id for task in enqueued] == ["E1S02"]
    kept = ShotGridManager(board_projects.get_project_path(PROJECT)).get(grid.id)
    assert kept is not None
    assert kept.status == "generating"
    assert kept.task_id == grid.task_id
    assert [member.unit_id for member in batch.members] == ["E1S02"]


async def test_shot_grid_batch_rejects_an_unknown_episode_and_a_reference_project(
    board_projects: ProjectManager, board_queue: GenerationQueue
) -> None:
    _write_episode(board_projects, [_segment("E1S01", image_prompt=_scene(("远景", "小明站在院门口")))])
    with pytest.raises(StoryboardBatchEpisodeNotFound):
        await load_shot_grid_batch_plan(
            board_projects, board_queue, project_name=PROJECT, episode_id=9, user_id=DEFAULT_USER_ID
        )

    board_projects.update_project(PROJECT, lambda project: project.update({"generation_mode": "reference_video"}))
    with pytest.raises(StoryboardBatchRouteMismatch):
        await load_shot_grid_batch_plan(
            board_projects, board_queue, project_name=PROJECT, episode_id=1, user_id=DEFAULT_USER_ID
        )
