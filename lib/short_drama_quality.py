"""短剧工厂单集 Markdown 的服务端质量闸。

与 ``agent_runtime_profile/.claude/skills/short-drama-factory/scripts/validate_episode.py``
的 E12–E14 判定保持一致（脚本要能独立 ``python3`` 跑，不 import 本模块；一致性由
``tests/unit/lib/test_short_drama_quality_parity.py`` 守）。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

BEAT_SECONDS = 2.0
CHARACTER_APPEARANCE_MIN = 8

_PROFILE_SECONDS = {"standard": 105, "long": 180, "manju": 75}
_EPISODE_SECONDS_RE = re.compile(r"^【本集时长】[：:]\s*(\d+)\s*秒", re.M)
_CHARACTER_RE = re.compile(r"^\[人物\][：:]\s*(.+)$")
_SCENE_RE = re.compile(r"^\[场景\][：:]\s*(.+)$")
_PROP_RE = re.compile(r"^\[道具\][：:]\s*(.+)$")
_BIBLE_CHARACTER_RE = re.compile(r"^\s*【角色】[：:]\s*(.+)$")


@dataclass(frozen=True, slots=True)
class QualityIssue:
    code: str
    level: str
    message: str
    line: int | None = None

    def render(self) -> str:
        """与 CLI 完全相同的单行输出。"""
        if self.line is None:
            return f"{self.code} {self.message}"
        return f"{self.code} 第{self.line}行 {self.message}"


def _resolve_profile(text: str, profile: str) -> str:
    if profile in _PROFILE_SECONDS:
        return profile
    if re.search(r"长档", text):
        return "long"
    if re.search(r"漫剧", text):
        return "manju"
    return "standard"


def _dialogue(line: str) -> bool:
    value = line.strip()
    if not value or value.startswith(("[", "△", "【", "#", ">")):
        return False
    return (
        re.match(r"^[\u4e00-\u9fa5A-Za-z0-9]{1,8}(?:（[^）]*）|\([^)]*\))?\s*[:：]\s*.+$", value)
        is not None
    )


def _character_entries(value: str) -> list[tuple[str, str]]:
    """按括号深度切角色条目，外观取 ``｜`` 之后的部分（与导入器同口径）。"""
    entries: list[tuple[str, str]] = []
    current: list[str] = []
    depth = 0
    for char in value:
        if char in "（(":
            depth += 1
        elif char in "）)":
            depth = max(0, depth - 1)
        if char in "、,，" and depth == 0:
            token = "".join(current).strip()
            if token:
                entries.append(_parse_character_entry(token))
            current = []
        else:
            current.append(char)
    token = "".join(current).strip()
    if token:
        entries.append(_parse_character_entry(token))
    return entries


def _parse_character_entry(token: str) -> tuple[str, str]:
    name = re.split(r"[（(]", token, maxsplit=1)[0].strip()
    match = re.search(r"[（(]([^）)]*)[）)]", token)
    appearance = ""
    if match:
        inner = match.group(1)
        if "｜" in inner:
            appearance = inner.split("｜", 1)[1].strip()
        elif "|" in inner:
            appearance = inner.split("|", 1)[1].strip()
    return name, appearance


def _split_prop_line(value: str) -> tuple[str, bool, str]:
    name, separator, appearance = value.partition("｜")
    if not separator:
        name, separator, appearance = value.partition("|")
    return name.strip(), bool(separator), appearance.strip()


def _declared_asset_names(markdown: str) -> tuple[set[str], set[str]]:
    characters: set[str] = set()
    scenes: set[str] = set()
    for line in markdown.splitlines():
        stripped = line.strip()
        person_match = _CHARACTER_RE.match(stripped)
        if person_match:
            characters.update(name for name, _ in _character_entries(person_match.group(1)) if name)
        scene_match = _SCENE_RE.match(stripped)
        if scene_match:
            scenes.add(_split_prop_line(scene_match.group(1))[0])
    return characters, scenes


def _bible_section(bible: str, name: str) -> str | None:
    lines = bible.splitlines()
    start: int | None = None
    sections: list[str] = []
    for index, line in enumerate(lines):
        match = _BIBLE_CHARACTER_RE.match(line)
        if match:
            raw_name = re.split(r"[（(]", match.group(1), maxsplit=1)[0].strip()
            if start is not None:
                sections.append("\n".join(lines[start:index]))
            start = index if raw_name == name else None
    if start is not None:
        sections.append("\n".join(lines[start:]))
    return sections[0] if sections else None


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def check_episode(
    markdown: str, *, bible: str | None = None, profile: str = "auto"
) -> list[QualityIssue]:
    """校验 E12 拍数、E13 道具行和 E14 角色外观；WARN 不阻止落盘。"""
    selected_profile = _resolve_profile(markdown, profile)
    seconds_match = _EPISODE_SECONDS_RE.search(markdown)
    seconds = (
        int(seconds_match.group(1)) if seconds_match else _PROFILE_SECONDS[selected_profile]
    )
    required = math.ceil(seconds / BEAT_SECONDS)
    beats = sum(
        line.strip().startswith("△") or _dialogue(line) for line in markdown.splitlines()
    )
    issues: list[QualityIssue] = []
    if beats < required:
        issues.append(
            QualityIssue(
                "E12",
                "fail",
                f"拍数 {beats} < {required}（{seconds} 秒 ÷ {BEAT_SECONDS:g} 秒/拍），"
                f"缺 {required - beats} 拍；一句概括不算拍，拆成连续动作",
            )
        )

    prop_entries: dict[str, int] = {}
    body_lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    character_names, scene_names = _declared_asset_names(markdown)
    for line_number, line in enumerate(markdown.splitlines(), start=1):
        stripped = line.strip()
        prop_match = _PROP_RE.match(stripped)
        if prop_match:
            name, separator, appearance = _split_prop_line(prop_match.group(1))
            if not separator or not name or not appearance:
                issues.append(
                    QualityIssue(
                        "E13",
                        "fail",
                        f"道具行缺少有效名称或「｜」后的外观描述："
                        f"{name or prop_match.group(1).strip()}",
                        line_number,
                    )
                )
                continue
            if name in prop_entries:
                issues.append(
                    QualityIssue("E13", "fail", f"道具「{name}」在本集中重复登记", line_number)
                )
            prop_entries[name] = line_number
            if name in character_names or name in scene_names:
                issues.append(
                    QualityIssue(
                        "E13",
                        "warn",
                        f"道具「{name}」与角色或场景重名，导入时会记录资产冲突",
                        line_number,
                    )
                )
            if not any(
                name in body
                for body in body_lines
                if not body.startswith(("[道具]", "[人物]", "[场景]"))
            ):
                issues.append(
                    QualityIssue(
                        "E13",
                        "warn",
                        f"道具「{name}」已登记但未在动作或台词中出现",
                        line_number,
                    )
                )

        person_match = _CHARACTER_RE.match(stripped)
        if person_match:
            for name, appearance in _character_entries(person_match.group(1)):
                if not name:
                    continue
                if len(appearance) < CHARACTER_APPEARANCE_MIN:
                    issues.append(
                        QualityIssue(
                            "E14",
                            "fail",
                            f"[人物] 行外观缺失或过短：{name}；"
                            "每位出场角色须带「｜」后的完整外形，并从人物圣经原样抄入（30~60 字）",
                            line_number,
                        )
                    )
                elif bible is not None:
                    section = _bible_section(bible, name)
                    if section is None:
                        issues.append(
                            QualityIssue(
                                "E14", "fail", f"人物圣经里没有 {name}，外观抄无可抄", line_number
                            )
                        )
                    elif _compact(appearance) not in _compact(section):
                        issues.append(
                            QualityIssue(
                                "E14",
                                "fail",
                                f"{name} 的外观不是从人物圣经原样抄入（被改写或漏抄）",
                                line_number,
                            )
                        )

    return issues


__all__ = ["BEAT_SECONDS", "CHARACTER_APPEARANCE_MIN", "QualityIssue", "check_episode"]
