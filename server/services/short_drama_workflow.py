"""短剧工厂 Markdown 到正式剧本的统一导入工作流。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from lib.episode.episode_paths import episode_script_filename
from lib.infra.async_thread import run_sync_transaction
from lib.project.asset_types import register_missing_assets
from lib.project.project_manager import ProjectManager
from lib.script.script_models import DramaEpisodeScript
from lib.script.short_drama_import import (
    STORY_SCENE_WINDOW_SECONDS,
    convert_to_drama_script,
    parse_short_drama_episode,
    referenced_asset_descriptions,
    referenced_asset_names,
)
from lib.script.short_drama_source import (
    ShortDramaSourceConflict,
    load_short_drama_source,
    replace_short_drama_source,
)


class ShortDramaWorkflowError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ShortDramaImportResult:
    episode: int
    scenes: int
    source_path: str
    source_revision: str
    registered_assets: dict[str, list[str]]
    asset_conflicts: list[dict[str, str]]
    declared_seconds: int | None = None
    actual_seconds: int | None = None
    duration_shortfall: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        result = {
            "episode": self.episode,
            "scenes": self.scenes,
            "source_path": self.source_path,
            "source_revision": self.source_revision,
        }
        if self.registered_assets:
            result["registered_assets"] = self.registered_assets
        if self.asset_conflicts:
            result["asset_conflicts"] = self.asset_conflicts
        if self.declared_seconds is not None:
            result.update(
                declared_seconds=self.declared_seconds,
                actual_seconds=self.actual_seconds,
                duration_shortfall=self.duration_shortfall,
            )
        return result


def _require_episode(project: dict[str, Any], episode: int) -> None:
    if project.get("content_mode") != "drama":
        raise ShortDramaWorkflowError("invalid_request", "短剧工厂导入仅适用于剧情演绎（drama）项目")
    episodes = project.get("episodes")
    if not isinstance(episodes, list) or not any(
        isinstance(entry, dict) and entry.get("episode") == episode for entry in episodes
    ):
        raise ShortDramaWorkflowError("episode_not_found", f"第 {episode} 集不存在")


def script_has_media_artifacts(script: dict[str, Any]) -> bool:
    for scene in script.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        generated = scene.get("generated_assets")
        if isinstance(generated, dict) and any(generated.get(key) for key in ("storyboard_image", "video_clip")):
            return True
    return False


async def import_short_drama_episode(
    *,
    project_name: str,
    episode: int,
    markdown: str,
    projects: ProjectManager,
    save_source: bool = True,
    expected_source_revision: str | None = None,
    confirm_artifacts: bool = True,
) -> ShortDramaImportResult:
    """解析源稿并提交正式剧本；Web 与 Agent 入口共用此命令。"""
    try:
        project = await asyncio.to_thread(projects.load_project, project_name)
    except FileNotFoundError as exc:
        raise ShortDramaWorkflowError("project_not_found", str(exc)) from exc
    _require_episode(project, episode)

    if not confirm_artifacts:
        entry = next(item for item in project["episodes"] if item.get("episode") == episode)
        script_file = entry.get("script_file")
        if isinstance(script_file, str) and script_file:
            try:
                current_script = await asyncio.to_thread(projects.load_script, project_name, script_file)
            except FileNotFoundError:
                pass
            else:
                if script_has_media_artifacts(current_script):
                    raise ShortDramaWorkflowError(
                        "short_drama_reimport_confirmation_required",
                        "本集已有分镜图或视频，重新导入后这些产物会变为过期状态",
                    )

    try:
        parsed = parse_short_drama_episode(markdown)
        script = convert_to_drama_script(
            parsed,
            episode,
            # 导入阶段不知道用户最终用哪个模型生成视频，按剧情窗口切幕、不做档位吸附；
            # 档位对齐交给生成期的预检（见 STORY_SCENE_WINDOW_SECONDS 的说明）。
            scene_window=STORY_SCENE_WINDOW_SECONDS,
        )
        core = {key: value for key, value in script.items() if key != "short_drama_meta"}
        DramaEpisodeScript.model_validate(core)
    except ShortDramaWorkflowError:
        raise
    except Exception as exc:
        raise ShortDramaWorkflowError("short_drama_parse_failed", f"Markdown 解析失败: {exc}") from exc

    if save_source:
        source = await run_sync_transaction(
            replace_short_drama_source,
            projects,
            project_name,
            episode,
            markdown,
        )
    else:
        source = await asyncio.to_thread(load_short_drama_source, projects, project_name, episode)
        if source is None:
            raise ShortDramaWorkflowError("short_drama_source_missing", "本集没有已保存的 Markdown 源稿")
        if source.revision != expected_source_revision:
            raise ShortDramaWorkflowError(
                "short_drama_source_conflict",
                "Markdown 源稿已被其他修改更新，请刷新后重试",
            )
        if source.markdown != markdown:
            raise ShortDramaWorkflowError(
                "short_drama_source_conflict",
                "本次导入内容与已保存的 Markdown 源稿不一致",
            )

    meta = script.setdefault("short_drama_meta", {})
    meta["source_path"] = source.path
    meta["source_revision"] = source.revision
    names = referenced_asset_names(script)
    descriptions = referenced_asset_descriptions(parsed)
    registered: dict[str, list[str]] = {}
    conflicts: list[dict[str, str]] = []

    def _update_project(current_project: dict[str, Any]) -> None:
        current_source = load_short_drama_source(projects, project_name, episode)
        if current_source is None or current_source.revision != source.revision:
            raise ShortDramaSourceConflict("short drama source changed during script commit")
        new_assets, new_conflicts = register_missing_assets(
            current_project,
            names,
            descriptions_by_type=descriptions,
        )
        registered.update(new_assets)
        conflicts.extend(new_conflicts)

    try:
        await run_sync_transaction(
            projects.save_script,
            project_name,
            script,
            episode_script_filename(episode),
            project_update=_update_project,
        )
    except ShortDramaSourceConflict as exc:
        raise ShortDramaWorkflowError(
            "short_drama_source_conflict",
            "Markdown 源稿已被其他修改更新，请刷新后重试",
        ) from exc
    except Exception as exc:
        raise ShortDramaWorkflowError("short_drama_save_failed", f"剧本落盘失败: {exc}") from exc

    declared = int(getattr(parsed, "total_duration", 0) or 0)
    actual = sum(int(scene.get("duration_seconds") or 0) for scene in script.get("scenes", []))
    return ShortDramaImportResult(
        episode=episode,
        scenes=len(script["scenes"]),
        source_path=source.path,
        source_revision=source.revision,
        registered_assets=registered,
        asset_conflicts=conflicts,
        declared_seconds=declared if declared and actual != declared else None,
        actual_seconds=actual if declared and actual != declared else None,
        duration_shortfall=actual < declared if declared and actual != declared else None,
    )


__all__ = [
    "ShortDramaImportResult",
    "ShortDramaWorkflowError",
    "import_short_drama_episode",
    "script_has_media_artifacts",
]
