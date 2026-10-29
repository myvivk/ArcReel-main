"""导入原文路由：生成剧本、读原文、存原文。

只服务 ``creation_mode == "source_import"`` 的项目；模式判定与错误码在服务层收口，
本模块只做请求模型、项目上下文与变更来源标记。网关鉴权由 ``app.include_router`` 统一挂载。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict

from lib.project.project_change_hints import project_change_source
from lib.project.project_manager import get_project_manager
from server.auth import CurrentUser
from server.dependencies import require_project_migration_ok
from server.services.source_import import import_source_episode, load_original, save_original

router = APIRouter(dependencies=[Depends(require_project_migration_ok)])


class SourceImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    #: 首次导入传 null；重新导入传当前原文或源稿 revision（前端传已保存原文的 revision）。
    expected_revision: str | None = None
    #: 本集已有分镜图/视频时必须显式确认为 true，否则 409。
    confirm_artifacts: bool = False


class SaveOriginalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    expected_revision: str | None = None


@router.post("/projects/{project_name}/episodes/{episode}/source-import")
async def import_episode_source(
    project_name: str,
    episode: Annotated[int, PathParam(ge=1)],
    req: SourceImportRequest,
    user: CurrentUser,
) -> dict[str, object]:
    """把本集原文改写为工厂单集格式剧本并落盘。

    结构校验失败 422 且不落盘；机检 FAIL 先返修一次，仍失败则落盘并在 ``quality_warnings`` 回带。
    源稿/原文 revision 与提交前不一致时 409，避免覆盖并发写入。
    """

    with project_change_source("webui"):
        result = await import_source_episode(
            projects=get_project_manager(),
            project_name=project_name,
            episode=episode,
            text=req.text,
            expected_revision=req.expected_revision,
            confirm_artifacts=req.confirm_artifacts,
            user_id=user.id,
        )
    return result.as_dict()


@router.get("/projects/{project_name}/episodes/{episode}/source-import/original")
async def get_imported_original(
    project_name: str,
    episode: Annotated[int, PathParam(ge=1)],
) -> dict[str, object]:
    """读取本集已保存原文与「原文已改、剧本未重生成」状态；没有原文时 404。"""

    view = await load_original(
        projects=get_project_manager(),
        project_name=project_name,
        episode=episode,
    )
    return view.as_dict()


@router.put("/projects/{project_name}/episodes/{episode}/source-import/original")
async def put_imported_original(
    project_name: str,
    episode: Annotated[int, PathParam(ge=1)],
    req: SaveOriginalRequest,
) -> dict[str, object]:
    """只保存原文，不重新生成剧本；``stale`` 表示剧本尚未按当前原文重生成。"""

    with project_change_source("webui"):
        saved = await save_original(
            projects=get_project_manager(),
            project_name=project_name,
            episode=episode,
            text=req.text,
            expected_revision=req.expected_revision,
        )
    return saved.as_dict()
