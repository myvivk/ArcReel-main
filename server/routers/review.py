"""只读评测入口：分镜提示词评分与视频评测，不写项目、不入队。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from lib.episode.episode_paths import episode_script_filename
from lib.infra.media_probe import MediaProbeError, probe_video_frame_times
from lib.project.project_manager import get_project_manager
from lib.script.grid.keyframe_parser import shot_scene_text
from lib.script.grid.shot_grid_manager import ShotGridManager
from lib.script.grid.video_prompt_targets import video_prompt_text
from lib.script.prompt_review import review_storyboard_prompt
from lib.script.reference_review import review_reference_material
from lib.script.shot_prompt_standard import (
    handoff_problem,
    seam_prop_problem,
    video_prompt_delta_metrics,
)
from lib.script.storyboard_sequence import get_storyboard_items
from lib.video_review.signals import SignalDetectionError, signals_for
from server.auth import CurrentUser

router = APIRouter()


@router.get("/projects/{project_name}/episodes/{episode}/storyboard-score")
async def get_storyboard_score(project_name: str, episode: int, user: CurrentUser):
    """逐条分镜提示词评分；只读，不改变任何脚本或产物。"""

    pm = get_project_manager()
    filename = episode_script_filename(episode)
    script = await asyncio.to_thread(pm.load_script, project_name, filename)
    project = await asyncio.to_thread(pm.load_project, project_name)
    items, id_field, _, _, _ = get_storyboard_items(script)
    characters = {str(name) for name in (project.get("characters") or {})}
    props = {str(name) for name in (project.get("props") or {})}
    registered = characters | props | {str(name) for name in (project.get("scenes") or {})}
    scenes = []
    previous_text: str | None = None
    for item in items:
        scene_text = shot_scene_text(item)
        review = review_storyboard_prompt(
            scene_text,
            duration_seconds=item.get("duration_seconds"),
            registered_names=registered,
            character_names=characters,
            registered_props=props,
            previous_text=previous_text,
        )
        scenes.append({"scene_id": str(item.get(id_field, "")), **review})
        previous_text = scene_text
    return {"episode": episode, "scenes": scenes}


@router.get("/projects/{project_name}/episodes/{episode}/reference-review")
async def get_reference_review(project_name: str, episode: int, shot_id: str, user: CurrentUser):
    """单条参考素材评测：布局、帧链、资产 token、跨镜接缝；视觉维度未测。"""

    pm = get_project_manager()
    filename = episode_script_filename(episode)
    script = await asyncio.to_thread(pm.load_script, project_name, filename)
    project = await asyncio.to_thread(pm.load_project, project_name)
    project_path = pm.get_project_path(project_name)
    items, id_field, _, _, _ = get_storyboard_items(script)
    index = next((i for i, item in enumerate(items) if str(item.get(id_field, "")) == shot_id), None)
    if index is None:
        return {"status": "shot_missing", "shot_id": shot_id, "score": 0, "dimensions": [], "defects": [], "advice": []}
    scene = items[index]
    assets = scene.get("generated_assets") if isinstance(scene.get("generated_assets"), dict) else {}
    grid_id = (assets.get("shot_grid") or {}).get("grid_id") if isinstance(assets, dict) else None
    manager = ShotGridManager(project_path)
    grid = manager.get(str(grid_id)) if isinstance(grid_id, str) and grid_id else None
    if grid is None:
        # 重新分镜会清空 generated_assets 绑定；生成成功的 completed 宫格仍应可评。
        candidates = [
            candidate
            for candidate in manager.list_all()
            if candidate.shot_id == shot_id
            and candidate.script_file == filename
            and candidate.episode == episode
            and candidate.status in {"completed", "confirmed"}
        ]
        candidates.sort(key=lambda candidate: candidate.updated_at or "", reverse=True)
        grid = candidates[0] if candidates else None
    cells = list(grid.cells) if grid is not None else []
    cell_prompts = [cell.prompt for cell in cells]
    cell_files_exist = [bool(cell.image_path and (project_path / cell.image_path).is_file()) for cell in cells]
    previous_last_cell: str | None = None
    if index > 0:
        previous_assets = items[index - 1].get("generated_assets")
        previous_id = (
            (previous_assets.get("shot_grid") or {}).get("grid_id") if isinstance(previous_assets, dict) else None
        )
        previous_grid = (
            ShotGridManager(project_path).get(str(previous_id))
            if isinstance(previous_id, str) and previous_id
            else None
        )
        if previous_grid is not None and previous_grid.cells:
            previous_last_cell = previous_grid.cells[-1].prompt
    characters = {str(name) for name in (project.get("characters") or {})}
    props = {str(name) for name in (project.get("props") or {})}
    scenes = {str(name) for name in (project.get("scenes") or {})}
    result = review_reference_material(
        cell_prompts=cell_prompts,
        cell_files_exist=cell_files_exist,
        rows=grid.rows if grid is not None else 0,
        cols=grid.cols if grid is not None else 0,
        status=grid.status if grid is not None else "missing",
        previous_last_cell=previous_last_cell,
        registered_names=characters | props | scenes,
        character_names=characters,
        prop_names=props,
    )
    return {"shot_id": shot_id, **result}


@router.get("/projects/{project_name}/episodes/{episode}/video-review")
async def get_video_review(project_name: str, episode: int, shot_id: str, user: CurrentUser):
    """单条视频的技术评测：黑帧/冻结/切点 + 提示词 + 跨镜连续性 + 版本时效。

    当前没有服务端 VLM/音频分析，不设“视觉一致性”“音频”维度，也不展示未测项。
    """

    pm = get_project_manager()
    filename = episode_script_filename(episode)
    script = await asyncio.to_thread(pm.load_script, project_name, filename)
    project = await asyncio.to_thread(pm.load_project, project_name)
    project_path = pm.get_project_path(project_name)
    items, id_field, _, _, _ = get_storyboard_items(script)
    index = next((i for i, item in enumerate(items) if str(item.get(id_field, "")) == shot_id), None)
    if index is None:
        return {"status": "shot_missing", "shot_id": shot_id, "score": 0, "dimensions": [], "defects": [], "advice": []}
    scene = items[index]
    assets = scene.get("generated_assets") if isinstance(scene.get("generated_assets"), dict) else {}
    video_clip = assets.get("video_clip") if isinstance(assets, dict) else None
    video_path = project_path / str(video_clip) if isinstance(video_clip, str) and video_clip else None
    duration = int(scene.get("duration_seconds") or 0)
    defects: list[dict[str, object]] = []
    advice: list[str] = []

    technical_score = 0
    structure_score = 0
    if video_path is not None and video_path.is_file():
        try:
            frame_times = await probe_video_frame_times(video_path)
            cache = project_path / ".cache" / "video_signals" / "videos" / f"{shot_id}_review.json"
            cache.parent.mkdir(parents=True, exist_ok=True)
            signals, _ = await signals_for(video_path, frame_times, cache_file=cache)
            black = sum(span.end - span.start for span in signals.black)
            freeze = sum(span.end - span.start for span in signals.freeze)
            technical_score = 20
            if black:
                technical_score -= min(12, 6 + round(black * 4))
                defects.append({"id": "black_frames", "severity": "high", "message": f"检测到黑帧 {black:.1f}s"})
            if freeze:
                technical_score -= min(10, 4 + round(freeze * 4))
                defects.append({"id": "freeze_frames", "severity": "medium", "message": f"检测到冻结 {freeze:.1f}s"})
            technical_score = max(0, technical_score)
            structure_score = 15
            if duration and abs(signals.duration_seconds - duration) > 1.5:
                structure_score -= 5
                defects.append(
                    {
                        "id": "duration_mismatch",
                        "severity": "low",
                        "message": f"成片 {signals.duration_seconds:.1f}s，计划 {duration}s",
                    }
                )
            if signals.cuts and len(signals.cuts) + 1 > max(1, duration // 2):
                structure_score -= 3
                defects.append(
                    {
                        "id": "too_many_cuts",
                        "severity": "low",
                        "message": f"{len(signals.cuts)} 个切点，镜头可能过碎",
                    }
                )
            structure_score = max(0, structure_score)
        except (MediaProbeError, SignalDetectionError):
            defects.append({"id": "video_unreadable", "severity": "blocker", "message": "视频无法解码"})
    else:
        defects.append({"id": "video_missing", "severity": "high", "message": "该分镜还没有视频"})

    action = video_prompt_text(scene)
    grid_id = (assets.get("shot_grid") or {}).get("grid_id") if isinstance(assets, dict) else None
    grid = ShotGridManager(project_path).get(str(grid_id)) if isinstance(grid_id, str) and grid_id else None
    references = tuple(cell.prompt for cell in grid.cells) if grid is not None else ()
    characters = {str(name) for name in (project.get("characters") or {})}
    props = {str(name) for name in (project.get("props") or {})}
    registered_names = characters | props | {str(name) for name in (project.get("scenes") or {})}
    scene_characters = scene.get("characters_in_scene")
    scene_props = scene.get("props")
    metrics = video_prompt_delta_metrics(
        shot_scene_text(scene),
        action,
        reference_texts=references,
        registered_names=registered_names,
        character_names=[str(name) for name in scene_characters] if isinstance(scene_characters, list) else (),
        prop_names=[str(name) for name in scene_props] if isinstance(scene_props, list) else (),
    )
    prompt_score = round(metrics.score * 25 / 100) if action else 0
    if not action:
        defects.append({"id": "video_prompt_missing", "severity": "high", "message": "还没有视频提示词"})
    elif metrics.problem:
        defects.append({"id": "video_prompt_problem", "severity": "high", "message": metrics.problem})
    defects.extend(
        {"id": "video_prompt_mention", "severity": "high", "message": problem} for problem in metrics.mention_problems
    )
    defects.extend(
        {"id": "video_prompt_mention_hint", "severity": "low", "message": mention_hint}
        for mention_hint in metrics.mention_warnings
    )

    continuity_score = 20
    if index > 0:
        previous_text = shot_scene_text(items[index - 1])
        if problem := handoff_problem(previous_text, shot_scene_text(scene), character_names=characters):
            continuity_score -= 8
            defects.append({"id": "handoff", "severity": "medium", "message": problem})
        if problem := seam_prop_problem(previous_text, shot_scene_text(scene), registered_props=props):
            continuity_score -= 8
            defects.append({"id": "prop_seam", "severity": "high", "message": problem})
    continuity_score = max(0, continuity_score)

    current_grid_id = (assets.get("shot_grid") or {}).get("grid_id") if isinstance(assets, dict) else None
    basis = assets.get("video_prompt_basis") if isinstance(assets, dict) else None
    basis_grid_id = basis.get("grid_id") if isinstance(basis, dict) else None
    version_score = 0 if not video_path or not video_path.is_file() else 10
    if video_path is not None and video_path.is_file() and (not current_grid_id or basis_grid_id != current_grid_id):
        version_score = 0
        defects.append({"id": "video_stale", "severity": "high", "message": "视频或提示词已落后于当前宫格"})

    measured = technical_score + structure_score + prompt_score + continuity_score + version_score
    score = round(measured * 100 / 90)
    has_high = any(defect["severity"] in {"blocker", "high"} for defect in defects)
    verdict = "fail" if has_high or score < 60 else "warn" if score < 85 else "pass"
    dimensions = [
        {"id": "technical", "score": technical_score, "max": 20, "findings": []},
        {"id": "structure", "score": structure_score, "max": 15, "findings": []},
        {"id": "prompt", "score": prompt_score, "max": 25, "findings": []},
        {"id": "continuity", "score": continuity_score, "max": 20, "findings": []},
        {"id": "version", "score": version_score, "max": 10, "findings": []},
    ]
    if score < 85:
        advice.append("优先修复高严重度缺陷，再看黑帧/冻结与跨镜连续性。")
    return {
        "status": "ok" if video_path is not None and video_path.is_file() else "video_missing",
        "shot_id": shot_id,
        "score": score,
        "max": 100,
        "coverage": 1.0,
        "verdict": verdict,
        "dimensions": dimensions,
        "defects": defects,
        "advice": advice,
    }
