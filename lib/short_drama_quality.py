"""工厂质量闸，直接复用随运行时配置分发的独立机检脚本。"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from lib.agent.agent_profile import agent_profile_dir


class _EpisodeValidator(Protocol):
    def validate(
        self, text: str, prev_texts: Sequence[str], profile: str, bible: str | None
    ) -> tuple[list[str], list[str], int]: ...


class _SeriesValidator(Protocol):
    def validate_ledger(
        self, text: str, total_eps: int, scripts: dict[int, str]
    ) -> tuple[list[str], list[str], int]: ...


def _load_validator(name: str) -> object:
    # 只执行应用安装的脚本，不能执行项目目录中可由用户修改的副本。
    path = agent_profile_dir() / ".claude/skills/short-drama-factory/scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载工厂机检脚本：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True, slots=True)
class QualityIssue:
    code: str
    level: str
    message: str
    line: int | None = None

    def render(self) -> str:
        if self.line is None:
            return f"{self.code} {self.message}"
        return f"{self.code} 第{self.line}行 {self.message}"


def _issues(failures: list[str], warnings: list[str]) -> list[QualityIssue]:
    result = []
    for level, messages in (("fail", failures), ("warn", warnings)):
        for text in messages:
            code, _, message = text.partition(" ")
            line = re.match(r"第(\d+)行 (.*)", message)
            result.append(
                QualityIssue(code, level, line.group(2), int(line.group(1)))
                if line
                else QualityIssue(code, level, message)
            )
    return result


def check_episode(
    markdown: str, *, bible: str | None = None, profile: str = "auto", previous: Sequence[str] = ()
) -> list[QualityIssue]:
    """执行 E1–E14 全部机检；FAIL 拒绝入库，WARN 保留供复核。"""
    validator = cast(_EpisodeValidator, _load_validator("validate_episode"))
    failures, warnings, _ = validator.validate(markdown, previous, profile, bible)
    return _issues(failures, warnings)


def check_series(ledger: str, *, scripts: dict[int, str], total_episodes: int = 80) -> list[QualityIssue]:
    """按最终台账和已交付 / 本批正文检查跨集连续性。"""
    validator = cast(_SeriesValidator, _load_validator("validate_series"))
    failures, warnings, _ = validator.validate_ledger(ledger, total_episodes, scripts)
    return _issues(failures, warnings)


def report_passed(markdown: str) -> bool:
    """读取报告明确的最终结论；不把问题说明里的「通过」当作结论。"""
    conclusions = re.findall(
        r"^\s*(?:#+\s*)?(?:【(?:最终结论|检查结论|合规结论|结论|结果)】|"
        r"(?:最终结论|检查结论|合规结论|结论|结果|Result|Status))\s*[:：]\s*"
        r"(PASSED|PASS|通过|FAILED|FAIL|不通过|未通过)(?=\s|$|[（(。])",
        markdown.replace("**", ""),
        re.M | re.I,
    )
    return bool(conclusions) and all(value.upper() in {"PASSED", "PASS", "通过"} for value in conclusions)
