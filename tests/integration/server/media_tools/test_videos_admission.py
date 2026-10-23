"""generate_videos 的整批准入：全有或全无、逐单元结论与按调用方隔离的在途任务。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from lib.db.models.user import User
from server.tool_runtime import CallerContext, ToolOutcome
from tests.factories import make_video_request_facts
from tests.integration.server.agent_tool_support import (
    ToolHarness,
    prepare_factory_script,
    read_generation_result,
    run_generate_videos,
)


@pytest.fixture(autouse=True)
def storyboard_request_facts(set_admission_video_request_facts) -> None:
    facts = make_video_request_facts(provider_id="fake", model_id="fake-video", audio_switch_controllable=True)
    set_admission_video_request_facts(facts)


@pytest.fixture
def factory_ctx(fake_ctx: ToolHarness) -> ToolHarness:
    return prepare_factory_script(fake_ctx)


@pytest.fixture
def idle_factory_ctx(idle_fake_ctx: ToolHarness) -> ToolHarness:
    return prepare_factory_script(idle_fake_ctx)


_EPISODE_1 = {"scope": "episode", "episode_id": 1}
_ALL = {"scope": "all"}
# 越出项目根的成片路径：清单无从检查这份产物，它的状态既不是「缺失」也不是「可用」。
_UNREADABLE_CLIP = "../outside/E1S02.mp4"


def _admission_codes(out: ToolOutcome[Any]) -> dict[str, list[str]]:
    assert isinstance(out.value, dict)
    return {
        unit["unit_id"]: [problem["code"] for problem in unit["problems"]]
        for unit in out.value["batch_admission"]["units"]
    }


async def test_generate_videos_episode_scope_batch_is_all_or_nothing_when_a_unit_is_occupied(
    idle_factory_ctx: ToolHarness, concurrent_session_factory
) -> None:
    """在途任务冲突拦下整批：一个都不入队，其余 unit 报告自己是被谁扣下的。"""
    factory_ctx = idle_factory_ctx
    factory_ctx.pm.script_payload["scenes"] = [
        {"scene_id": "E1S01", "source_text": "第一段旁白。", "video_prompt": "第一镜"},
        {"scene_id": "E1S02", "source_text": "第二段旁白。", "video_prompt": "第二镜"},
    ]
    project_dir = factory_ctx.pm.get_project_path("demo")
    for segment_id in ("E1S01", "E1S02"):
        image = project_dir / "storyboards" / f"scene_{segment_id}.png"
        image.write_bytes(b"png")
        for item in factory_ctx.pm.script_payload["scenes"]:
            if item["scene_id"] == segment_id:
                item["generated_assets"] = {"storyboard_image": f"storyboards/scene_{segment_id}.png"}

    async with concurrent_session_factory() as session:
        session.add(User(id="tenant-user", username="tenant-user"))
        await session.commit()
    factory_ctx.caller = CallerContext(user_id="tenant-user", source="embedded")
    other_user = await factory_ctx.queue.enqueue_task(
        project_name="demo",
        task_type="video",
        media_type="video",
        resource_id="E1S01",
        script_file="episode_1.json",
    )
    occupied = await factory_ctx.queue.enqueue_task(
        project_name="demo",
        task_type="video",
        media_type="video",
        resource_id="E1S02",
        script_file="episode_1.json",
        user_id="tenant-user",
    )
    caller_active = await factory_ctx.queue.get_active_tasks_for_resources(
        project_name="demo",
        task_type="video",
        resource_ids=["E1S01", "E1S02"],
        script_file="episode_1.json",
        user_id="tenant-user",
    )
    assert (await factory_ctx.queue.get_task(other_user["task_id"])) is not None
    assert [task["task_id"] for task in caller_active] == [occupied["task_id"]]

    out = await run_generate_videos(factory_ctx, _EPISODE_1)

    other_task = await factory_ctx.queue.get_task(other_user["task_id"])
    occupied_task = await factory_ctx.queue.get_task(occupied["task_id"])
    assert other_task is not None
    assert other_task["user_id"] == "default"
    assert occupied_task is not None
    assert occupied_task["user_id"] == "tenant-user"
    result = read_generation_result(out)
    assert sorted(result.blocked) == ["E1S01", "E1S02"]
    codes = {item.unit_id: item.problem.code for item in result.items if item.problem is not None}
    assert codes["E1S02"] == "generation_active_task_conflict"
    assert codes["E1S01"] == "generation_batch_admission_withheld"


_WEB_SCRIPT_FILE = "scripts/episode_1.json"  # Web 路由传账本原值；Agent 工具传纯文件名


def _enqueue_video_as_web(factory_ctx: ToolHarness) -> Any:
    return factory_ctx.queue.enqueue_task(
        project_name="demo",
        task_type="video",
        media_type="video",
        resource_id="E1S01",
        payload={},
        script_file=_WEB_SCRIPT_FILE,
        provider_id="fake",
    )


async def test_generate_videos_refuses_a_scene_the_web_route_already_queued(idle_factory_ctx: ToolHarness) -> None:
    """Web 先入队同一单元：Agent 整集生成在准入处被「已在队列中」拦下，不再入队计费。"""
    factory_ctx = idle_factory_ctx
    queued = await _enqueue_video_as_web(factory_ctx)
    enqueue = AsyncMock(return_value=([], []))

    out = await run_generate_videos(factory_ctx, _EPISODE_1, batch_waiter=enqueue)

    enqueue.assert_not_awaited()
    result = read_generation_result(out)
    assert result.blocked == ["E1S01"]
    codes = {item.unit_id: item.problem.code for item in result.items if item.problem is not None}
    assert codes["E1S01"] == "generation_active_task_conflict"
    active = await factory_ctx.queue.get_active_tasks_for_resources(
        project_name="demo", task_type="video", resource_ids=["E1S01"], script_file="episode_1.json"
    )
    assert [task["task_id"] for task in active] == [queued["task_id"]]


async def test_web_route_enqueue_dedupes_to_the_task_the_agent_tool_already_queued(
    idle_factory_ctx: ToolHarness,
) -> None:
    """Agent 先入队同一单元：Web 路由随后的入队落在同一个任务上，只产生一个任务。"""
    factory_ctx = idle_factory_ctx
    agent_task_ids: list[str] = []

    async def enqueue_as_agent(*, specs, **_batch_kwargs):
        for spec in specs:
            queued = await factory_ctx.queue.enqueue_task(
                project_name="demo",
                task_type=spec.task_type,
                media_type=spec.media_type,
                resource_id=spec.resource_id,
                payload=spec.payload,
                script_file=spec.script_file,
                provider_id="fake",
            )
            agent_task_ids.append(queued["task_id"])
        return [], []

    out = await run_generate_videos(factory_ctx, _EPISODE_1, batch_waiter=enqueue_as_agent)

    assert out.problem is None, out
    assert len(agent_task_ids) == 1
    web = await _enqueue_video_as_web(factory_ctx)
    assert web["deduped"] is True
    assert web["task_id"] == agent_task_ids[0]


async def test_generate_videos_all_scope_creates_zero_tasks_when_one_artifact_state_is_unreadable(
    factory_ctx: ToolHarness,
) -> None:
    """产物状态读不出的场景属于这次请求：它带着自己的问题进准入，整批停下，健康的场景不入队计费。"""
    factory_ctx.pm.script_payload["scenes"].append(
        {
            "scene_id": "E1S02",
            "image_prompt": "山道清晨",
            "source_text": "清晨的山道上落着薄雾。",
            "video_prompt": {"action": "镜头推近", "camera_motion": "Push", "ambiance_audio": "鸟鸣"},
            "duration_seconds": 4,
            "generated_assets": {"storyboard_image": "storyboards/scene_E1S02.png", "video_clip": _UNREADABLE_CLIP},
        }
    )
    (factory_ctx.project_path / "storyboards").mkdir(parents=True, exist_ok=True)
    (factory_ctx.project_path / "storyboards" / "scene_E1S02.png").write_bytes(b"\x89PNG")
    enqueue = AsyncMock(return_value=([], []))

    out = await run_generate_videos(factory_ctx, _ALL, batch_waiter=enqueue)

    assert out.value["batch_admission"]["decision"] == "blocked"
    enqueue.assert_not_awaited()
    codes = _admission_codes(out)
    assert codes["E1S02"] == ["generation_artifact_state_unavailable"]
    assert codes["E1S01"] == ["generation_batch_admission_withheld"]


async def test_generate_videos_all_scope_blocks_a_reference_gap_and_withholds_the_batch(
    factory_ctx: ToolHarness,
) -> None:
    """图生视频的整批准入与单条提交同判：引用有缺口的场景阻断，整批一个都不入队。"""

    factory_ctx.pm.script_payload["scenes"].append(
        {
            "scene_id": "E1S02",
            "image_prompt": "山道清晨",
            "source_text": "清晨的山道上落着薄雾。",
            "video_prompt": {"action": "镜头推近", "camera_motion": "Push", "ambiance_audio": "鸟鸣"},
            "duration_seconds": 4,
            "characters_in_scene": ["无名氏"],
            "generated_assets": {"storyboard_image": "storyboards/scene_E1S02.png"},
        }
    )
    (factory_ctx.project_path / "storyboards").mkdir(parents=True, exist_ok=True)
    (factory_ctx.project_path / "storyboards" / "scene_E1S02.png").write_bytes(b"\x89PNG")

    enqueue = AsyncMock(return_value=([], []))

    out = await run_generate_videos(factory_ctx, _ALL, batch_waiter=enqueue)

    assert out.value["batch_admission"]["decision"] == "blocked"
    enqueue.assert_not_awaited()
    units = {unit["unit_id"]: unit for unit in out.value["batch_admission"]["units"]}
    assert [problem["code"] for problem in units["E1S02"]["problems"]] == ["reference_asset_unregistered"]
    assert units["E1S02"]["problems"][0]["action"] == "generate_dependency"
    assert units["E1S02"]["problems"][0]["params"]["missing_text"] == "无名氏"
    assert [problem["code"] for problem in units["E1S01"]["problems"]] == ["generation_batch_admission_withheld"]


async def test_generate_videos_all_scope_reports_an_all_unreadable_selection_as_blocked(
    factory_ctx: ToolHarness,
) -> None:
    """全部目标的产物状态都读不出时不能报成空的成功：那会把每一条状态问题都藏起来。"""
    factory_ctx.pm.script_payload["scenes"][0]["generated_assets"]["video_clip"] = _UNREADABLE_CLIP
    enqueue = AsyncMock(return_value=([], []))

    out = await run_generate_videos(factory_ctx, _ALL, batch_waiter=enqueue)

    assert out.value["batch_admission"]["decision"] == "blocked"
    enqueue.assert_not_awaited()
    assert _admission_codes(out) == {"E1S01": ["generation_artifact_state_unavailable"]}


async def test_generate_factory_episode_refuses_a_non_scalar_unit_id(
    factory_ctx: ToolHarness,
) -> None:
    """整集视频生成遇到非标量 scene_id：它按位置记名拒收，健康的兄弟条目不会独自入队计费。"""

    script = factory_ctx.pm.script_payload
    healthy = script["scenes"][0]
    script["scenes"] = [{**healthy, "scene_id": ["S9"]}, healthy]
    factory_ctx.pm.script_payload = script
    enqueue = AsyncMock(return_value=([], []))

    out = await run_generate_videos(factory_ctx, _EPISODE_1, batch_waiter=enqueue)

    enqueue.assert_not_awaited()
    codes = _admission_codes(out)
    assert codes["items[0]"] == ["generation_unit_request_invalid"]
    assert healthy["scene_id"] in codes


async def test_generate_factory_scenes_refuses_a_duplicated_named_unit(
    factory_ctx: ToolHarness,
) -> None:
    """点名的 unit 在剧本里有两份：无从判定要做哪一条，整批停在建任务之前。"""

    script = factory_ctx.pm.script_payload
    script["scenes"] = [*script["scenes"], {**script["scenes"][0]}]
    factory_ctx.pm.script_payload = script
    duplicated_id = script["scenes"][0]["scene_id"]
    enqueue = AsyncMock(return_value=([], []))

    out = await run_generate_videos(
        factory_ctx, {"scope": "selected", "ids": [duplicated_id]}, batch_waiter=enqueue, force=True
    )

    enqueue.assert_not_awaited()
    assert _admission_codes(out) == {duplicated_id: ["generation_unit_request_invalid"]}
