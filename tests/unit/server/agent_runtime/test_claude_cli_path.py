"""resolve_claude_cli_path：Windows 上把 npm 的 claude.cmd 包装脚本换成原生可执行文件。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from server.agent_runtime.claude_cli_path import resolve_claude_cli_path


def _which(mapping: Mapping[str, str]) -> Callable[[str], str | None]:
    return lambda name: mapping.get(name)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def _resolve(
    *,
    which: Mapping[str, str],
    home: Path,
    system: str = "win32",
    bundled: Path | None = None,
) -> str | None:
    return resolve_claude_cli_path(
        system=system,
        which=_which(which),
        home=home,
        sdk_bundled_cli=lambda: bundled,
    )


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_non_windows_does_not_touch_cli_discovery(system: str, tmp_path: Path) -> None:
    """非 Windows（含 Ubuntu 部署）不探测、不改写路径：SDK 原有发现流程原样保留。"""

    def which(name: str) -> str | None:
        raise AssertionError(f"{system} 上不应查询 {name}")

    assert resolve_claude_cli_path(system=system, which=which, home=tmp_path) is None


def test_windows_native_cli_on_path_leaves_discovery_to_sdk(tmp_path: Path) -> None:
    """PATH 上是原生可执行文件时 SDK 不会拒绝，无须改写。"""
    exe = _touch(tmp_path / "claude.exe")
    assert _resolve(which={"claude": str(exe)}, home=tmp_path / "home") is None


def test_windows_without_cli_leaves_discovery_to_sdk(tmp_path: Path) -> None:
    """没有 CLI 时保持 SDK 的 not-found 报错，不在这里另造路径。"""
    assert _resolve(which={}, home=tmp_path / "home") is None


def test_windows_defers_to_sdk_bundled_cli(tmp_path: Path) -> None:
    """随包 claude.exe 存在时 SDK 的首选不是包装脚本，保留它的选择。"""
    shim = _touch(tmp_path / "prefix" / "claude.cmd")
    _touch(tmp_path / "prefix" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe")
    bundled = _touch(tmp_path / "sdk" / "_bundled" / "claude.exe")
    assert _resolve(which={"claude": str(shim)}, home=tmp_path / "home", bundled=bundled) is None


def test_windows_prefers_native_exe_shadowed_by_batch_shim(tmp_path: Path) -> None:
    """PATH 目录顺序让包装脚本先命中时，改用同样在 PATH 上的 claude.exe。"""
    shim = _touch(tmp_path / "shim" / "claude.cmd")
    exe = _touch(tmp_path / "bin" / "claude.exe")
    assert _resolve(which={"claude": str(shim), "claude.exe": str(exe)}, home=tmp_path / "home") == str(exe)


def test_windows_uses_native_installer_exe(tmp_path: Path) -> None:
    shim = _touch(tmp_path / "claude.cmd")
    native = _touch(tmp_path / ".local" / "bin" / "claude.exe")
    assert _resolve(which={"claude": str(shim)}, home=tmp_path) == str(native)


def test_windows_prefers_exe_next_to_shim(tmp_path: Path) -> None:
    shim = _touch(tmp_path / "prefix" / "claude.cmd")
    sibling = _touch(tmp_path / "prefix" / "claude.exe")
    _touch(tmp_path / "prefix" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe")
    assert _resolve(which={"claude": str(shim)}, home=tmp_path / "home") == str(sibling)


def test_windows_maps_npm_shim_to_packaged_exe(tmp_path: Path) -> None:
    """npm 全局安装：<prefix>\\claude.cmd 转发到同前缀 node_modules 内的 claude.exe。"""
    shim = _touch(tmp_path / "prefix" / "claude.cmd")
    packaged = _touch(tmp_path / "prefix" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe")
    assert _resolve(which={"claude": str(shim)}, home=tmp_path / "home") == str(packaged)


def test_windows_with_only_batch_shim_returns_none(tmp_path: Path) -> None:
    """找不到原生可执行文件时返回 None，由 SDK 自己的发现与报错兜底。"""
    shim = _touch(tmp_path / "claude.cmd")
    assert _resolve(which={"claude": str(shim)}, home=tmp_path / "home") is None
