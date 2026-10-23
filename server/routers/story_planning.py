"""短剧工厂系列工件的只读阅读接口。"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends

from lib.project.project_manager import ProjectManager, get_project_manager
from lib.short_drama_ingest import load_short_drama_artifacts
from server.dependencies import require_project_migration_ok

router = APIRouter(dependencies=[Depends(require_project_migration_ok)])
Manager = Annotated[ProjectManager, Depends(get_project_manager)]


@router.get("/projects/{project_name}/story-planning")
async def get_story_planning(project_name: str, pm: Manager) -> dict[str, object]:
    project = await asyncio.to_thread(pm.load_project, project_name)
    if project.get("content_mode") != "drama":
        return {"character_bible": None, "series_outline": None, "ledger": None, "episode_plans": []}
    artifacts = await asyncio.to_thread(load_short_drama_artifacts, pm, project_name)
    series = {artifact.path: artifact.as_dict() for artifact in artifacts if artifact.episode is None}
    plans = [artifact.as_dict() for artifact in artifacts if artifact.kind == "episode-plan"]
    return {
        "character_bible": series.get("character-bible.md"),
        "series_outline": series.get("series-outline.md"),
        "ledger": series.get("ledger.md"),
        "episode_plans": plans,
    }


@router.get("/projects/{project_name}/story-planning/context/{episode}")
async def get_story_context(project_name: str, episode: int, pm: Manager) -> dict[str, str]:
    project = await asyncio.to_thread(pm.load_project, project_name)
    if project.get("content_mode") == "drama":
        artifacts = await asyncio.to_thread(load_short_drama_artifacts, pm, project_name)
        selected = [
            artifact
            for artifact in artifacts
            if artifact.episode is None
            or (artifact.kind == "episode-plan" and artifact.episode <= episode)
            or (artifact.kind == "episode" and artifact.episode < episode)
        ]
        context = "\n\n".join(
            f"【工厂已有工件：source/short_drama/{artifact.path}】\n{artifact.content.decode('utf-8')}"
            for artifact in selected
        )
        return {"context": context}
    return {"context": ""}
