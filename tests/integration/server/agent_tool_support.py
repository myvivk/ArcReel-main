"""Agent 工具测试共享的装配、替身与 helper；``fake_ctx`` 等 fixture 在 ``tests/integration/server/conftest.py``。"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import threading
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any, overload

from lib.config.resolver import ConfigResolver
from lib.db import async_session_factory
from lib.db.base import DEFAULT_USER_ID
from lib.generation.generation_queue import GenerationQueue, get_generation_queue
from lib.generation.generation_queue_client import batch_enqueue_and_wait
from lib.generation.generation_result import (
    GenerationBatchResult,
)
from lib.project.project_manager import ProjectManager
from lib.project.project_schema import CURRENT_PROJECT_SCHEMA_VERSION
from lib.script.short_drama_source import short_drama_source_relative_path
from server.agent_toolset.declaration import ToolDeclaration, invoke_declaration
from server.agent_toolset.envelope import json_value
from server.agent_toolset.toolset import AGENT_TOOLSET
from server.services.project import workflow_planner
from server.tool_runtime import (
    BatchWaiter,
    CallerContext,
    ProjectScope,
    Services,
    TextGenerationResult,
    ToolOutcome,
    ToolProblem,
)
from tests.fakes import FakeConfigResolver

# ---------------------------------------------------------------------------
# Generation result contract helpers
# ---------------------------------------------------------------------------


async def fake_scene_batch(*, project_name, specs, on_success=None, on_failure=None, **_batch_kwargs):
    """Stand in for the queue: every scene spec lands its canonical mp4."""

    from lib.generation.generation_queue_client import BatchTaskResult

    return [
        BatchTaskResult(
            resource_id=spec.resource_id,
            task_id="t1",
            status="succeeded",
            result={"file_path": f"videos/scene_{spec.resource_id}.mp4"},
        )
        for spec in specs
    ], []


def read_generation_result(out: dict[str, Any] | ToolOutcome[Any]) -> GenerationBatchResult:
    """Read the structured contract out of a tool response, never its text."""

    if isinstance(out, ToolOutcome):
        assert isinstance(out.value, dict)
        return GenerationBatchResult.model_validate(out.value["generation_result"])
    return GenerationBatchResult.model_validate(out["generation_result"])


class ToolHarness:
    """一次 Agent 工具调用的装配：会话项目、调用方与 ``Services``；项目管理器、能力解析器与队列可换成替身。

    ``services`` 每次按当前属性重建，用例改了 ``config_resolver`` 等属性后下一次调用即生效。
    """

    def __init__(
        self,
        project_name: str,
        data_root: Path,
        pm: ProjectManager | None = None,
        *,
        config_resolver: ConfigResolver | None = None,
        caller: CallerContext | None = None,
        queue: GenerationQueue | None = None,
    ):
        self.project_name = project_name
        self.data_root = data_root
        self.pm: ProjectManager = pm if pm is not None else ProjectManager(data_root)
        self.config_resolver = config_resolver
        self.caller = caller or CallerContext(user_id=DEFAULT_USER_ID, source="embedded")
        self.queue = queue or get_generation_queue()

    @property
    def project_path(self) -> Path:
        return self.pm.get_project_path(self.project_name)

    @property
    def scope(self) -> ProjectScope:
        return ProjectScope(project_name=self.project_name, data_root=self.data_root)

    @property
    def services(self) -> Services:
        return Services(
            projects=self.pm,
            workflow_planner=workflow_planner.get_workflow_planner(self.pm),
            capabilities=self.config_resolver or ConfigResolver(async_session_factory),
            queue=self.queue,
        )


@overload
async def run_declared_tool[ResultT](
    declaration: ToolDeclaration[Any, ResultT],
    ctx: ToolHarness,
    arguments: dict[str, Any],
    *,
    batch_waiter: BatchWaiter = ...,
    **collaborators: Any,
) -> ToolOutcome[ResultT]: ...


@overload
async def run_declared_tool(
    declaration: str,
    ctx: ToolHarness,
    arguments: dict[str, Any],
    *,
    batch_waiter: BatchWaiter = ...,
    **collaborators: Any,
) -> ToolOutcome[Any]: ...


async def run_declared_tool(
    declaration: ToolDeclaration[Any, Any] | str,
    ctx: ToolHarness,
    arguments: dict[str, Any],
    *,
    batch_waiter: BatchWaiter = batch_enqueue_and_wait,
    **collaborators: Any,
) -> ToolOutcome[Any]:
    """经工具声明的共享入口（两宿主同一入口）调用，拿到 handler 的 ``ToolOutcome``。

    ``declaration`` 可传声明或工具名；内嵌批次等待器随 caller 注入；``collaborators`` 作为 handler 的
    关键字参数注入领域协作者替身。
    """

    if isinstance(declaration, str):
        found = next(item for item in AGENT_TOOLSET if item.name == declaration)
        assert isinstance(found, ToolDeclaration), f"{declaration} 不作用于项目"
        declaration = found
    if collaborators:
        declaration = replace(declaration, handler=partial(declaration.handler, **collaborators))
    caller = replace(ctx.caller, batch_waiter=batch_waiter)
    return await invoke_declaration(declaration, arguments, ctx.scope, caller, ctx.services)


def said(outcome: ToolOutcome[Any]) -> str:
    """结果的可读文本：失败时的 problem detail，文本生成的 message，其余为值的 JSON。"""

    if outcome.problem is not None:
        return outcome.problem.detail
    if isinstance(outcome.value, TextGenerationResult):
        return outcome.value.message
    return json.dumps(json_value(outcome.value), ensure_ascii=False)


def problem_of(outcome: ToolOutcome[Any]) -> ToolProblem:
    assert outcome.problem is not None, outcome.value
    return outcome.problem


async def run_generate_videos(
    ctx: ToolHarness,
    target: dict[str, Any],
    *,
    script: str = "episode_1.json",
    batch_waiter: BatchWaiter = batch_enqueue_and_wait,
    **arguments: Any,
) -> ToolOutcome[Any]:
    """经 ``generate_videos`` 声明入口生成视频，拿到 handler 的 ``ToolOutcome``。"""

    return await run_declared_tool(
        "generate_videos",
        ctx,
        {"script": script, "target": target, **arguments},
        batch_waiter=batch_waiter,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_CLAIMED_BASIS_DIGEST = "sha256-v1:" + "a" * 64


class FakePM:
    def __init__(self, project_name: str, project_dir: Path):
        self._project_name = project_name
        self._project_dir = project_dir
        self.project_payload: dict[str, Any] = {
            "schema_version": CURRENT_PROJECT_SCHEMA_VERSION,
            "content_mode": "drama",
            "generation_mode": "storyboard",
            "source_kind": "novel",
            "source_language": "中文",
            "overview": {},
            "episodes": [{"episode": 1, "script_file": "scripts/episode_1.json", "source_origin": "own"}],
            "characters": {"张三": {"description": "主角"}, "李四": {"description": ""}},
            "scenes": {"村口": {"description": "黄昏的村口"}},
            "props": {},
            "products": {"保温杯": {"description": "不锈钢保温杯", "reference_images": [], "selling_points": []}},
            "style": "anime",
            "style_description": "soft pastel",
        }
        self.project_load_threads: list[int] = []
        self.script_payload: dict[str, Any] = {
            "content_mode": "narration",
            "episode": 1,
            "segments": [
                {
                    "segment_id": "E1S01",
                    "image_prompt": "村口黄昏",
                    "novel_text": "黄昏时分，风吹过村口。",
                    "video_prompt": {"action": "镜头平移", "camera_motion": "Pan", "ambiance_audio": "风声"},
                    "duration_seconds": 4,
                    "generated_assets": {"storyboard_image": "storyboards/scene_E1S01.png"},
                },
            ],
        }

    def get_project_path(self, _name: str) -> Path:
        return self._project_dir

    def mirror_to_disk(self, script_filename: str | None = None) -> None:
        """把内存态落盘并重建产物清单：清单是读取已生成产物的唯一口径。

        ``load_script`` 每次读取都会先调用本方法；``load_project`` 与生产一样只读不写，只经它
        读项目、随后又从盘上读产物的工具，由用例在调用前显式调用本方法。

        清单按当下的项目与剧本重新激活（生产的补录路径），随后补回那些夹具不重建来源凭据的
        产物声明。用例故意构造的畸形条目激活不了，留空清单即可——工具侧本来就该按「产物不
        可用」逐条拒收。
        """

        (self._project_dir / "project.json").write_text(
            json.dumps(self.project_payload, ensure_ascii=False), encoding="utf-8"
        )
        filename = script_filename or self._canonical_script_filename()
        if filename is not None:
            scripts_dir = self._project_dir / "scripts"
            scripts_dir.mkdir(exist_ok=True)
            (scripts_dir / Path(filename).name).write_text(
                json.dumps(self.script_payload, ensure_ascii=False), encoding="utf-8"
            )
        from lib.artifacts.artifact_activation import activate_artifact_target_state

        # 用例故意构造的畸形项目/剧本激活不了；此处吞掉异常让清单留空，
        # 被测工具随后按「产物不可用」逐条拒收，这正是这些用例要断言的路径。
        with contextlib.suppress(Exception):
            activate_artifact_target_state(self._project_dir, bump_schema=False)
        self._register_claims(filename)

    def _canonical_script_filename(self) -> str | None:
        episode = self.script_payload.get("episode")
        return f"episode_{episode}.json" if isinstance(episode, bool) is False and isinstance(episode, int) else None

    def _script_episode(self, script_filename: str | None) -> int | None:
        """剧本身份取自身字段，缺字段时按规范文件名兜底——与生产的解析口径一致。"""

        episode = self.script_payload.get("episode")
        if isinstance(episode, int) and not isinstance(episode, bool) and episode >= 1:
            return episode
        match = re.fullmatch(r"episode_(\d+)\.json", Path(script_filename).name) if script_filename else None
        return int(match.group(1)) if match else None

    def _register_claims(self, script_filename: str | None = None) -> None:
        """把剧本已登记的产物补进清单：生产里它们在产出那一刻就登记过。

        激活能从来源凭据重建的条目以激活结果为准，这里只兜住夹具不重建凭据的那些
        （付费媒体的版本记录、缺 image_prompt 的历史分镜）。
        """

        from lib.artifacts.artifact_manifest import (
            ArtifactKey,
            ArtifactManifest,
            ArtifactManifestEntry,
            ProjectArtifactManifestAdapter,
        )

        adapter = ProjectArtifactManifestAdapter(self._project_dir)
        manifest = ArtifactManifest(adapter)
        recorded: dict[Any, str] = {}
        episode = self._script_episode(script_filename)
        if episode is not None:
            items = next(
                (
                    self.script_payload[field]
                    for field in ("segments", "scenes", "video_units")
                    if field in self.script_payload
                ),
                [],
            )
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict) or item.get("needs_replan") is True:
                    continue
                resource_id = next(
                    (
                        str(item[field])
                        for field in ("segment_id", "scene_id", "unit_id")
                        if isinstance(item.get(field), str) and item.get(field)
                    ),
                    "",
                )
                assets = item.get("generated_assets")
                if not resource_id or not isinstance(assets, dict):
                    continue
                for field, key_factory in (
                    ("storyboard_image", ArtifactKey.episode_storyboard),
                    ("narration_audio", ArtifactKey.episode_audio),
                    ("video_clip", ArtifactKey.episode_video),
                ):
                    artifact_path = assets.get(field)
                    if not isinstance(artifact_path, str) or not artifact_path:
                        continue
                    absolute = (self._project_dir / artifact_path).resolve()
                    if not absolute.is_file() or not absolute.is_relative_to(self._project_dir.resolve()):
                        continue
                    recorded[key_factory(episode, resource_id)] = artifact_path
        known = set(adapter.snapshot_entries())
        for key, artifact_path in recorded.items():
            if key in known:
                continue
            manifest.register_entry_transactionally(
                key,
                ArtifactManifestEntry(artifact_path=artifact_path, basis_digest=_CLAIMED_BASIS_DIGEST),
            )

    def load_project(self, _name: str) -> dict[str, Any]:
        self.project_load_threads.append(threading.get_ident())
        return self.project_payload

    def load_script(self, _name: str, filename: str) -> dict[str, Any]:
        self.mirror_to_disk(filename)
        return self.script_payload

    def load_script_readonly(self, _name: str, _filename: str) -> dict[str, Any]:
        return self.script_payload

    def project_exists(self, _name: str) -> bool:
        return True


def fake_caps_resolver(**kwargs: Any) -> Any:
    """构造假能力解析器（见 ``tests.fakes.FakeConfigResolver``）。

    返回值按 ``Any`` 交出：注入点标注的是生产的 ``ConfigResolver``，替身只实现被调到的那几个
    方法，逐个调用点写类型豁免不如在这唯一的构造点交出去。
    """
    return FakeConfigResolver(**kwargs)


def use_fake_caps(fake_ctx: ToolHarness, **kwargs: Any) -> Any:
    """给这个会话装上假能力解析器并返回它。

    ``ToolHarness.config_resolver`` 经 ``Services.capabilities`` 透传给能力 dict 与图像能力的取值器（如
    ``get_video_capabilities`` 的能力载荷、图生图能力闸）。时长档位与声音档不经它：那些读视频请求
    事实，用例用 ``set_video_request_facts`` / ``video_request_facts`` 提供。
    """
    resolver = fake_caps_resolver(**kwargs)
    fake_ctx.config_resolver = resolver
    return resolver


def prepare_factory_script(ctx: ToolHarness) -> ToolHarness:
    """铺设工厂源稿及匹配来源修订的视觉派生状态，供媒体工具回归使用。"""
    source = ctx.project_path / short_drama_source_relative_path(1)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# 第1集\n黄昏时分，风吹过村口。\n", encoding="utf-8")
    ctx.pm.script_payload = {
        "episode": 1,
        "title": "村口",
        "content_mode": "drama",
        "short_drama_meta": {"source_revision": hashlib.sha256(source.read_bytes()).hexdigest()},
        "scenes": [
            {
                "scene_id": "E1S01",
                "source_text": "黄昏时分，风吹过村口。",
                "image_prompt": "【镜头1】村口黄昏。",
                "video_prompt": {"action": "镜头平移", "camera_motion": "Pan", "ambiance_audio": "风声"},
                "duration_seconds": 4,
                "utterances": [],
                "characters_in_scene": [],
                "scenes": [],
                "props": [],
                "generated_assets": {"storyboard_image": "storyboards/scene_E1S01.png"},
            }
        ],
    }
    return ctx


def activate_unbound_project(fake_ctx: ToolHarness, *, generation_mode: str = "storyboard") -> None:
    project = fake_ctx.pm.project_payload
    project.update(
        {
            "schema_version": CURRENT_PROJECT_SCHEMA_VERSION,
            "content_mode": "drama",
            "generation_mode": generation_mode,
            "episodes": [],
        }
    )
    (fake_ctx.project_path / "project.json").write_text(json.dumps(project), encoding="utf-8")
