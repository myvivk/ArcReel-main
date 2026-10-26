"""开发态事件循环工厂：Windows 上给内嵌 Agent 留出可创建子进程的循环。

uvicorn 在 ``--reload``（子进程模式）下默认选择 SelectorEventLoop；Windows 的 Selector
循环不支持 ``asyncio.create_subprocess_exec``（直接抛 NotImplementedError），内嵌 Agent
启动 Claude Code CLI 时就会失败。这里在 Windows 上改用 ProactorEventLoop，其它平台沿用
uvicorn 原本的选择。

注意 uvicorn 对 ``--loop`` 的自定义导入路径**不会**再传参调用，返回值要直接是事件循环实例。

用法：

    uv run uvicorn server.app:app --reload --reload-dir server --reload-dir lib \
        --port 1241 --loop server.dev_loop:proactor_loop_factory
"""

from __future__ import annotations

import asyncio
import sys


def proactor_loop_factory() -> asyncio.AbstractEventLoop:
    if sys.platform == "win32":
        return asyncio.ProactorEventLoop()
    return asyncio.SelectorEventLoop()
