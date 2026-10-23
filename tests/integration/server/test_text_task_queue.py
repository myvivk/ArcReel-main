from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from lib.artifacts.artifact_activation import activate_artifact_target_state
from lib.artifacts.artifact_manifest import ArtifactKey, ProjectArtifactManifestAdapter
from lib.config.resolver import ConfigResolver
from lib.db import async_session_factory
from lib.db.base import DEFAULT_USER_ID
from lib.generation.generation_queue import GenerationQueue
from lib.generation.generation_queue_client import wait_for_task
from lib.generation.generation_worker import CapacityTable, GenerationWorker
from lib.infra.async_thread import run_sync_transaction
from lib.project.project_manager import ProjectManager
from server.tool_runtime import (
    CallerContext,
    GenerateEpisodeScriptRequest,
    ProjectScope,
    Services,
    ToolRequest,
    execute_queued_text_task,
    generate_episode_script,
)
from tests.fakes import refuse_resume_execution
from tests.integration.lib.script.storyboard_support import factory_prompt_project, read_factory_script


def _admit_text_operations(projects: ProjectManager, project_name: str) -> None:
    directory = projects.get_project_path(project_name)
    script = read_factory_script(directory)
    script["scenes"][0].update(image_prompt=None, pending_authoring=True)
    projects.save_script(project_name, script, "episode_1.json")


async def _start_text_worker(
    queue: GenerationQueue,
    executor: Callable[..., Awaitable[dict[str, Any]]],
) -> GenerationWorker:
    async def text_provider(_task: dict[str, Any]) -> str:
        return "text"

    worker = GenerationWorker(
        queue=queue,
        capacity=CapacityTable(_limits={}, _defaults={"text": 1}),
        provider_projection=text_provider,
        executor=executor,
        lanes=("text",),
        resume_executor=refuse_resume_execution,
    )
    worker.poll_interval = 0.01
    worker.heartbeat_interval = 0.01
    assert await queue.acquire_or_renew_worker_lease(
        name=worker.lease_name,
        owner_id=worker.owner_id,
        ttl_seconds=worker.lease_ttl,
    )
    await worker.start()
    return worker


async def test_web_prompt_authoring_returns_the_batch_without_waiting(tmp_path: Path, file_db_factory) -> None:
    """Web 入口与 Agent 同一个服务命令：提交即返批次句柄，任务按 Web 来源登记，载荷带范围与模式。"""
    directory = factory_prompt_project(tmp_path)
    projects = ProjectManager.for_project_dir(directory)
    _admit_text_operations(projects, "demo")
    queue = GenerationQueue(session_factory=file_db_factory, project_manager=projects)
    assert await queue.acquire_or_renew_worker_lease(name="default", owner_id="test-worker", ttl_seconds=60)
    services = Services(
        projects=projects,
        workflow_planner=object(),
        capabilities=ConfigResolver(async_session_factory),
        queue=queue,
    )

    outcome = await generate_episode_script(
        ToolRequest(GenerateEpisodeScriptRequest(episode_id=1, instructions="冷色调")),
        ProjectScope(project_name="demo", data_root=projects.data_root),
        CallerContext(user_id=DEFAULT_USER_ID, source="webui"),
        services,
    )

    assert outcome.problem is None
    batch = outcome.value
    assert batch is not None
    assert batch.done is False
    task_id = batch.members[0].task_id
    assert task_id is not None
    task = await queue.get_task(task_id)
    assert task is not None
    assert task["source"] == "webui"
    assert task["payload"]["instructions"] == "冷色调"
    assert task["payload"]["rewrite"] is False


async def test_cancel_during_started_episode_script_commit_leaves_member_running_to_success(
    tmp_path: Path, file_db_factory, monkeypatch
) -> None:
    project_path = factory_prompt_project(tmp_path, project_name="script")
    projects = ProjectManager.for_project_dir(project_path)
    monkeypatch.setenv("ARCREEL_DATA_DIR", str(projects.data_root))
    _admit_text_operations(projects, "script")
    projects.update_project(
        "script",
        lambda project: project.update(
            episodes=[{"episode": 1, "title": "第一集", "script_file": "scripts/episode_1.json"}]
        ),
    )
    activate_artifact_target_state(project_path, bump_schema=False)
    started = threading.Event()
    release = threading.Event()

    class Generator:
        content_mode = "drama"

        def __init__(self, *_args, **_kwargs) -> None:
            self.project_json = projects.load_project("script")

        def prompt_overwrite(self, *_args, **_kwargs):
            return None

        @classmethod
        async def create(cls, *_args, **_kwargs):
            return cls()

        async def generate(self, *_args, **_kwargs):
            def commit():
                script = read_factory_script(project_path)
                script["title"] = "新剧本"
                path = projects.save_script(
                    "script",
                    script,
                    "episode_1.json",
                    validate=False,
                )
                started.set()
                release.wait()
                return path

            return await run_sync_transaction(commit)

    monkeypatch.setattr("server.text_generation.ScriptGenerator", Generator)
    queue = GenerationQueue(session_factory=file_db_factory, project_manager=projects)

    async def execute(task: dict[str, Any], *, claimed_provider_id: str | None = None) -> dict[str, Any]:
        del claimed_provider_id
        return await execute_queued_text_task(task)

    worker = await _start_text_worker(queue, execute)
    services = Services(
        projects=projects,
        workflow_planner=object(),
        capabilities=ConfigResolver(async_session_factory),
        queue=queue,
    )
    try:
        submitted = await generate_episode_script(
            ToolRequest(GenerateEpisodeScriptRequest(episode_id=1)),
            ProjectScope(project_name="script", data_root=projects.data_root),
            CallerContext(user_id=DEFAULT_USER_ID, source="mcp"),
            services,
        )
        assert submitted.value is not None
        batch_id = submitted.value.batch_id
        task_id = submitted.value.members[0].task_id
        assert task_id is not None
        assert await asyncio.to_thread(started.wait, 1)
        cancelled = await queue.cancel_generation_batch(project_name="script", batch_id=batch_id)
        assert cancelled.model_dump() == {"cancelled": [], "skipped_running": [task_id], "skipped_terminal": []}
    finally:
        release.set()
    try:
        task = await wait_for_task(task_id, 0.01, queue=queue)
        assert task["status"] == "succeeded"
    finally:
        await worker.stop()
    batch = await queue.get_generation_batch(project_name="script", batch_id=batch_id)
    assert batch.done is True
    assert batch.members[0].status == "succeeded"
    assert projects.load_script("script", "episode_1.json")["title"] == "新剧本"
    assert ProjectArtifactManifestAdapter(project_path).get_entry(ArtifactKey.episode_script(1)) is not None
