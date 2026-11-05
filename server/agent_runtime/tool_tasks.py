"""Track active embedded MCP tool tasks by assistant session."""

from __future__ import annotations

import asyncio
from collections import defaultdict

_ACTIVE: dict[str, set[asyncio.Task[object]]] = defaultdict(set)


def register(session_id: str | None) -> asyncio.Task[object] | None:
    if not session_id:
        return None
    task = asyncio.current_task()
    if task is None:
        return None
    _ACTIVE[session_id].add(task)
    return task


def unregister(session_id: str | None, task: asyncio.Task[object] | None) -> None:
    if not session_id or task is None:
        return
    tasks = _ACTIVE.get(session_id)
    if tasks is None:
        return
    tasks.discard(task)
    if not tasks:
        _ACTIVE.pop(session_id, None)


def cancel_session(session_id: str) -> int:
    tasks = tuple(_ACTIVE.get(session_id, ()))
    for task in tasks:
        task.cancel("assistant session interrupted")
    return len(tasks)
