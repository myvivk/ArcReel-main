"""集管理的 Web 入口：新建一集、调序与删除一集。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from lib.episode.episode_deletion import (
    EpisodeBatchDeletionResult,
    EpisodeDeletionResult,
    delete_episode,
    delete_episodes,
    render_episode_batch_deletion_text,
    render_episode_deletion_text,
)
from lib.episode.episode_management import EpisodeManagementError, create_episode, move_episode
from lib.generation.generation_queue import get_generation_queue
from lib.infra.api_errors import ApiError, NotFoundError
from lib.infra.async_thread import run_sync_transaction
from lib.project.project_change_hints import project_change_source
from lib.project.project_manager import ProjectManager, get_project_manager
from server.dependencies import require_project_migration_ok
from server.i18n import Translator
from server.services.tasks.episode_activity import episode_has_active_tasks as episode_tasks_active

logger = logging.getLogger(__name__)

router = APIRouter()

_STATUS: dict[str, int] = {"episode_not_found": 404}


class CreateEpisodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 插在哪一集之后（集 ID）；缺省时放在播出顺序末尾。
    after: int | None = None
    title: Annotated[str, Field(max_length=200)] = ""
    hook: Annotated[str, Field(max_length=2000)] = ""


class DeleteEpisodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 创作者看过的丢失清单的 ``revision``；缺省或与当前清单不符时只返回清单，不删除。
    revision: str | None = None


class DeleteEpisodesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 要删除的集 ID 列表。
    episodes: Annotated[list[Annotated[int, Field(gt=0)]], Field(min_length=1, max_length=500)]
    #: 创作者看过的全部丢失清单的合成 ``revision``；缺省或与当前不符时只返回清单，不删除。
    revision: str | None = None


class MoveEpisodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 移到哪一集之后（集 ID）；null 时移到最前。
    after: int | None


async def _run[T](name: str, _t: Translator, action: Callable[[ProjectManager], T], *, episode: int | None = None) -> T:
    def _sync() -> T:
        manager = get_project_manager()
        if not manager.project_exists(name):
            raise NotFoundError("project_not_found", name=name)
        with project_change_source("webui"):
            return action(manager)

    try:
        return await run_sync_transaction(_sync)
    except EpisodeManagementError as exc:
        raise HTTPException(status_code=_STATUS.get(exc.code, 409), detail=_t(f"episode_manage_{exc.code}")) from exc
    except (HTTPException, ApiError):
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


@router.post("/projects/{name}/episodes", dependencies=[Depends(require_project_migration_ok)], status_code=201)
async def create_episode_endpoint(name: str, req: CreateEpisodeRequest, _t: Translator) -> dict[str, Any]:
    """新建一集：分配新集 ID，插在 ``after`` 之后（缺省放在末尾）；正文由工厂工件入库提供。"""
    episode = await _run(
        name,
        _t,
        lambda manager: create_episode(
            manager,
            name,
            after=req.after,
            title=req.title,
            hook=req.hook,
        ),
    )
    return {"success": True, "episode": episode}


@router.post("/projects/{name}/episodes/{episode}/move", dependencies=[Depends(require_project_migration_ok)])
async def move_episode_endpoint(name: str, episode: int, req: MoveEpisodeRequest, _t: Translator) -> dict[str, Any]:
    """调整播出顺序：把这一集移到 ``after`` 之后。切出集之间保持源文顺序。"""
    await _run(name, _t, lambda manager: move_episode(manager, name, episode, after=req.after), episode=episode)
    return {"success": True}


async def episode_has_active_tasks(project_name: str, episode: int) -> bool:
    """这一集是否有排队或执行中的任务：任务的资源、剧本文件或载荷带这一集的集 ID。"""
    return await episode_tasks_active(get_generation_queue(), project_name, episode)


@router.post("/projects/{name}/episodes/{episode}/delete", dependencies=[Depends(require_project_migration_ok)])
async def delete_episode_endpoint(name: str, episode: int, req: DeleteEpisodeRequest, _t: Translator) -> dict[str, Any]:
    """删除一集（硬删除）。

    先不带 ``revision`` 调用，得到 ``status=confirmation_required`` 与服务端成文的丢失清单 ``impact.text``；
    创作者确认后带上 ``impact.revision`` 重新提交。清单在两次调用之间变了时再次返回确认，不删除。
    """

    if req.revision is not None and await episode_has_active_tasks(name, episode):
        raise HTTPException(status_code=409, detail=_t("episode_manage_tasks_active"))

    def _delete(manager: ProjectManager) -> dict[str, Any]:
        outcome = delete_episode(manager, name, episode, revision=req.revision)
        impact = outcome.impact.to_dict()
        if isinstance(outcome, EpisodeDeletionResult):
            return {"status": "deleted", "impact": impact}
        text = render_episode_deletion_text(impact, manager.load_project(name), _t)
        return {"status": "confirmation_required", "impact": {**impact, "text": text}}

    return await _run(name, _t, _delete, episode=episode)


@router.post("/projects/{name}/episodes/delete-batch", dependencies=[Depends(require_project_migration_ok)])
async def delete_episodes_endpoint(name: str, req: DeleteEpisodesRequest, _t: Translator) -> dict[str, Any]:
    """批量删除多集（硬删除），要么全部删除要么都不删。

    先不带 ``revision`` 调用，得到 ``status=confirmation_required`` 与全部集合并成文的丢失清单
    ``batch.text``；创作者确认后带上 ``batch.revision`` 重新提交。清单在两次调用之间变了时再次返回确认。
    """
    if req.revision is not None:
        for episode in req.episodes:
            if await episode_has_active_tasks(name, episode):
                raise HTTPException(status_code=409, detail=_t("episode_manage_tasks_active"))

    def _delete(manager: ProjectManager) -> dict[str, Any]:
        outcome = delete_episodes(manager, name, req.episodes, revision=req.revision)
        if isinstance(outcome, EpisodeBatchDeletionResult):
            return {"status": "deleted", "episodes": outcome.episodes}
        project = manager.load_project(name)
        text = render_episode_batch_deletion_text(outcome.impacts, project, _t)
        return {
            "status": "confirmation_required",
            "batch": {
                "episodes": outcome.episodes,
                "revision": outcome.revision,
                "recoverable": outcome.recoverable,
                "text": text,
            },
        }

    return await _run(name, _t, _delete)
