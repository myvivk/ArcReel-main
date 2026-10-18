"""把已落盘的短剧工厂工件补登记为各集 OWN 源文本（无需重新提供工件内容）。

场景：项目已通过 ``ingest_short_drama_artifacts`` 把七类 Markdown 写入 ``source/short_drama/``，
但当时没有登记每集源文本（旧行为），导致分集页「本集正文」为空、下游只能空起稿。
本脚本直接读取 ``source/short_drama/episodes/episode_NN.md`` 并登记，使本集正文可见、
下游建立在工厂正文上。已登记的集会幂等跳过（``set_episode_source_text`` 覆盖写入）。

用法：
    uv run python scripts/backfill_factory_sources.py --root <projects容器> --project <名>
    uv run python scripts/backfill_factory_sources.py --root <projects容器>      # 全部 drama 项目
    uv run python scripts/backfill_factory_sources.py --root <projects容器> --dry-run   # 只预览

``--root`` 是 projects 容器目录（其下每个子目录是一个项目，含 ``project.json``）。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from lib.project.project_manager import ProjectManager
from lib.short_drama_ingest import register_factory_episode_sources


def _project_names(root: Path, only: str | None) -> list[str]:
    if only:
        return [only]
    return [p.name for p in root.iterdir() if (p / "project.json").exists()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--root",
        default="projects/projects",
        help="projects 容器目录（其下每个子目录是一个含 project.json 的项目）；默认 projects/projects",
    )
    parser.add_argument("--project", default=None, help="仅处理指定项目；缺省处理全部 drama 项目")
    parser.add_argument("--dry-run", action="store_true", help="只打印将会登记的集，不写入")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.exists():
        parser.error(f"projects 容器不存在：{root}")

    pm = ProjectManager(root)
    for name in _project_names(root, args.project):
        try:
            project = pm.load_project(name)
        except FileNotFoundError:
            print(f"[skip] {name}: 项目不存在")
            continue
        if project.get("content_mode") != "drama":
            continue
        sd_episodes = pm.get_project_path(name) / "source" / "short_drama" / "episodes"
        if not sd_episodes.exists():
            print(f"[skip] {name}: 无 source/short_drama/episodes")
            continue
        artifacts = [(f"episodes/{f.name}", f.read_bytes()) for f in sorted(sd_episodes.glob("episode_*.md"))]
        if not artifacts:
            print(f"[skip] {name}: 无 episode_*.md 工件")
            continue
        if args.dry_run:
            print(f"[dry-run] {name}: 将登记 {len(artifacts)} 集 -> {[a[0] for a in artifacts]}")
            continue
        register_factory_episode_sources(pm, name, artifacts)
        print(f"[ok] {name}: 登记 {len(artifacts)} 集为 OWN 源文本")


if __name__ == "__main__":
    main()
