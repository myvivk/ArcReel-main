"""集管理：新建、调序与删除一集（ADR 0096）。

- **新建**（:func:`create_episode`）：分配新集 ID，插在指定的一集之后，缺省放在播出顺序末尾。标题留空时界面按播出位置
  显示「第 N 集」；带原文时是自带原文的集，否则是无原文的集。
- **调序**（:func:`move_episode`）：自带原文与无原文的集可以放到任意位置，包括两个切出集之间；落进整本源文的切出集
  之间恒按源文位置排列，违背这一顺序的移动被拒。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lib.episode.episode_ids import allocate_episode_ids
from lib.episode.episode_ledger import parse_positive_episode_num
from lib.episode.episode_paths import episode_script_relpath
from lib.episode.episode_sources import SOURCE_ORIGIN_FIELD, SourceOrigin, cut_episode_placements, discover_sources

if TYPE_CHECKING:
    from lib.project.project_manager import ProjectManager


class EpisodeManagementError(ValueError):
    """集管理被拒；``code`` 是稳定的原因码，入口据此映射状态码与文案。账本不被改动。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _ledger(project: dict[str, Any]) -> list[dict[str, Any]]:
    raw = project.get("episodes")
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(entry, dict) for entry in raw):
        raise EpisodeManagementError("ledger_invalid", "分集账本的形状异常")
    return raw


def _index_of(entries: list[dict[str, Any]], episode: int) -> int:
    index = next(
        (i for i, entry in enumerate(entries) if parse_positive_episode_num(entry.get("episode")) == episode), None
    )
    if index is None:
        raise EpisodeManagementError("episode_not_found", f"集（id={episode}）不在账本中")
    return index


def create_episode(
    pm: ProjectManager,
    project_name: str,
    *,
    after: int | None = None,
    title: str = "",
    hook: str = "",
) -> int:
    """创建尚未交稿的工厂集账本，正文仅由工厂工件入库提供。"""
    with pm.locked_source_registration(project_name) as (_source_dir, project, _undo):
        entries = _ledger(project)
        insert_at = len(entries) if after is None else _index_of(entries, after) + 1
        (episode,) = allocate_episode_ids(project, 1)
        origin = SourceOrigin.NONE
        entry: dict[str, Any] = {
            "episode": episode,
            "title": title.strip(),
            "script_file": episode_script_relpath(episode),
            SOURCE_ORIGIN_FIELD: origin.value,
        }
        if hook.strip():
            entry["hook"] = hook.strip()
        project["episodes"] = [*entries[:insert_at], entry, *entries[insert_at:]]
    return episode


def move_episode(pm: ProjectManager, project_name: str, episode: int, *, after: int | None) -> None:
    """把这一集移到 ``after`` 之后，``after`` 为 None 时移到最前。落位的切出集之间须保持源文顺序。"""
    project_dir = pm.get_project_path(project_name)

    def _mutate(project: dict[str, Any]) -> None:
        entries = list(_ledger(project))
        if after == episode:
            # 移到自己之后即原地不动
            _index_of(entries, episode)
            return
        moving = entries.pop(_index_of(entries, episode))
        insert_at = 0 if after is None else _index_of(entries, after) + 1
        entries.insert(insert_at, moving)
        placements = cut_episode_placements(project, discover_sources(project_dir, project))
        positions = [
            placements[num].position
            for entry in entries
            if (num := parse_positive_episode_num(entry.get("episode"))) is not None and num in placements
        ]
        if positions != sorted(positions):
            raise EpisodeManagementError("cut_order_locked", "切出集之间按源文位置排列，不能调换它们的先后")
        project["episodes"] = entries

    pm.update_project(project_name, _mutate)


__all__ = ["EpisodeManagementError", "create_episode", "move_episode"]
