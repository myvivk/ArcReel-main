"""故事规划文档的统一读取：人物圣经 / 全剧大纲 / 连续性台账 / 分集规划。

「故事规划」是剧本之外的第二权威来源：补足剧本没展开的角色背景、世界观与跨集一致性。
四类文档的落盘位置与读取口径**只在这里定义**——本集资产抽取（登记完整性）与资产描述
AI 生成（补漏跨集背景）共用这一份，避免各处各读一份。

- 人物圣经（``character-bible``）：``【角色】：名字`` 起的整段，按角色取段。
- 全剧大纲（``series-outline``）：全局设定与场景名行，命中资产名的行。
- 连续性台账（``continuity-ledger``）：跨集一致性约束，命中资产名的行。
- 分集规划（``episode-plan``）：本集叙事规划与资产预期，命中资产名的行；文档按 ``第 N 集``
  分节时只取本集那一节。

四类文档与逐集源稿（``source/short_drama/episodes/episode_NN.md``）共用 ``[人物]/[场景]/
[道具]`` 标记行写法，故名字抽取复用 :func:`lib.script.short_drama_import.parse_short_drama_episode`
的解析结果，不另写一套标记行语法。
"""

from __future__ import annotations

import re
from pathlib import Path

from lib.script.short_drama_import import parse_short_drama_episode

#: 四类规划文档的候选文件名（小写匹配）；同一类命中多份时按路径排序全部读取。
STORY_PLAN_FILES: dict[str, tuple[str, ...]] = {
    "character_bible": ("character-bible.md", "character_bible.md", "人物圣经.md"),
    "series_outline": ("series-outline.md", "series_outline.md", "outline.md", "全剧大纲.md"),
    "continuity_ledger": (
        "continuity-ledger.md",
        "continuity_ledger.md",
        "continuity.md",
        "连续性台账.md",
    ),
    "episode_plan": (
        "episode-plan.md",
        "episode_plan.md",
        "episode-planning.md",
        "episode_planning.md",
        "分集规划.md",
    ),
}

STORY_PLAN_LABELS: dict[str, str] = {
    "character_bible": "人物圣经",
    "series_outline": "全剧大纲",
    "continuity_ledger": "连续性台账",
    "episode_plan": "分集规划",
}

EPISODE_ASSET_TYPES: tuple[str, ...] = ("character", "scene", "prop")

_MARKER_LIMIT = 8
_HIT_LINE_LIMIT = 6

_CHARACTER_ROLE_RE = re.compile(r"^\s*【角色】[：:]\s*(.+)$")
_EPISODE_HEADER_RE = re.compile(r"^\s*(?:#{1,6}\s*)?第\s*([0-9]+)\s*集")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def story_plan_documents(project_dir: Path) -> dict[str, list[Path]]:
    """项目 ``source/`` 下四类规划文档的路径，按类分组；没有该类文档时该键缺席。"""

    root = project_dir / "source"
    found: dict[str, list[Path]] = {}
    if not root.is_dir():
        return found
    for path in sorted(root.rglob("*.md")):
        lowered = path.name.lower()
        for kind, names in STORY_PLAN_FILES.items():
            if lowered in names:
                found.setdefault(kind, []).append(path)
                break
    return found


def plan_episode_section(text: str, episode: int | str | None) -> str:
    """按 ``第 N 集`` 标题截取本集那一段；文档不分集（没有集标题）时返回整篇。

    给了集号却找不到该集的小节时返回空串——别的集的规划不该被算成本集的资产。
    """

    if episode is None:
        return text
    headers = [line for line in text.splitlines() if _EPISODE_HEADER_RE.match(line)]
    if not headers:
        return text
    try:
        wanted = int(str(episode))
    except (TypeError, ValueError):
        return text
    collected: list[str] = []
    matching = False
    for line in text.splitlines():
        match = _EPISODE_HEADER_RE.match(line)
        if match:
            if matching:
                break
            matching = int(match.group(1)) == wanted
            continue
        if matching:
            collected.append(line)
    return "\n".join(collected).strip()


def story_plan_sections(project_dir: Path, kind: str, *, episode: int | str | None = None) -> list[tuple[Path, str]]:
    """某类规划文档的 ``(路径, 本集相关正文)``；分集规划按集号收窄。"""

    sections: list[tuple[Path, str]] = []
    for path in story_plan_documents(project_dir).get(kind, []):
        text = _read_text(path)
        body = plan_episode_section(text, episode) if kind == "episode_plan" else text
        if body.strip():
            sections.append((path, body))
    return sections


def _bible_section(bible: str, name: str) -> str:
    """人物圣经里 ``【角色】：名字`` 起到下一个角色前的整段。"""

    lines = bible.splitlines()
    collected: list[str] = []
    start: int | None = None
    for index, line in enumerate(lines):
        match = _CHARACTER_ROLE_RE.match(line)
        if not match:
            continue
        if start is not None:
            collected.append("\n".join(lines[start:index]).strip())
        raw_name = re.split(r"[（(]", match.group(1), maxsplit=1)[0].strip()
        start = index if raw_name == name else None
    if start is not None:
        collected.append("\n".join(lines[start:]).strip())
    return collected[0] if collected else ""


def _hits(text: str, name: str, limit: int = _HIT_LINE_LIMIT) -> list[str]:
    return [line.strip() for line in text.splitlines() if name in line][:limit]


def collect_story_plan(
    project_dir: Path,
    asset_type: str,
    name: str,
    *,
    episode: int | str | None = None,
) -> list[str]:
    """某资产在故事规划里的记载，按「人物圣经 → 全剧大纲 → 连续性台账 → 分集规划」给出。

    供资产描述 AI 生成作为**第二优先级块**（剧本（工厂源稿）之后、题材补全之前）：补标记行
    没写到的跨集背景，不覆盖剧本里已有的外观事实。
    """

    items: list[str] = []
    if asset_type == "character":
        for kind in ("character_bible", "episode_plan"):
            for path, body in story_plan_sections(project_dir, kind, episode=episode):
                section = _bible_section(body, name)
                if section:
                    items.append(f"【{STORY_PLAN_LABELS[kind]}｜{path.name}】\n{section}")
    for kind in ("series_outline", "continuity_ledger", "episode_plan"):
        for path, body in story_plan_sections(project_dir, kind, episode=episode):
            hits = _hits(body, name)
            if hits:
                items.append(f"【{STORY_PLAN_LABELS[kind]}｜{path.name}】\n" + "\n".join(hits))
    # 重复出现的同一份记载（同一文档既作圣经又作规划）只保留一条。
    deduped: list[str] = []
    for item in items:
        if item not in deduped:
            deduped.append(item)
    return deduped


def marker_asset_names(text: str) -> dict[str, list[str]]:
    """从 ``[人物]/[场景]/[道具]`` 标记行里读出各类型的名字（去重保序）。

    逐集源稿与故事规划共用这套标记行写法，两边都取这一份解析结果。
    """

    parsed = parse_short_drama_episode(text)
    collected: dict[str, list[str]] = {asset_type: [] for asset_type in EPISODE_ASSET_TYPES}

    def _add(asset_type: str, name: str) -> None:
        key = (name or "").strip()
        if key and key not in collected[asset_type]:
            collected[asset_type].append(key)

    for block in parsed.blocks:
        for name in block.characters:
            _add("character", name)
        _add("scene", block.location)
        for name in block.props:
            _add("prop", name)
    return collected


def story_plan_asset_names(project_dir: Path, *, episode: int | str | None = None) -> dict[str, list[str]]:
    """故事规划里定义 / 提及的资产名，按类型去重保序。

    人物圣经的 ``【角色】`` 清单直接给出角色名；四类文档里的 ``[人物]/[场景]/[道具]`` 标记行
    按标记给类型（与逐集源稿同一语法）。分集规划按集号收窄到本集那一节。
    """

    collected: dict[str, list[str]] = {asset_type: [] for asset_type in EPISODE_ASSET_TYPES}

    def _add(asset_type: str, name: str) -> None:
        key = (name or "").strip()
        if key and key not in collected[asset_type]:
            collected[asset_type].append(key)

    for _path, body in story_plan_sections(project_dir, "character_bible", episode=episode):
        for line in body.splitlines():
            match = _CHARACTER_ROLE_RE.match(line)
            if not match:
                continue
            for name in _split_names(match.group(1)):
                _add("character", name)
    for kind in ("series_outline", "continuity_ledger", "episode_plan"):
        for _path, body in story_plan_sections(project_dir, kind, episode=episode):
            for asset_type, names in marker_asset_names(body).items():
                for name in names:
                    _add(asset_type, name)
    return collected


def _split_names(line: str) -> list[str]:
    """``林辰（隐忍龙王｜…）、陈峰`` 这类清单里的名字；括号内的顿号不算分隔符。"""

    names: list[str] = []
    depth = 0
    current = ""
    for ch in line:
        if ch in "（(":
            depth += 1
            current += ch
        elif ch in "）)":
            depth -= 1
            current += ch
        elif ch in "、,，" and depth == 0:
            if current.strip():
                names.append(_before_paren(current))
            current = ""
        else:
            current += ch
    if current.strip():
        names.append(_before_paren(current))
    return [name for name in names if name]


def _before_paren(token: str) -> str:
    return re.split(r"[（(]", token.strip(), maxsplit=1)[0].strip()


__all__ = [
    "EPISODE_ASSET_TYPES",
    "STORY_PLAN_FILES",
    "STORY_PLAN_LABELS",
    "collect_story_plan",
    "marker_asset_names",
    "plan_episode_section",
    "story_plan_asset_names",
    "story_plan_documents",
    "story_plan_sections",
]
