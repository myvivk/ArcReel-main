"""Windows 上把 npm 的 claude.cmd 包装脚本换成原生 claude 可执行文件。

SDK 拒绝在 Windows 上经 cmd.exe 执行 .bat/.cmd 包装脚本（参数注入无法安全转义），
而 ``npm install -g @anthropic-ai/claude-code`` 的 PATH 入口是 ``claude.cmd``。
该包装脚本只是转发到包内的 ``bin/claude.exe``；PATH 上是包装脚本时，这里返回
一个原生可执行文件，交给 SDK 显式启动。

PATH 上的 ``claude`` 本身是原生文件、或根本没有 CLI 时返回 None：这两种情况
SDK 自己的发现流程都能处理（优先随包 CLI，并探测 ``~/.local/bin/claude.exe``）。
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from pathlib import Path

# npm 全局安装把包装脚本放在 <prefix>\\claude.cmd，原生二进制在同级 node_modules 下。
_NPM_CLAUDE_EXE = Path("node_modules") / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"


def _is_batch_script(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rstrip(". ").lower().endswith((".cmd", ".bat"))


def _is_native_executable(path: str) -> bool:
    """按末段文件名判断是否为 CreateProcess 可直接执行的映像（.exe / .com）。"""
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rstrip(". ").lower().endswith((".exe", ".com"))


def _sdk_bundled_cli() -> Path | None:
    """SDK 随包 CLI 的位置；只有带 claude.exe 的 Windows 平台 wheel 才有这个文件。"""
    import claude_agent_sdk

    location = getattr(claude_agent_sdk, "__file__", None)
    if not location:
        return None
    return Path(location).parent / "_bundled" / "claude.exe"


def resolve_claude_cli_path(
    *,
    system: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    home: Path | None = None,
    sdk_bundled_cli: Callable[[], Path | None] | None = None,
) -> str | None:
    """返回可替代 claude.cmd 包装脚本的原生可执行文件路径；无可用项时返回 None。"""
    # 非 Windows（含 Ubuntu 云服务器）：不传 cli_path，保持 SDK 原有发现顺序（随包 CLI 优先）。
    if (system or sys.platform) != "win32":
        return None

    shim = which("claude")
    if shim is None or not _is_batch_script(shim):
        return None

    # 随包 CLI 是 SDK 的首选，存在时 connect() 根本不会走到 PATH 上的包装脚本。
    bundled = (sdk_bundled_cli or _sdk_bundled_cli)()
    if bundled is not None and bundled.is_file():
        return None

    on_path = which("claude.exe")
    if on_path and _is_native_executable(on_path):
        return on_path

    native_install = (home or Path.home()) / ".local" / "bin" / "claude.exe"
    if native_install.is_file():
        return str(native_install)

    shim_path = Path(shim)
    sibling = shim_path.with_name("claude.exe")
    if sibling.is_file():
        return str(sibling)
    packaged = shim_path.parent / _NPM_CLAUDE_EXE
    if packaged.is_file():
        return str(packaged)
    return None
