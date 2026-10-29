"""给一集视频提示词打确定性增量分：镜头语言、静帧密度、与分镜/K 帧的复述。

只读项目文件，不调用文本或视频模型，不写项目；输出 Markdown 表与可选 JSON 快照，
用于阈值标定和重生成前后对比。

用法：
    uv run python scripts/score_video_prompts.py \
        --project projects/projects/proj-a25e3280 --episode 1 \
        --json .tmp/video-prompt-score-before.json

    # 重生成后与旧快照对比
    uv run python scripts/score_video_prompts.py \
        --project projects/projects/proj-a25e3280 --episode 1 \
        --baseline .tmp/video-prompt-score-before.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lib.script.grid.keyframe_parser import shot_scene_text
from lib.script.grid.shot_grid_manager import ShotGridManager, select_shot_grids
from lib.script.grid.video_prompt_targets import video_prompt_text
from lib.script.script_skeleton import resolve_declared_kind, resolve_kind_items
from lib.script.shot_prompt_standard import video_prompt_delta_metrics


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _episode_script(project_dir: Path, project: Mapping[str, Any], episode: int) -> Path:
    for entry in project.get("episodes") or []:
        if isinstance(entry, Mapping) and int(entry.get("episode", -1)) == episode:
            script_file = entry.get("script_file")
            if isinstance(script_file, str) and script_file.strip():
                return project_dir / script_file
            break
    return project_dir / "scripts" / f"episode_{episode}.json"


def _score_episode(project_dir: Path, episode: int) -> dict[str, Any]:
    project = _read_json(project_dir / "project.json")
    registered_names = {
        str(name) for bucket in ("characters", "scenes", "props") for name in (project.get(bucket) or {})
    }
    script_path = _episode_script(project_dir, project, episode)
    script = _read_json(script_path)
    kind = resolve_declared_kind(project.get("content_mode"), project.get("generation_mode"))
    raw_items, id_field, _ = resolve_kind_items(script, kind=kind)
    items = [item for item in raw_items if isinstance(item, Mapping)] if isinstance(raw_items, list) else []
    grids = select_shot_grids(
        (grid for grid in ShotGridManager(project_dir).list_all() if grid.status == "confirmed"),
        items=items,
        id_field=id_field,
    )
    rows: list[dict[str, Any]] = []
    for item in items:
        unit_id = str(item.get(id_field, ""))
        action = video_prompt_text(item)
        grid = grids.get(unit_id)
        reference_texts = tuple(cell.prompt for cell in grid.cells) if grid is not None else ()
        scene_characters = item.get("characters_in_scene")
        scene_props = item.get("props")
        if not action.strip():
            rows.append(
                {
                    "scene_id": unit_id,
                    "duration_seconds": item.get("duration_seconds"),
                    "action": "",
                    "action_chars": 0,
                    "sentence_count": 0,
                    "max_similarity": 0.0,
                    "max_verbatim_run": 0,
                    "static_density": 0.0,
                    "static_terms": [],
                    "camera_terms": [],
                    "warnings": [],
                    "score": None,
                    "passed": None,
                    "problem": None,
                    "status": "missing",
                }
            )
            continue
        metrics = video_prompt_delta_metrics(
            shot_scene_text(item),
            action,
            reference_texts=reference_texts,
            registered_names=registered_names,
            character_names=[str(name) for name in scene_characters] if isinstance(scene_characters, list) else (),
            prop_names=[str(name) for name in scene_props] if isinstance(scene_props, list) else (),
        )
        rows.append(
            {
                "scene_id": unit_id,
                "duration_seconds": item.get("duration_seconds"),
                "action": action,
                "action_chars": metrics.action_chars,
                "sentence_count": metrics.sentence_count,
                "max_similarity": round(metrics.max_similarity, 3),
                "max_verbatim_run": metrics.max_verbatim_run,
                "static_density": round(metrics.static_density, 3),
                "static_terms": list(metrics.static_terms),
                "camera_terms": list(metrics.camera_terms),
                "warnings": list(metrics.warnings),
                "mention_problems": list(metrics.mention_problems),
                "mention_warnings": list(metrics.mention_warnings),
                "score": metrics.score,
                "passed": metrics.passed,
                "problem": metrics.problem,
                "status": "scored",
            }
        )
    return {
        "project": project_dir.name,
        "episode": episode,
        "script": str(script_path.relative_to(project_dir)),
        "scored_at": datetime.now(UTC).isoformat(),
        "scenes": rows,
    }


def _markdown(report: Mapping[str, Any], baseline: Mapping[str, Any] | None) -> str:
    rows = report.get("scenes") or []
    scenes = [row for row in rows if isinstance(row, Mapping)]
    scored = [row for row in scenes if row.get("status") != "missing"]
    passed = sum(1 for row in scored if row.get("passed"))
    missing = len(scenes) - len(scored)
    old_by_id = {
        str(row.get("scene_id")): row for row in (baseline or {}).get("scenes") or [] if isinstance(row, Mapping)
    }
    lines = [
        "# 视频提示词增量评分",
        "",
        f"- 项目：`{report.get('project')}`",
        f"- 集：{report.get('episode')}",
        f"- 脚本：`{report.get('script')}`",
        f"- 打分时间：{report.get('scored_at')}",
        f"- 通过：{passed}/{len(scored)}" + (f"（另有 {missing} 个分镜尚未编写视频提示词）" if missing else ""),
    ]
    if baseline is not None:
        lines.append(f"- 对比基线：`{baseline.get('scored_at')}`")
    lines += [
        "",
        "| 分镜 | 时长 | 字数 | 句数 | 最大相似度 | 最长重合 | 静帧密度 | 疑似 | 分数 | 结论 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in scenes:
        conclusion = "MISSING" if row.get("status") == "missing" else "PASS" if row.get("passed") else "FAIL"
        warnings = row.get("warnings") or []
        lines.append(
            f"| {row.get('scene_id')} | {row.get('duration_seconds') or '-'} | {row.get('action_chars')} "
            f"| {row.get('sentence_count')} | {row.get('max_similarity')} | {row.get('max_verbatim_run')} "
            f"| {float(row.get('static_density') or 0):.0%} | {len(warnings) if warnings else '-'} "
            f"| {row.get('score') or '-'} | {conclusion} |"
        )
    if baseline is not None:
        lines += [
            "",
            "## 与基线对比",
            "",
            "| 分镜 | 旧字数 | 新字数 | 旧分数 | 新分数 | 旧相似度 | 新相似度 | 旧疑似 | 新疑似 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in scenes:
            if row.get("status") == "missing":
                continue
            old = old_by_id.get(str(row.get("scene_id")))
            if old is None or old.get("status") == "missing":
                continue
            lines.append(
                f"| {row.get('scene_id')} | {old.get('action_chars')} | {row.get('action_chars')} "
                f"| {old.get('score')} | {row.get('score')} | {old.get('max_similarity')} "
                f"| {row.get('max_similarity')} | {len(old.get('warnings') or [])} "
                f"| {len(row.get('warnings') or [])} |"
            )
    failures = [row for row in scored if not row.get("passed")]
    if failures:
        lines += ["", "## 未通过原因", ""]
        for row in failures:
            lines.append(f"- `{row.get('scene_id')}`：{row.get('problem')}")
    suspects = [row for row in scored if row.get("warnings")]
    if suspects:
        lines += ["", "## 疑似复述（未拦截）", ""]
        for row in suspects:
            for warning in row.get("warnings") or []:
                lines.append(f"- `{row.get('scene_id')}`：{warning}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=True, help="项目目录（含 project.json）")
    parser.add_argument("--episode", type=int, required=True, help="集号")
    parser.add_argument("--json", dest="json_path", help="把完整快照写到这个路径")
    parser.add_argument("--markdown", dest="markdown_path", help="把 Markdown 报告写到这个路径；缺省打印到 stdout")
    parser.add_argument("--baseline", help="上一轮快照 JSON，用于重生成前后对比")
    args = parser.parse_args()

    project_dir = Path(args.project).resolve()
    if not (project_dir / "project.json").is_file():
        print(f"不是项目目录（缺 project.json）：{project_dir}", file=sys.stderr)
        return 2
    report = _score_episode(project_dir, args.episode)
    baseline = _read_json(Path(args.baseline)) if args.baseline else None
    if args.json_path:
        json_path = Path(args.json_path)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown = _markdown(report, baseline)
    if args.markdown_path:
        markdown_path = Path(args.markdown_path)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(markdown, encoding="utf-8")
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
