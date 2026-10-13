"""Authenticated story planning endpoints."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from lib.infra.async_thread import run_sync_transaction
from lib.project.project_manager import ProjectManager, get_project_manager
from lib.story.story_planning import (
    StoryPlan,
    StorySnapshot,
    authoring_context,
    extract_script_events,
    save_plan,
    snapshot,
)
from server.dependencies import require_project_migration_ok

router = APIRouter(dependencies=[Depends(require_project_migration_ok)])
Manager = Annotated[ProjectManager, Depends(get_project_manager)]


class StoryRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str


class StorySaveRequest(StoryRevisionRequest):
    plan: StoryPlan


@router.get("/projects/{project_name}/story-planning")
async def get_story_planning(project_name: str, pm: Manager) -> StorySnapshot:
    return await asyncio.to_thread(snapshot, pm, project_name)


@router.get("/projects/{project_name}/story-planning/context/{episode}")
async def get_story_context(project_name: str, episode: int, pm: Manager) -> dict[str, str]:
    return {"context": await asyncio.to_thread(authoring_context, pm, project_name, episode)}


@router.put("/projects/{project_name}/story-planning")
async def put_story_planning(project_name: str, req: StorySaveRequest, pm: Manager) -> StorySnapshot:
    return await run_sync_transaction(save_plan, pm, project_name, req.plan, req.revision)


@router.post("/projects/{project_name}/story-planning/extract")
async def extract_story_planning(project_name: str, req: StoryRevisionRequest, pm: Manager) -> StorySnapshot:
    return await run_sync_transaction(extract_script_events, pm, project_name, req.revision)
