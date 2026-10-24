"""Agent 工件入库保留七类原文，拒绝不完整的质量交付。"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from lib.episode.episode_management import create_episode
from lib.project.project_manager import ProjectManager
from server.agent_toolset.script_authoring import SCRIPT_AUTHORING_TOOLS
from server.tool_runtime import (
    CallerContext,
    IngestShortDramaArtifactsRequest,
    ProjectScope,
    Services,
    ToolRequest,
    ingest_short_drama_artifacts_tool,
)
from tests.short_drama_support import factory_package

pytestmark = pytest.mark.usefixtures("factory_profile")


class _NoCapabilities:
    async def video_capabilities_for_project(self, project):
        raise AssertionError("artifact ingestion must not resolve video capabilities")


def _services(tmp_path: Path) -> Services:
    manager = ProjectManager(tmp_path / "projects")
    manager.create_project("demo", content_mode="drama")
    manager.create_project_metadata("demo", "Demo", "Anime", "drama")
    create_episode(manager, "demo")
    return Services(projects=manager, workflow_planner=object(), capabilities=_NoCapabilities())


def _request(package):
    return ToolRequest(
        IngestShortDramaArtifactsRequest(
            artifacts=[{"path": path, "content_base64": base64.b64encode(raw).decode("ascii")} for path, raw in package]
        )
    )


async def test_agent_ingests_original_artifact_bytes_without_native_script(tmp_path):
    services = _services(tmp_path)
    package = factory_package()
    outcome = await ingest_short_drama_artifacts_tool(
        _request(package),
        ProjectScope(project_name="demo", data_root=services.projects.data_root),
        CallerContext(user_id="test", source="mcp"),
        services,
    )
    assert outcome.problem is None
    assert outcome.value["imported"] == 7
    project_path = services.projects.get_project_path("demo")
    for path, raw in package:
        assert (project_path / "source/short_drama" / path).read_bytes() == raw
    assert not (project_path / "scripts/episode_1.json").exists()
    project = services.projects.load_project("demo")
    assert project["episodes"][0]["source_origin"] == "own"
    assert not project.get("characters")


async def test_agent_rejects_incomplete_quality_package_without_writing(tmp_path):
    services = _services(tmp_path)
    outcome = await ingest_short_drama_artifacts_tool(
        _request([item for item in factory_package() if not item[0].startswith("compliance/")]),
        ProjectScope(project_name="demo", data_root=services.projects.data_root),
        CallerContext(user_id="test", source="mcp"),
        services,
    )
    assert outcome.problem is not None
    assert outcome.problem.code == "short_drama_import_refused"
    assert not list((services.projects.get_project_path("demo") / "source/short_drama").rglob("*.md"))


def test_legacy_markdown_conversion_tools_are_not_exposed():
    names = {tool.name for tool in SCRIPT_AUTHORING_TOOLS}
    assert "ingest_short_drama_artifacts" in names
    assert "import_short_drama_script" not in names
    assert "import_short_drama_series" not in names
