"""短剧工厂 Markdown 工件入库、只读源稿与 AI 分镜路由。"""

import asyncio
from dataclasses import asdict
from typing import Annotated, Any, Never

from fastapi import APIRouter
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field

from lib.infra.api_errors import ConflictError, NotFoundError, UnprocessableError
from lib.infra.async_thread import run_sync_transaction
from lib.project.project_manager import get_project_manager
from lib.script.short_drama_import import assets_missing_description
from lib.script.short_drama_source import load_short_drama_source, short_drama_source_relative_path
from lib.short_drama_ingest import (
    ShortDramaArtifactError,
    decode_artifact_content,
    ingest_short_drama_artifacts,
    load_short_drama_artifacts,
)
from server.auth import CurrentUser
from server.services.short_drama_workflow import (
    ShortDramaWorkflowError,
    import_short_drama_episode,
)
from server.tool_runtime import (
    CallerContext,
    ProjectScope,
    Services,
)

router = APIRouter()


class ShortDramaReimportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_revision: str = Field(min_length=1)
    confirm_artifacts: bool = False
    planning_strength: int = Field(default=2, ge=1, le=3)
    force_regenerate: bool = False
    #: 非空时只重生成这些分镜（scene_id），其余分镜原样保留；空/缺省 = 整体 AI 分镜。
    regenerate_scene_ids: list[str] = Field(default_factory=list)


class ShortDramaArtifactItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    content_base64: str = Field(min_length=1)


class ShortDramaArtifactImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifacts: list[ShortDramaArtifactItem] = Field(min_length=1)


def short_drama_context(project_name: str, user_id: str) -> tuple[ProjectScope, CallerContext, Services]:
    pm = get_project_manager()
    return (
        ProjectScope(project_name=project_name, data_root=pm.data_root),
        CallerContext(user_id=user_id, source="webui"),
        Services.defaults(pm),
    )


def _raise_workflow_error(exc: ShortDramaWorkflowError, project_name: str, episode: int) -> Never:
    if exc.code == "project_not_found":
        raise NotFoundError("project_not_found", name=project_name) from exc
    if exc.code == "episode_not_found":
        raise NotFoundError("episode_not_found", episode=episode) from exc
    if exc.code == "short_drama_source_missing":
        raise NotFoundError("short_drama_source_missing") from exc
    if exc.code == "short_drama_source_conflict":
        raise ConflictError("short_drama_source_conflict") from exc
    if exc.code == "short_drama_script_conflict":
        raise ConflictError("short_drama_script_conflict") from exc
    if exc.code == "short_drama_reimport_confirmation_required":
        raise ConflictError("short_drama_reimport_confirmation_required") from exc
    raise UnprocessableError("short_drama_import_refused", reason=exc.detail).with_diagnostic(exc.detail) from exc


def _require_source_episode(project_name: str, episode: int) -> dict[str, Any]:
    manager = get_project_manager()
    try:
        project = manager.load_project(project_name)
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=project_name) from exc
    if project.get("content_mode") != "drama":
        raise UnprocessableError("short_drama_import_refused", reason="not a drama project")
    if not any(isinstance(entry, dict) and entry.get("episode") == episode for entry in project.get("episodes") or []):
        raise NotFoundError("episode_not_found", episode=episode)
    return project


@router.get("/projects/{project_name}/episodes/{episode}/short-drama-source")
async def get_source(project_name: str, episode: Annotated[int, PathParam(ge=1)]):
    """读取本集短剧工厂 Markdown 源稿；存量导入未保留源稿时返回 exists=false。

    同时回带 ``assets_missing_description``：本集源稿引用的资产里缺外观描述的清单，
    分镜页据此在缺描述时置灰「AI 分镜」入口并提示先补齐。
    """
    project = await asyncio.to_thread(_require_source_episode, project_name, episode)
    source = await asyncio.to_thread(load_short_drama_source, get_project_manager(), project_name, episode)
    if source is None:
        return {
            "episode": episode,
            "path": short_drama_source_relative_path(episode),
            "markdown": "",
            "revision": None,
            "exists": False,
            "assets_missing_description": [],
        }
    missing = await asyncio.to_thread(assets_missing_description, project, source.markdown)
    return {**asdict(source), "exists": True, "assets_missing_description": missing}


@router.post("/projects/{project_name}/episodes/{episode}/short-drama-source/reimport")
async def reimport_source(
    project_name: str,
    episode: Annotated[int, PathParam(ge=1)],
    req: ShortDramaReimportRequest,
    user: CurrentUser,
):
    """从已保存工厂源稿生成视觉制作状态；revision 防止并发覆盖，下游产物需显式确认。"""
    source = await asyncio.to_thread(load_short_drama_source, get_project_manager(), project_name, episode)
    if source is None:
        raise NotFoundError("short_drama_source_missing")
    if source.revision != req.source_revision:
        raise ConflictError("short_drama_source_conflict")
    _scope, _caller, services = short_drama_context(project_name, user.id)
    try:
        result = await import_short_drama_episode(
            project_name=project_name,
            episode=episode,
            markdown=source.markdown,
            projects=services.projects,
            expected_source_revision=req.source_revision,
            confirm_artifacts=req.confirm_artifacts,
            planning_strength=req.planning_strength,
            force_regenerate=req.force_regenerate,
            regenerate_scene_ids=req.regenerate_scene_ids,
        )
    except ShortDramaWorkflowError as exc:
        _raise_workflow_error(exc, project_name, episode)
    return result.as_dict()


@router.get("/projects/{project_name}/short-drama/artifacts")
async def get_artifacts(project_name: str):
    manager = get_project_manager()
    try:
        project = await asyncio.to_thread(manager.load_project, project_name)
        if project.get("content_mode") != "drama":
            raise UnprocessableError("short_drama_import_refused", reason="not a drama project")
        artifacts = await asyncio.to_thread(load_short_drama_artifacts, manager, project_name)
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=project_name) from exc
    return {"artifacts": [artifact.as_dict() for artifact in artifacts]}


@router.post("/projects/{project_name}/short-drama/import-artifacts")
async def import_artifacts(project_name: str, req: ShortDramaArtifactImportRequest):
    manager = get_project_manager()
    try:
        project = await asyncio.to_thread(manager.load_project, project_name)
        if project.get("content_mode") != "drama":
            raise UnprocessableError("short_drama_import_refused", reason="not a drama project")
        artifacts = await run_sync_transaction(
            ingest_short_drama_artifacts,
            manager,
            project_name,
            [(item.path, decode_artifact_content(None, item.content_base64)) for item in req.artifacts],
        )
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=project_name) from exc
    except ShortDramaArtifactError as exc:
        raise UnprocessableError("short_drama_import_refused", reason=str(exc)).with_diagnostic(str(exc)) from exc
    return {"artifacts": [artifact.as_dict(include_content=False) for artifact in artifacts]}
