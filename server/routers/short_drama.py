"""短剧工厂剧本导入的 Web 入口。

与 Agent 的 ``import_short_drama_script`` / ``import_short_drama_series`` 调用同一个服务命令：把
short-drama-factory 的剧本 Markdown 转写为正式剧情演绎剧本并落盘。路由只负责把结果映射成 HTTP，
「动作/神态进 scene_description、台词进 utterances、单集超 60 秒按节拍切分多幕、视觉层留空、
短剧元信息进 short_drama_meta、集尾断章对齐 hook」等转写规则全在 lib.script.short_drama_import 与
Agent 工具里，与聊天式入口完全一致（不重述规则）。

前置：项目须为剧情演绎（drama），目标集须已存在（批量建空白集见 episode_management）。
"""

from typing import Annotated

from fastapi import APIRouter, Path as PathParam
from pydantic import BaseModel, ConfigDict, Field

from lib.infra.api_errors import NotFoundError, UnprocessableError
from lib.project.project_manager import get_project_manager
from server.agent_toolset.envelope import json_value
from server.auth import CurrentUser
from server.i18n import Translator
from server.tool_runtime import (
    CallerContext,
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


def _context(project_name: str, user_id: str) -> tuple[ProjectScope, CallerContext, Services]:
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


@router.post("/projects/{project_name}/short-drama/import")
async def import_one(
    project_name: str,
    req: ShortDramaImportRequest,
    user: CurrentUser,
    _t: Translator,
):
    """导入一集短剧工厂剧本：Markdown → 正式剧情演绎剧本并落盘，返回集号与场景数。"""
    scope, caller, services = _context(project_name, user.id)
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
    scope, caller, services = _context(project_name, user.id)
    outcome = await import_short_drama_series(
        ToolRequest(
            ImportShortDramaSeriesRequest(
                episodes=[{"episode_id": item.episode_id, "markdown": item.markdown} for item in req.episodes],
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
