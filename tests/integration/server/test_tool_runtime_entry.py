from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from lib.project.asset_rename import AssetRenameReport
from lib.project.project_manager import ProjectManager
from server import tool_runtime as tool_runtime_module
from server.tool_runtime import (
    CallerContext,
    CreateProjectToolRequest,
    NoArguments,
    ProjectScope,
    RenameAssetRequest,
    Services,
    ToolRequest,
    create_project,
    list_projects,
    rename_asset,
    retry_project_migration,
)


class _Unused:
    pass


def _services(tmp_path: Path) -> Services:
    return Services(
        projects=ProjectManager(tmp_path / "projects"),
        workflow_planner=_Unused(),
        capabilities=_Unused(),
    )


async def test_create_project_rolls_back_when_metadata_initialization_fails(tmp_path: Path) -> None:
    class FailingMetadataProjectManager(ProjectManager):
        def create_project_metadata(self, *_args, **_kwargs):
            raise OSError("disk full")

    projects_root = tmp_path / "projects"
    services = Services(
        projects=FailingMetadataProjectManager(projects_root),
        workflow_planner=_Unused(),
        capabilities=_Unused(),
    )
    request = ToolRequest(
        CreateProjectToolRequest(
            name="demo",
            title="Demo",
            content_mode="drama",
            generation_mode="storyboard",
        )
    )

    failed = await create_project(request, CallerContext(user_id="test", source="mcp"), services)

    assert failed.problem is not None
    assert failed.problem.code == "internal_error"
    assert not (projects_root / "demo").exists()
    ProjectManager(projects_root).create_project("demo")


async def test_create_project_settles_publication_before_propagating_cancellation(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingMetadataProjectManager(ProjectManager):
        def create_project_metadata(self, *args, **kwargs):
            started.set()
            release.wait()
            return super().create_project_metadata(*args, **kwargs)

    projects = BlockingMetadataProjectManager(tmp_path / "projects")
    services = Services(
        projects=projects,
        workflow_planner=_Unused(),
        capabilities=_Unused(),
    )
    caller = CallerContext(user_id="test", source="mcp")
    creation = asyncio.create_task(
        create_project(
            ToolRequest(CreateProjectToolRequest(name="demo", title="Demo")),
            caller,
            services,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 1)
        assert not projects.project_exists("demo")
        listed = await list_projects(ToolRequest(NoArguments()), caller, services)
        assert listed.value == []
        creation.cancel()
        await asyncio.sleep(0)
        assert not creation.done()
    finally:
        release.set()
        creation_results = await asyncio.wait_for(asyncio.gather(creation, return_exceptions=True), timeout=1)

    assert isinstance(creation_results[0], asyncio.CancelledError)
    assert projects.project_exists("demo")


async def test_list_projects_does_not_persist_readonly_migrations(tmp_path: Path) -> None:
    services = _services(tmp_path)
    services.projects.create_project("demo")
    services.projects.create_project_metadata("demo", "Demo")
    project_file = services.projects.get_project_path("demo") / "project.json"
    legacy = json.loads(project_file.read_text(encoding="utf-8"))
    legacy["style"] = "Anime"
    legacy.pop("style_template_id", None)
    project_file.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    before = project_file.read_bytes()

    outcome = await list_projects(
        ToolRequest(NoArguments()),
        CallerContext(user_id="test", source="mcp"),
        services,
    )

    assert outcome.problem is None
    assert outcome.value
    assert outcome.value[0]["name"] == "demo"
    assert project_file.read_bytes() == before


async def test_rename_asset_settles_write_before_propagating_cancellation(tmp_path: Path, monkeypatch) -> None:
    services = _services(tmp_path)
    services.projects.create_project("demo")
    services.projects.create_project_metadata("demo", "Demo")
    scope = ProjectScope(project_name="demo", data_root=services.projects.data_root)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_rename(_project: str, table: str, old_name: str, new_name: str) -> AssetRenameReport:
        started.set()
        release.wait()
        finished.set()
        return AssetRenameReport(table, old_name, new_name, 0, 0, 0, False)

    monkeypatch.setattr(services.projects, "rename_asset", blocking_rename)
    task = asyncio.create_task(
        rename_asset(
            ToolRequest(RenameAssetRequest(table="characters", old_name="A", new_name="B")),
            scope,
            CallerContext(user_id="test", source="mcp"),
            services,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        task_results = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1)

    assert isinstance(task_results[0], asyncio.CancelledError)
    assert finished.is_set()


async def test_retry_migration_settles_write_before_propagating_cancellation(tmp_path: Path, monkeypatch) -> None:
    services = _services(tmp_path)
    services.projects.create_project("demo")
    services.projects.create_project_metadata("demo", "Demo")
    scope = ProjectScope(project_name="demo", data_root=services.projects.data_root)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_migration(_project_dir: Path, **_options: object):
        started.set()
        release.wait()
        finished.set()
        return

    monkeypatch.setattr(tool_runtime_module, "migrate_project_with_verdict", blocking_migration)
    task = asyncio.create_task(
        retry_project_migration(
            ToolRequest(NoArguments()), scope, CallerContext(user_id="test", source="mcp"), services
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        task_results = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1)

    assert isinstance(task_results[0], asyncio.CancelledError)
    assert finished.is_set()


def test_create_project_request_rejects_removed_fields_before_writing(tmp_path: Path) -> None:
    """只有短剧工厂一种模式：已移除的按模式专属字段（如整片时长）传进来必须被拒，不能静默丢弃。"""
    services = _services(tmp_path)

    with pytest.raises(ValidationError):
        CreateProjectToolRequest.model_validate({"name": "demo", "content_mode": "drama", "target_duration": 30})

    assert services.projects.list_projects() == []
