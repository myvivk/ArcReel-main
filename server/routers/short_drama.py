"""短剧工厂 Markdown 工件的零处理导入与兼容源稿路由。"""

import asyncio
from dataclasses import asdict
from typing import Annotated, Never

from fastapi import APIRouter
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field

from lib.infra.api_errors import ConflictError, NotFoundError, UnprocessableError
from lib.infra.async_thread import run_sync_transaction
from lib.short_drama_ingest import (
    ShortDramaArtifactError,
    decode_artifact_content,
    ingest_short_drama_artifacts,
    load_short_drama_artifacts,
)
from lib.project.project_manager import get_project_manager
from lib.script.short_drama_source import (
    ShortDramaSourceConflict,
    load_short_drama_source,
    save_short_drama_source,
    short_drama_source_relative_path,
)
from server.agent_toolset.envelope import json_value
from server.auth import CurrentUser
from server.i18n import Translator
from server.services.short_drama_workflow import ShortDramaWorkflowError, import_short_drama_episode
from server.tool_runtime import (
    CallerContext,
    ImportShortDramaEpisodeItem,
    ImportShortDramaScriptRequest,
    ImportShortDramaSeriesRequest,
    ProjectScope,
    Services,
    ToolProblem,
    ToolRequest,
    import_short_drama_script,
    import_short_drama_series,
)

router = APIRouter()


class ShortDramaImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode_id: Annotated[int, Field(ge=1)] = Field(description="导入到哪一集（集 ID，须已存在）")
    markdown: str = Field(description="该集的 short-drama-factory 剧本 Markdown 原文")


class ShortDramaSeriesItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode_id: Annotated[int, Field(ge=1)] = Field(description="导入到哪一集（集 ID，须已存在）")
    markdown: str = Field(description="该集的 short-drama-factory 剧本 Markdown 原文")


class ShortDramaImportBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episodes: list[ShortDramaSeriesItem] = Field(description="本批要导入的多集剧本（每集一份 Markdown）")
    continue_on_error: bool = Field(
        default=True, description="单集解析/落盘失败时是否跳过继续，而非整体失败"
    )


class ShortDramaSourceUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markdown: str
    expected_revision: str | None = None


class ShortDramaReimportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_revision: str = Field(min_length=1)
    confirm_artifacts: bool = False


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


def _raise_problem(problem: ToolProblem, project_name: str, _t: Translator) -> None:
    if problem.code == "project_not_found":
        raise NotFoundError("project_not_found", name=project_name)
    # invalid_request（内容模式不匹配）与其余一律按 422：转写失败信息经 detail 透出
    raise UnprocessableError("short_drama_import_refused", reason=problem.detail).with_diagnostic(problem.detail)


def _raise_workflow_error(exc: ShortDramaWorkflowError, project_name: str, episode: int) -> Never:
    if exc.code == "project_not_found":
        raise NotFoundError("project_not_found", name=project_name) from exc
    if exc.code == "episode_not_found":
        raise NotFoundError("episode_not_found", episode=episode) from exc
    if exc.code == "short_drama_source_missing":
        raise NotFoundError("short_drama_source_missing") from exc
    if exc.code == "short_drama_source_conflict":
        raise ConflictError("short_drama_source_conflict") from exc
    if exc.code == "short_drama_reimport_confirmation_required":
        raise ConflictError("short_drama_reimport_confirmation_required") from exc
    raise UnprocessableError("short_drama_import_refused", reason=exc.detail).with_diagnostic(exc.detail) from exc


def _require_source_episode(project_name: str, episode: int) -> None:
    manager = get_project_manager()
    try:
        project = manager.load_project(project_name)
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=project_name) from exc
    if project.get("content_mode") != "drama":
        raise UnprocessableError("short_drama_import_refused", reason="not a drama project")
    if not any(
        isinstance(entry, dict) and entry.get("episode") == episode
        for entry in project.get("episodes") or []
    ):
        raise NotFoundError("episode_not_found", episode=episode)


@router.get("/projects/{project_name}/episodes/{episode}/short-drama-source")
async def get_source(project_name: str, episode: Annotated[int, PathParam(ge=1)]):
    """读取本集短剧工厂 Markdown 源稿；存量导入未保留源稿时返回 exists=false。"""
    await asyncio.to_thread(_require_source_episode, project_name, episode)
    source = await asyncio.to_thread(load_short_drama_source, get_project_manager(), project_name, episode)
    if source is None:
        return {
            "episode": episode,
            "path": short_drama_source_relative_path(episode),
            "markdown": "",
            "revision": None,
            "exists": False,
        }
    return {**asdict(source), "exists": True}


@router.put("/projects/{project_name}/episodes/{episode}/short-drama-source")
async def update_source(
    project_name: str,
    episode: Annotated[int, PathParam(ge=1)],
    req: ShortDramaSourceUpdateRequest,
):
    """只保存 Markdown 源稿；解析失败的草稿也可保存，不改正式剧本。"""
    await asyncio.to_thread(_require_source_episode, project_name, episode)
    try:
        source = await run_sync_transaction(
            save_short_drama_source,
            get_project_manager(),
            project_name,
            episode,
            req.markdown,
            expected_revision=req.expected_revision,
        )
    except ShortDramaSourceConflict as exc:
        raise ConflictError("short_drama_source_conflict") from exc
    return {**asdict(source), "exists": True}


@router.post("/projects/{project_name}/episodes/{episode}/short-drama-source/reimport")
async def reimport_source(
    project_name: str,
    episode: Annotated[int, PathParam(ge=1)],
    req: ShortDramaReimportRequest,
    user: CurrentUser,
):
    """从已保存源稿重建正式脚本；revision 防止并发覆盖，下游产物需显式确认。"""
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
            save_source=False,
            expected_source_revision=req.source_revision,
            confirm_artifacts=req.confirm_artifacts,
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
            [
                (item.path, decode_artifact_content(None, item.content_base64))
                for item in req.artifacts
            ],
        )
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=project_name) from exc
    except ShortDramaArtifactError as exc:
        raise UnprocessableError("short_drama_import_refused", reason=str(exc)).with_diagnostic(str(exc)) from exc
    return {"artifacts": [artifact.as_dict(include_content=False) for artifact in artifacts]}


@router.post("/projects/{project_name}/short-drama/import")
async def import_one(
    project_name: str,
    req: ShortDramaImportRequest,
    user: CurrentUser,
    _t: Translator,
):
    """导入一集短剧工厂剧本：Markdown → 正式剧情演绎剧本并落盘，返回集号与场景数。"""
    scope, caller, services = short_drama_context(project_name, user.id)
    outcome = await import_short_drama_script(
        ToolRequest(ImportShortDramaScriptRequest(episode_id=req.episode_id, markdown=req.markdown)),
        scope,
        caller,
        services,
    )
    if outcome.problem is not None:
        _raise_problem(outcome.problem, project_name, _t)
    return json_value(outcome.value)


@router.post("/projects/{project_name}/short-drama/import-batch")
async def import_batch(
    project_name: str,
    req: ShortDramaImportBatchRequest,
    user: CurrentUser,
    _t: Translator,
):
    """批量导入多集短剧工厂剧本：各集独立并发转写落盘，返回导入/失败计数与各集明细。"""
    scope, caller, services = short_drama_context(project_name, user.id)
    outcome = await import_short_drama_series(
        ToolRequest(
            ImportShortDramaSeriesRequest(
                episodes=[
                    ImportShortDramaEpisodeItem(episode_id=item.episode_id, markdown=item.markdown)
                    for item in req.episodes
                ],
                continue_on_error=req.continue_on_error,
            )
        ),
        scope,
        caller,
        services,
    )
    if outcome.problem is not None:
        _raise_problem(outcome.problem, project_name, _t)
    return json_value(outcome.value)
