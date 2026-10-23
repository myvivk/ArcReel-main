"""Tests for the ``generate_videos`` tool handler and its storyboard / reference admission helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lib.artifacts.version_manager import MANUAL_UPLOAD_VERSION_SOURCE, VersionManager
from lib.generation.generation_batch import GenerationBatchReadModel
from lib.project.project_schema import CURRENT_PROJECT_SCHEMA_VERSION
from lib.project.resource_paths import resource_relative_path
from lib.script.short_drama_source import short_drama_source_relative_path
from server.media_tools import videos as enqueue_videos_mod
from server.media_tools.context import generation_is_error
from server.tool_runtime import ToolOutcome
from tests.factories import make_video_request_facts
from tests.integration.server.agent_tool_support import (
    _CLAIMED_BASIS_DIGEST,
    ToolHarness,
    activate_unbound_project,
    fake_scene_batch,
    prepare_factory_script,
    read_generation_result,
    run_declared_tool,
    run_generate_videos,
)


@pytest.fixture(autouse=True)
def storyboard_request_facts(set_admission_video_request_facts) -> None:
    facts = make_video_request_facts(provider_id="fake", model_id="fake-video", audio_switch_controllable=True)
    set_admission_video_request_facts(facts)


def _factory_meta(ctx: ToolHarness) -> dict[str, str]:
    source = ctx.project_path / short_drama_source_relative_path(1)
    return {"source_revision": hashlib.sha256(source.read_bytes()).hexdigest()}


@pytest.fixture
def factory_ctx(fake_ctx: ToolHarness) -> ToolHarness:
    return prepare_factory_script(fake_ctx)


_EPISODE_1: dict[str, Any] = {"scope": "episode", "episode_id": 1}
_ALL: dict[str, Any] = {"scope": "all"}
# 越出项目根的成片路径：清单无从检查这份产物，它的状态既不是「缺失」也不是「可用」。
_UNREADABLE_CLIP = "../outside/E1S01.mp4"


def _scene(unit_id: str) -> dict[str, Any]:
    return {"scope": "scene", "ids": [unit_id]}


def _selected(*unit_ids: str) -> dict[str, Any]:
    return {"scope": "selected", "ids": list(unit_ids)}


def _is_error(out: ToolOutcome[Any]) -> bool:
    """两宿主置 isError 的同一判定：problem，或终态结果未全部成功（待确认档位不算）。"""

    return out.problem is not None or generation_is_error(out.value)


def _text(out: ToolOutcome[Any]) -> str:
    """调用方读到的人读文本：problem 的 detail，或终态结果的摘要。"""

    if out.problem is not None:
        return out.problem.detail
    assert isinstance(out.value, dict)
    return out.value["summary"]


def _recording_batch(enqueued: list[Any]):
    """记下每批入队的 spec，不产出任何终态。"""

    async def _batch(*, specs, **_batch_kwargs):
        enqueued.extend(specs)
        return [], []

    return _batch


def _select_manual_video(
    project_path: Path,
    *,
    resource_type: str,
    resource_id: str,
    content: bytes,
) -> str:
    artifact_path = resource_relative_path(resource_type, resource_id)
    staged = project_path / f".{resource_type}-{resource_id}.upload.mp4"
    staged.write_bytes(content)
    VersionManager(project_path).commit_staged_version(
        resource_type,
        resource_id,
        "",
        staged_file=staged,
        current_file=project_path / artifact_path,
        source=MANUAL_UPLOAD_VERSION_SOURCE,
    )
    return artifact_path


def _activated_project(project_dir: Path, storyboard_ids: dict[str, str] | None = None) -> dict[str, Any]:
    """构造一个已迁移到当前 schema 的项目，并把点名的分镜图登记进产物清单。

    直接调用入队构造函数的用例不经 pm，项目形态得在这里补齐：清单是读取已生成产物的
    唯一口径，没有登记的分镜图不能作为视频输入。
    """

    from lib.artifacts.artifact_manifest import (
        ArtifactKey,
        ArtifactManifest,
        ArtifactManifestEntry,
        ProjectArtifactManifestAdapter,
    )

    project: dict[str, Any] = {
        "schema_version": CURRENT_PROJECT_SCHEMA_VERSION,
        "content_mode": "drama",
        "generation_mode": "storyboard",
        "episodes": [{"episode": 1, "script_file": "scripts/episode_1.json"}],
    }
    (project_dir / "project.json").write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
    manifest = ArtifactManifest(ProjectArtifactManifestAdapter(project_dir))
    for resource_id, artifact_path in (storyboard_ids or {}).items():
        manifest.register_entry_transactionally(
            ArtifactKey.episode_storyboard(1, resource_id),
            ArtifactManifestEntry(artifact_path=artifact_path, basis_digest=_CLAIMED_BASIS_DIGEST),
        )
    return project


def _refused_problems(refused: list[Any]) -> dict[str, tuple[str, str]]:
    """Map each refused ticket's unit ID to its ``(code, action)`` pair."""

    return {ticket.unit_id: (problem.code, problem.action.value) for ticket in refused for problem in ticket.problems}


# ---------------------------------------------------------------------------
# generate_videos：请求校验
# ---------------------------------------------------------------------------


_VALID_REQUEST: dict[str, Any] = {
    "script": "episode_1.json",
    "target": _EPISODE_1,
}

# 项目级旁白交付配置的两种取值；视频请求对两者一视同仁。
_POST_PRODUCTION_PROJECT: dict[str, Any] = {"narration_delivery": "post_production"}
_USE_TTS_PROJECT: dict[str, Any] = {
    "narration_delivery": "use_tts",
    "audio_backend": "dashscope/qwen3-tts-flash",
    "narration_voice": "Cherry",
}


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"target": {"scope": "episode"}}, id="episode-without-number"),
        pytest.param({"target": {"scope": "episode", "episode_id": 1, "ids": ["E1S01"]}}, id="episode-with-ids"),
        pytest.param({"target": {"scope": "episode", "episode_id": 1, "extra": True}}, id="episode-unknown-field"),
        pytest.param({"target": {"scope": "all", "episode": 1}}, id="all-with-episode"),
        pytest.param({"target": {"scope": "all", "ids": ["E1S01"]}}, id="all-with-ids"),
        pytest.param({"target": {"scope": "scene", "ids": []}}, id="scene-without-id"),
        pytest.param({"target": {"scope": "scene", "ids": ["E1S01", "E1S02"]}}, id="scene-with-two-ids"),
        pytest.param({"target": {"scope": "scene", "ids": ["E1S01"], "episode": 1}}, id="scene-with-episode"),
        pytest.param({"target": {"scope": "selected"}}, id="selected-without-ids"),
        pytest.param({"target": {"scope": "selected", "ids": []}}, id="selected-with-empty-ids"),
        pytest.param({"target": _ALL, "force": True}, id="force-on-all"),
        pytest.param({"force": True}, id="force-on-episode"),
        pytest.param({"confirmed_request_duration_seconds": 0}, id="tier-zero"),
        pytest.param({"confirmed_request_duration_seconds": True}, id="tier-boolean"),
        pytest.param({"confirmed_request_duration_seconds": "12"}, id="tier-string"),
        pytest.param({"confirmed_request_durations": {"E1S01": 9.5}}, id="unit-tier-fraction"),
        pytest.param({"confirmed_request_durations": [8]}, id="unit-tiers-not-a-mapping"),
    ],
)
async def test_generate_videos_refuses_a_malformed_request_before_enqueuing(
    factory_ctx: ToolHarness, overrides: dict[str, Any]
) -> None:
    enqueue = AsyncMock(return_value=([], []))

    out = await run_declared_tool("generate_videos", factory_ctx, {**_VALID_REQUEST, **overrides}, batch_waiter=enqueue)

    assert out.problem is not None
    assert out.problem.code == "invalid_request"
    enqueue.assert_not_awaited()


@pytest.mark.parametrize("delivery", ["post_production", "use_tts"])
async def test_generate_videos_refuses_a_narration_delivery_without_enqueuing(
    factory_ctx: ToolHarness, delivery: str
) -> None:
    """旁白交付方式是项目配置：请求里带上它时整次调用被拒，指引说明视频按剧本计划时长请求。"""
    enqueue = AsyncMock(return_value=([], []))

    out = await run_declared_tool(
        "generate_videos", factory_ctx, {**_VALID_REQUEST, "narration_delivery": delivery}, batch_waiter=enqueue
    )

    assert out.problem is not None
    assert out.problem.code == "invalid_request"
    assert "narration_delivery" in out.problem.detail
    enqueue.assert_not_awaited()
    assert (await factory_ctx.queue.list_tasks(project_name="demo"))["items"] == []


@pytest.mark.parametrize("retired_param", sorted(enqueue_videos_mod._RETIRED_PARAMS))
async def test_generate_videos_refuses_a_retired_param_with_its_replacement(
    factory_ctx: ToolHarness, retired_param: str
) -> None:
    """已退役的参数名被拒，报错点名该参数并给出当下写法。"""

    out = await run_declared_tool("generate_videos", factory_ctx, {**_VALID_REQUEST, retired_param: "dummy"})

    assert out.problem is not None
    assert out.problem.code == "invalid_request"
    assert retired_param in out.problem.detail
    assert "已不存在" in out.problem.detail
    if retired_param in {"shot_ids", "unit_id", "unit_ids"}:
        assert "target.ids" in out.problem.detail


async def test_generate_videos_refuses_an_episode_target_that_is_not_the_scripts_episode(
    factory_ctx: ToolHarness,
) -> None:
    enqueue = AsyncMock(return_value=([], []))

    out = await run_generate_videos(factory_ctx, {"scope": "episode", "episode_id": 2}, batch_waiter=enqueue)

    assert out.problem is not None
    assert "不一致" in out.problem.detail
    enqueue.assert_not_awaited()


# ---------------------------------------------------------------------------
# generate_videos：分镜图生视频
# ---------------------------------------------------------------------------


async def test_generate_videos_episode_scope_happy(factory_ctx: ToolHarness) -> None:
    out = await run_generate_videos(factory_ctx, _EPISODE_1, batch_waiter=fake_scene_batch)

    assert not _is_error(out), out


async def test_generate_videos_episode_scope_skips_current_clip(factory_ctx: ToolHarness) -> None:
    """整集调用复用清单已认领的旧片段。"""

    project = factory_ctx.pm.project_payload
    project.update(
        {
            "schema_version": CURRENT_PROJECT_SCHEMA_VERSION,
            "generation_mode": "storyboard",
            "episodes": [{"episode": 1, "script_file": "scripts/episode_1.json"}],
        }
    )
    factory_ctx.pm.script_payload["episode"] = 1
    factory_ctx.pm.script_payload["scenes"][0]["generated_assets"] = {
        "storyboard_image": "storyboards/scene_E1S01.png",
        "video_clip": "videos/scene_E1S01.mp4",
    }

    clip = factory_ctx.project_path / "videos" / "scene_E1S01.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"rendered-video")
    enqueued: list[Any] = []

    out = await run_generate_videos(factory_ctx, _EPISODE_1, batch_waiter=_recording_batch(enqueued))

    assert not _is_error(out), out
    result = read_generation_result(out)
    assert enqueued == []
    assert result.succeeded == []
    assert [entry.unit_id for entry in result.skipped] == ["E1S01"]


async def test_generate_videos_episode_scope_blocks_a_clip_whose_manifest_state_is_unreadable(
    factory_ctx: ToolHarness,
) -> None:
    """整集调用里某片段的 Manifest 比对抛错（BLOCKED）时必须报 blocked，不能落入
    「既不可复用也不算 blocked」的空档而被当作缺失去付费重生——不可读不等于没有。"""

    project = factory_ctx.pm.project_payload
    project.update(
        {
            "schema_version": CURRENT_PROJECT_SCHEMA_VERSION,
            "generation_mode": "storyboard",
            "episodes": [{"episode": 1, "script_file": "scripts/episode_1.json"}],
        }
    )
    factory_ctx.pm.script_payload["episode"] = 1
    segments = factory_ctx.pm.script_payload["scenes"]
    segments[0]["generated_assets"] = {
        "storyboard_image": "storyboards/scene_E1S01.png",
        "video_clip": _UNREADABLE_CLIP,
    }

    enqueued: list[Any] = []

    out = await run_generate_videos(factory_ctx, _EPISODE_1, batch_waiter=_recording_batch(enqueued))

    result = read_generation_result(out)
    assert enqueued == []
    assert result.succeeded == []
    assert result.blocked == ["E1S01"]
    blocked_item = next(item for item in result.items if item.unit_id == "E1S01")
    assert blocked_item.problem is not None
    assert blocked_item.problem.code == "generation_artifact_state_unavailable"


async def test_generate_videos_episode_scope_rejects_unbound_active_script_before_enqueue(
    factory_ctx: ToolHarness,
) -> None:
    activate_unbound_project(factory_ctx)
    enqueue = AsyncMock(return_value=([], []))

    out = await run_generate_videos(factory_ctx, _EPISODE_1, batch_waiter=enqueue)

    assert out.problem is not None
    assert "not bound" in out.problem.detail
    enqueue.assert_not_awaited()


async def test_generate_videos_episode_scope_non_dict_generated_assets_does_not_abort_batch(
    factory_ctx: ToolHarness,
) -> None:
    """整集入队先按 generated_assets.video_clip 过滤已完成条目。容器被外部编辑损坏为非 dict
    时该过滤须按「未生成」处理，而不是在 pending 过滤阶段就抛未处理 AttributeError；随后该条目
    以自己的问题码拦住整批，本次不创建任何任务。"""
    project_dir = factory_ctx.pm.get_project_path("demo")
    (project_dir / "storyboards" / "scene_E1S02.png").write_bytes(b"png")
    factory_ctx.pm.script_payload["scenes"] = [
        {
            "scene_id": "E1S01",
            "source_text": "第一段旁白。",
            "video_prompt": "脏数据",
            "generated_assets": ["bad"],
        },
        {
            "scene_id": "E1S02",
            "source_text": "第二段旁白。",
            "video_prompt": "合法条目",
            "generated_assets": {"storyboard_image": "storyboards/scene_E1S02.png"},
        },
    ]
    enqueued: list[Any] = []

    out = await run_generate_videos(factory_ctx, _EPISODE_1, batch_waiter=_recording_batch(enqueued))

    # E1S01 的分镜图绑定不可用：整批准入不成立，零任务入队，合法条目也如实报告被搁置的原因。
    assert enqueued == []
    result = read_generation_result(out)
    assert sorted(result.blocked) == ["E1S01", "E1S02"]
    codes = {item.unit_id: item.problem.code for item in result.items if item.problem is not None}
    assert codes["E1S01"] == "generation_unit_input_unusable"
    assert codes["E1S02"] == "generation_batch_admission_withheld"
    assert _is_error(out)


async def test_generate_videos_episode_scope_error(factory_ctx: ToolHarness) -> None:
    factory_ctx.pm.script_payload = ({"content_mode": "drama", "scenes": [], "episode": 1}) | {
        "short_drama_meta": _factory_meta(factory_ctx)
    }

    out = await run_generate_videos(factory_ctx, _EPISODE_1)

    assert _is_error(out)


async def test_generate_videos_ignores_legacy_batch_checkpoint_files(factory_ctx: ToolHarness) -> None:
    checkpoint = factory_ctx.project_path / "videos" / ".checkpoint_ep1.json"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text("not-json", encoding="utf-8")

    out = await run_generate_videos(factory_ctx, _EPISODE_1, batch_waiter=fake_scene_batch)

    assert not _is_error(out), out
    assert checkpoint.read_text(encoding="utf-8") == "not-json"
    assert list(checkpoint.parent.glob(".checkpoint_*.json")) == [checkpoint]


async def test_generate_videos_resubmits_only_remaining_ids_from_a_durable_batch(
    factory_ctx: ToolHarness,
) -> None:
    from lib.db.base import DEFAULT_USER_ID
    from server.tool_runtime import CallerContext

    segments = factory_ctx.pm.script_payload["scenes"]
    segments.append(
        {
            **segments[0],
            "scene_id": "E1S02",
            "generated_assets": {"storyboard_image": "storyboards/scene_E1S02.png"},
        }
    )
    (factory_ctx.project_path / "storyboards" / "scene_E1S02.png").write_bytes(b"png")
    queue = factory_ctx.queue
    factory_ctx.caller = CallerContext(user_id=DEFAULT_USER_ID, source="mcp")

    first = await run_generate_videos(factory_ctx, _EPISODE_1)
    assert isinstance(first.value, GenerationBatchReadModel), first
    first_task = await queue.claim_next_task(media_type="video")
    assert first_task is not None, first
    await queue.mark_task_succeeded(first_task["task_id"], {"file_path": "videos/scene_E1S01.mp4"})
    second_task = await queue.claim_next_task(media_type="video")
    assert second_task is not None
    await queue.mark_task_failed(second_task["task_id"], "provider failed")
    video_path = _select_manual_video(
        factory_ctx.project_path,
        resource_type="videos",
        resource_id="E1S01",
        content=b"finished-video",
    )
    segments[0]["generated_assets"]["video_clip"] = video_path

    terminal = await queue.get_generation_batch(project_name="demo", batch_id=first.value.batch_id)
    assert terminal.done is True
    assert terminal.generation_result is not None
    assert terminal.generation_result.succeeded == ["E1S01"]
    assert terminal.generation_result.failed == ["E1S02"]

    retried = await run_generate_videos(factory_ctx, _selected("E1S01", "E1S02"), force=False)

    assert isinstance(retried.value, GenerationBatchReadModel), retried
    assert [item.unit_id for item in retried.value.skipped] == ["E1S01"]
    assert [(item.unit_id, item.status) for item in retried.value.members] == [("E1S02", "queued")]
    assert len((await queue.list_tasks(project_name="demo"))["items"]) == 3


async def test_generate_videos_scene_scope_happy(factory_ctx: ToolHarness) -> None:
    out = await run_generate_videos(factory_ctx, _scene("E1S01"), force=True, batch_waiter=fake_scene_batch)

    assert not _is_error(out), out


async def test_generate_videos_storyboard_request_is_independent_of_project_narration_delivery(
    factory_ctx: ToolHarness,
) -> None:
    """分镜图生视频：TTS 配音项目与后期配音项目得到同一份准入结论与同一份入队请求。"""

    async def _submit(project_config: dict[str, Any]) -> tuple[dict[str, Any], list[Any]]:
        factory_ctx.pm.project_payload.update(project_config)
        enqueued: list[Any] = []

        async def _batch(*, specs, **batch_kwargs):
            enqueued.extend(specs)
            return await fake_scene_batch(specs=specs, **batch_kwargs)

        out = await run_generate_videos(factory_ctx, _scene("E1S01"), force=True, batch_waiter=_batch)
        assert not _is_error(out), out
        return read_generation_result(out).model_dump(mode="json"), [
            (spec.resource_id, spec.payload) for spec in enqueued
        ]

    post_production = await _submit(_POST_PRODUCTION_PROJECT)
    use_tts = await _submit(_USE_TTS_PROJECT)

    assert use_tts == post_production
    [(resource_id, payload)] = use_tts[1]
    assert resource_id == "E1S01"
    assert "narration_delivery_options" not in (payload or {})


async def test_generate_videos_scene_scope_accepts_legacy_drama_dialogue(factory_ctx: ToolHarness) -> None:
    factory_ctx.pm.project_payload["content_mode"] = "drama"
    factory_ctx.pm.script_payload = (
        {
            "content_mode": "drama",
            "episode": 1,
            "scenes": [
                {
                    "scene_id": "E1S01",
                    "video_prompt": {
                        "action": "阿离转身",
                        "camera_motion": "Static",
                        "ambiance_audio": "风声",
                        "dialogue": [{"speaker": "张三", "line": "跟紧我。"}],
                    },
                    "voiceover": [],
                    "generated_assets": {"storyboard_image": "storyboards/scene_E1S01.png"},
                }
            ],
        }
    ) | {"short_drama_meta": _factory_meta(factory_ctx)}

    out = await run_generate_videos(factory_ctx, _scene("E1S01"), force=True, batch_waiter=fake_scene_batch)

    assert not _is_error(out), out


async def test_generate_videos_scene_scope_accepts_speech_free_legacy_drama(factory_ctx: ToolHarness) -> None:
    factory_ctx.pm.project_payload["content_mode"] = "drama"
    factory_ctx.pm.script_payload = (
        {
            "content_mode": "drama",
            "episode": 1,
            "scenes": [
                {
                    "scene_id": "E1S01",
                    "video_prompt": {
                        "action": "阿离转身",
                        "camera_motion": "Static",
                        "ambiance_audio": "风声",
                    },
                    "generated_assets": {"storyboard_image": "storyboards/scene_E1S01.png"},
                }
            ],
        }
    ) | {"short_drama_meta": _factory_meta(factory_ctx)}

    out = await run_generate_videos(factory_ctx, _scene("E1S01"), force=True, batch_waiter=fake_scene_batch)

    assert not _is_error(out), out


async def test_generate_videos_episode_scope_storyboard_batch_blocks_on_mixed_speech(
    factory_ctx: ToolHarness, monkeypatch
) -> None:
    """分镜图生视频的整批入口同样过发声准入：一个混合发声条目扣下整批，零任务入队。"""
    project_dir = factory_ctx.pm.get_project_path("demo")
    for segment_id in ("E1S01", "E1S02"):
        (project_dir / "storyboards" / f"scene_{segment_id}.png").write_bytes(b"png")
    factory_ctx.pm.script_payload["scenes"] = [
        {
            "scene_id": "E1S01",
            "source_text": "风吹过旷野。",
            # 旁白与角色台词同时出现：需要重规划，不是可以直接下单的条目。
            "utterances": [
                {"kind": "dialogue", "speaker": "阿离", "text": "快走。"},
                {"kind": "voiceover", "text": "风吹过旷野。"},
            ],
            "video_prompt": {"action": "快走", "camera_motion": "Static"},
            "generated_assets": {"storyboard_image": "storyboards/scene_E1S01.png"},
        },
        {
            "scene_id": "E1S02",
            "source_text": "他停下脚步。",
            "video_prompt": "第二镜",
            "generated_assets": {"storyboard_image": "storyboards/scene_E1S02.png"},
        },
    ]
    enqueued: list[Any] = []
    monkeypatch.setattr(
        "server.services.admission.video_batch_admission.get_active_tasks_for_resources", AsyncMock(return_value=[])
    )

    out = await run_generate_videos(factory_ctx, _EPISODE_1, batch_waiter=_recording_batch(enqueued))

    assert enqueued == []
    assert _is_error(out)
    result = read_generation_result(out)
    codes = {item.unit_id: item.problem.code for item in result.items if item.problem is not None}
    assert codes["E1S01"] == "mixed_speech"
    assert codes["E1S02"] == "generation_batch_admission_withheld"


async def test_generate_videos_episode_scope_storyboard_batch_blocks_when_a_video_prompt_is_pending(
    factory_ctx: ToolHarness, monkeypatch
) -> None:
    """机械转换出的条目 video_prompt 为 None：整批受阻、零任务入队，回执点名待生成的条目。"""
    project_dir = factory_ctx.pm.get_project_path("demo")
    for segment_id in ("E1S01", "E1S02"):
        (project_dir / "storyboards" / f"scene_{segment_id}.png").write_bytes(b"png")
    factory_ctx.pm.script_payload["scenes"] = [
        {
            "scene_id": "E1S01",
            "source_text": "风吹过旷野。",
            "image_prompt": None,
            "video_prompt": None,
            "generated_assets": {"storyboard_image": "storyboards/scene_E1S01.png"},
        },
        {
            "scene_id": "E1S02",
            "source_text": "他停下脚步。",
            "video_prompt": "第二镜",
            "generated_assets": {"storyboard_image": "storyboards/scene_E1S02.png"},
        },
    ]
    enqueued: list[Any] = []
    monkeypatch.setattr(
        "server.services.admission.video_batch_admission.get_active_tasks_for_resources", AsyncMock(return_value=[])
    )

    out = await run_generate_videos(factory_ctx, _EPISODE_1, batch_waiter=_recording_batch(enqueued))

    assert enqueued == []
    assert _is_error(out)
    result = read_generation_result(out)
    codes = {item.unit_id: item.problem.code for item in result.items if item.problem is not None}
    assert codes["E1S01"] == "generation_unit_request_invalid"
    assert codes["E1S02"] == "generation_batch_admission_withheld"


async def test_generate_videos_scene_scope_missing(factory_ctx: ToolHarness) -> None:
    out = await run_generate_videos(factory_ctx, _scene("NO_SUCH"), force=True)

    assert _is_error(out)


@pytest.mark.parametrize(
    "storyboard_value",
    [
        123,  # 剧本 JSON 里的脏数据（非字符串）须可读失败而非未处理 TypeError
        "/etc/passwd",  # 绝对路径：越权引用项目外文件
        "../../outside.png",  # `..` 穿越出项目目录
    ],
)
async def test_generate_videos_scene_scope_rejects_invalid_storyboard_image(
    factory_ctx: ToolHarness, storyboard_value: object
) -> None:
    factory_ctx.pm.script_payload["scenes"][0]["generated_assets"] = {"storyboard_image": storyboard_value}

    out = await run_generate_videos(factory_ctx, _scene("E1S01"), force=True)

    assert _is_error(out)
    # 锁定 resolve_storyboard_image_ref 抛出的 canonical 消息，而不是模糊子串或通用失败文本
    assert f"invalid storyboard image path: {storyboard_value!r}" in _text(out)


async def test_generate_videos_all_scope_happy(factory_ctx: ToolHarness) -> None:
    async def fake_batch(*, specs, **_batch_kwargs):
        from lib.generation.generation_queue_client import BatchTaskResult

        succ = [
            BatchTaskResult(
                resource_id=s.resource_id, task_id="t1", status="succeeded", result={"file_path": "videos/x.mp4"}
            )
            for s in specs
        ]
        return succ, []

    out = await run_generate_videos(factory_ctx, _ALL, batch_waiter=fake_batch)

    assert not _is_error(out), out


async def test_generate_videos_all_scope_error(factory_ctx: ToolHarness) -> None:
    def boom(*a, **kw):
        raise RuntimeError("broken")

    factory_ctx.pm.load_script = boom

    out = await run_generate_videos(factory_ctx, _ALL)

    assert _is_error(out)


async def test_generate_videos_selected_scope_happy(factory_ctx: ToolHarness) -> None:
    out = await run_generate_videos(factory_ctx, _selected("E1S01"), force=True, batch_waiter=fake_scene_batch)

    assert not _is_error(out), out


async def test_generate_videos_selected_scope_no_match(factory_ctx: ToolHarness) -> None:
    out = await run_generate_videos(factory_ctx, _selected("NO_SUCH"), force=True)

    assert _is_error(out)


# ---------------------------------------------------------------------------
# generate_videos：参考生视频
# ---------------------------------------------------------------------------


def test_build_video_specs_does_not_validate_duration_at_enqueue(tmp_path) -> None:
    """duration 是能力维度，入队侧不再校验——任意 duration 都透传给执行层（见 ADR-0001）。"""
    from server.services.admission.video_batch_admission import build_storyboard_video_specs as _build_video_specs

    (tmp_path / "storyboards").mkdir()
    (tmp_path / "storyboards" / "scene_S01.png").write_bytes(b"png")
    project = _activated_project(tmp_path, {"S01": "storyboards/scene_S01.png"})
    items = [
        {
            "scene_id": "S01",
            "source_text": "他在旷野上奔跑。",
            "video_prompt": "一个奔跑的镜头",
            "duration_seconds": 7,  # 不属于任何典型 supported_durations
            "generated_assets": {"storyboard_image": "storyboards/scene_S01.png"},
        }
    ]
    specs, _refused = _build_video_specs(
        items=items,
        id_field="scene_id",
        content_mode="drama",
        skeleton_kind="scenes",
        script_filename="episode_1.json",
        project_dir=tmp_path,
        skip_ids=None,
        project=project,
    )
    assert len(specs) == 1
    assert specs[0].payload["duration_seconds"] == 7

    # 未显式指定 duration 时不携带该键，留给执行层按 caps 收口默认。
    items[0].pop("duration_seconds")
    specs2, _ = _build_video_specs(
        items=items,
        id_field="scene_id",
        content_mode="drama",
        skeleton_kind="scenes",
        script_filename="episode_1.json",
        project_dir=tmp_path,
        skip_ids=None,
        project=project,
    )
    assert "duration_seconds" not in specs2[0].payload


@pytest.mark.parametrize(
    "storyboard_value",
    [
        123,  # 剧本 JSON 里的脏数据（非字符串）
        "/etc/passwd",  # 绝对路径
        "../../outside.png",  # `..` 穿越出项目目录
    ],
)
def test_build_video_specs_skips_invalid_storyboard_image_without_aborting_batch(
    tmp_path: Path, storyboard_value: object
) -> None:
    """批量入队场景下，单个条目 storyboard_image 非法（脏数据/越界/绝对路径）只记为该 ID 的
    blocked，不应让 `project_dir / storyboard_image` 抛未处理异常中断整批。"""
    from server.services.admission.video_batch_admission import build_storyboard_video_specs as _build_video_specs

    (tmp_path / "storyboards").mkdir()
    (tmp_path / "storyboards" / "scene_S02.png").write_bytes(b"png")
    project = _activated_project(tmp_path, {"S02": "storyboards/scene_S02.png"})
    items = [
        {
            "scene_id": "S01",
            "source_text": "第一段旁白。",
            "video_prompt": "非法引用",
            "generated_assets": {"storyboard_image": storyboard_value},
        },
        {
            "scene_id": "S02",
            "source_text": "第二段旁白。",
            "video_prompt": "合法引用",
            "generated_assets": {"storyboard_image": "storyboards/scene_S02.png"},
        },
    ]
    specs, refused = _build_video_specs(
        items=items,
        id_field="scene_id",
        content_mode="drama",
        skeleton_kind="scenes",
        script_filename="episode_1.json",
        project_dir=tmp_path,
        skip_ids=None,
        project=project,
    )
    assert [s.resource_id for s in specs] == ["S02"]
    assert _refused_problems(refused) == {"S01": ("generation_unit_input_unusable", "generate_dependency")}


def test_build_video_specs_skips_non_dict_generated_assets_without_aborting_batch(tmp_path: Path) -> None:
    """generated_assets 容器本身被外部编辑损坏为非 dict（如 list）时按「没有分镜图」跳过，
    不应让 `.get("storyboard_image")` 在非 dict 上抛未处理 AttributeError 中断整批。"""
    from server.services.admission.video_batch_admission import build_storyboard_video_specs as _build_video_specs

    (tmp_path / "storyboards").mkdir()
    (tmp_path / "storyboards" / "scene_S02.png").write_bytes(b"png")
    project = _activated_project(tmp_path, {"S02": "storyboards/scene_S02.png"})
    items = [
        {
            "scene_id": "S01",
            "source_text": "第一段旁白。",
            "video_prompt": "脏数据",
            "generated_assets": ["bad"],
        },
        {
            "scene_id": "S02",
            "source_text": "第二段旁白。",
            "video_prompt": "合法引用",
            "generated_assets": {"storyboard_image": "storyboards/scene_S02.png"},
        },
    ]
    specs, refused = _build_video_specs(
        items=items,
        id_field="scene_id",
        content_mode="drama",
        skeleton_kind="scenes",
        script_filename="episode_1.json",
        project_dir=tmp_path,
        skip_ids=None,
        project=project,
    )
    assert [s.resource_id for s in specs] == ["S02"]
    assert _refused_problems(refused) == {"S01": ("generation_unit_input_unusable", "generate_dependency")}


async def test_generate_videos_scene_scope_generated_assets_non_dict_readable_rejection(
    factory_ctx: ToolHarness,
) -> None:
    """generated_assets 容器本身非 dict 时须走「没有分镜图」的可读拒绝分支，
    不应在单条路径上抛未处理 AttributeError。"""
    factory_ctx.pm.script_payload["scenes"][0]["generated_assets"] = ["bad"]

    out = await run_generate_videos(factory_ctx, _scene("E1S01"), force=True)

    assert _is_error(out)
    assert "请先运行 generate_storyboards" in _text(out)


def _admitted_video_yaml(item: dict, **kwargs) -> dict:
    """准入渲染出口产出的 YAML 文档。

    该出口与执行路径共用 ``render_storyboard_video_prompt``，反向约束是其中的 ``Avoid`` 键。
    """
    import yaml

    from server.services.admission.video_batch_admission import storyboard_video_prompt

    return yaml.safe_load(storyboard_video_prompt(item, **kwargs))


def test_storyboard_video_prompt_drama_sources_dialogue_from_utterances() -> None:
    """drama：准入渲染出口从分镜级 dialogue-kind utterances 派生 video YAML 台词，
    voiceover-kind 不进；narration / ad（无 utterances 字段）原样渲染既有 video_prompt.dialogue。"""
    drama_item = {
        "scene_id": "E1S01",
        "video_prompt": {"action": "起身", "camera_motion": "Static", "ambiance_audio": "风声"},
        "utterances": [
            {"kind": "voiceover", "speaker": None, "text": "那是命运的开端。"},
            {"kind": "dialogue", "speaker": "王", "text": "你来了。"},
        ],
    }
    parsed = _admitted_video_yaml(drama_item, content_mode="drama")
    assert parsed["Dialogue"] == [{"Speaker": "王", "Line": "你来了。"}]

    narration_item = {
        "scene_id": "E1S01",
        "video_prompt": {
            "action": "走",
            "camera_motion": "Static",
            "ambiance_audio": "脚步声",
            "dialogue": [{"speaker": "Alice", "line": "hello"}],
        },
    }
    parsed_narr = _admitted_video_yaml(narration_item, content_mode="drama")
    assert parsed_narr["Dialogue"] == [{"Speaker": "Alice", "Line": "hello"}]


def test_storyboard_video_prompt_injects_voice_profiles_when_characters_given() -> None:
    """drama：传入带非空 voice_style 的角色资产时 YAML 顶部出现 Voice_Profiles；
    voice_characters 缺省（既有调用点行为）不注入。"""
    drama_item = {
        "scene_id": "E1S01",
        "video_prompt": {"action": "起身", "camera_motion": "Static", "ambiance_audio": "风声"},
        "utterances": [{"kind": "dialogue", "speaker": "王", "text": "你来了。"}],
    }
    characters = {"王": {"voice_style": "低沉沙哑"}}

    parsed = _admitted_video_yaml(drama_item, content_mode="drama", voice_characters=characters)
    assert parsed["Voice_Profiles"] == [{"Speaker": "王", "Voice_Style": "低沉沙哑"}]

    parsed_default = _admitted_video_yaml(drama_item, content_mode="drama")
    assert "Voice_Profiles" not in parsed_default

    parsed_no_style = _admitted_video_yaml(
        drama_item, content_mode="drama", voice_characters={"王": {"voice_style": ""}}
    )
    assert "Voice_Profiles" not in parsed_no_style


def test_storyboard_video_prompt_injects_voice_profiles_from_legacy_dialogue() -> None:
    """utterances 迁移前的存量 drama 剧本（无 utterances 字段，台词仍在
    video_prompt.dialogue）：改走 legacy 出口派生 Voice_Profiles，不因缺 utterances 静默丢失。"""
    legacy_drama_item = {
        "scene_id": "E1S01",
        "video_prompt": {
            "action": "起身",
            "camera_motion": "Static",
            "ambiance_audio": "风声",
            "dialogue": [{"speaker": "王", "line": "你来了。"}],
        },
    }
    characters = {"王": {"voice_style": "低沉沙哑"}}

    parsed = _admitted_video_yaml(legacy_drama_item, content_mode="drama", voice_characters=characters)
    assert parsed["Voice_Profiles"] == [{"Speaker": "王", "Voice_Style": "低沉沙哑"}]
    assert parsed["Dialogue"] == [{"Speaker": "王", "Line": "你来了。"}]


def test_storyboard_video_prompt_strips_caller_supplied_voice_profiles_for_non_drama() -> None:
    """narration/ad（item 无 utterances 字段）剧本 video_prompt 自带 voice_profiles 时一律剥离：
    该声明段唯一来源是 build_drama_video_prompt 的机械派生，剧本残留值不得越权、绕过 C 类
    （真无声）门控直达 YAML。"""
    narration_item = {
        "scene_id": "E1S01",
        "video_prompt": {
            "action": "走",
            "camera_motion": "Static",
            "ambiance_audio": "脚步声",
            "voice_profiles": [{"Speaker": "赝品", "Voice_Style": "越权"}],
        },
    }
    parsed = _admitted_video_yaml(narration_item, content_mode="drama")
    assert "Voice_Profiles" not in parsed


async def test_resolve_voice_context_skips_non_drama(factory_ctx: ToolHarness) -> None:
    """narration/ad：不解析 voice_consistency，直接跳过（无 drama dialogue speaker 概念）。"""
    from server.services.admission.video_batch_admission import resolve_voice_context as _resolve_voice_context

    assert await _resolve_voice_context(factory_ctx.pm.project_payload, "narration") is None


async def test_resolve_voice_context_drama_reads_project_characters_and_gate(
    factory_ctx: ToolHarness, monkeypatch
) -> None:
    """drama：读项目角色资产，无声（C 类真无声、或本集关闭音频）时退回不注入。"""
    from server.services.admission import video_batch_admission as admission_mod

    async def fake_not_silent(_project, _episode=None):
        return False

    monkeypatch.setattr(admission_mod, "resolve_project_is_silent", fake_not_silent)
    characters = await admission_mod.resolve_voice_context(factory_ctx.pm.project_payload, "drama")
    assert characters == factory_ctx.pm.project_payload["characters"]

    async def fake_silent(_project, _episode=None):
        return True

    monkeypatch.setattr(admission_mod, "resolve_project_is_silent", fake_silent)
    assert await admission_mod.resolve_voice_context(factory_ctx.pm.project_payload, "drama") is None


def _video_quote(out: ToolOutcome[Any]) -> dict[str, Any]:
    assert out.problem is None, out
    assert isinstance(out.value, dict)
    return out.value["video_quote"]


async def test_generate_videos_preview_quotes_storyboard_units_at_their_planned_duration(
    factory_ctx: ToolHarness,
    set_admission_video_request_facts,
) -> None:
    """分镜图生视频预检：不入队，按编排时长报价，档位不变；报价与 Web 批量确认的计价同一口径。"""

    from lib.db import async_session_factory
    from lib.db.repositories.custom_provider_repo import CustomProviderRepository
    from lib.generation.video_request_facts import VideoRequestCostFacts
    from server.services.admission.cost_estimation import quote_video_request_from_price

    facts = make_video_request_facts(
        provider_id="openai", model_id="sora-2", resolution="720p", audio_switch_controllable=True
    )
    set_admission_video_request_facts(facts)
    enqueued: list[Any] = []

    out = await run_generate_videos(
        factory_ctx, _selected("E1S01"), force=True, preview=True, batch_waiter=_recording_batch(enqueued)
    )

    assert enqueued == []
    quote = _video_quote(out)
    [unit] = quote["units"]
    assert unit["outcome"] == "generate"
    assert (unit["script_duration_seconds"], unit["request_duration_seconds"], unit["tier_changed"]) == (4, 4, False)
    async with async_session_factory() as session:
        price = await CustomProviderRepository(session).resolve_price(facts.provider_id, facts.model_id)
    expected = quote_video_request_from_price(VideoRequestCostFacts(facts, 4), price)
    assert unit["estimated_cost"] == expected.to_payload()
    assert quote["estimated_total"] == {expected.currency: pytest.approx(expected.amount)}
