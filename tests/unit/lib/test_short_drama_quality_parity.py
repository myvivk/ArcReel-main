"""工厂脚本与服务端质量闸的同源判定：同一批 fixture 的 E12–E14 必须逐条相同。"""

from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path

import pytest

from lib.short_drama_quality import check_episode

ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / "agent_runtime_profile/.claude/skills/short-drama-factory"
CLI_PATH = SKILL / "scripts/validate_episode.py"

_BASE = """\
第【1】集：一致性
【本集时长】：10 秒
[场景]：仓库｜水泥地，单侧顶光
[人物]：甲（杀手｜短发，旧夹克，右眉有疤）
[道具]：铁门｜锈迹斑斑，门轴下垂
△ 铁门第0下被敲响。
△ 铁门第1下被敲响。
△ 铁门第2下被敲响。
△ 铁门第3下被敲响。
△ 铁门第4下被敲响。
【本集断章卡点】：门外传来巨响。（黑屏）
"""

_BIBLE = "【角色】：甲\n【外形】：短发，旧夹克，右眉有疤\n"

FIXTURES: dict[str, tuple[str, str | None]] = {
    "valid": (_BASE, None),
    "too_few_beats": (_BASE.replace("△ 铁门第0下被敲响。\n", ""), None),
    "prop_without_separator": (_BASE.replace("｜锈迹斑斑，门轴下垂", ""), None),
    "prop_appearance_empty": (_BASE.replace("锈迹斑斑，门轴下垂", ""), None),
    "duplicate_prop": (_BASE.replace("[道具]：铁门", "[道具]：铁门｜掉漆\n[道具]：铁门"), None),
    "prop_not_in_body": (_BASE.replace("｜锈迹斑斑，门轴下垂", "｜锈迹斑斑").replace(
        "[道具]：铁门｜锈迹斑斑", "[道具]：玉佩｜羊脂白玉"
    ), None),
    "character_without_appearance": (_BASE.replace("（杀手｜短发，旧夹克，右眉有疤）", "（杀手）"), None),
    "bible_match": (_BASE, _BIBLE),
    "bible_rewritten": (_BASE.replace("右眉有疤", "左眉有疤"), _BIBLE),
    "bible_missing_character": (_BASE, "【角色】：乙\n【外形】：圆脸，光头\n"),
}


def _load_cli():
    spec = importlib.util.spec_from_file_location("short_drama_validate_episode", CLI_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_cli_and_lib_report_identical_gate_results(name: str) -> None:
    markdown, bible = FIXTURES[name]
    fails, warns, _ = _load_cli().validate(markdown, (), "auto", bible)
    cli_lines = [line for line in fails + warns if line.startswith(("E12", "E13", "E14"))]
    lib_lines = [issue.render() for issue in check_episode(markdown, bible=bible)]
    assert Counter(cli_lines) == Counter(lib_lines), f"{name}: CLI={cli_lines} lib={lib_lines}"


@pytest.mark.parametrize(
    "example", ["ep01-longwang-demo.md", "long-format-3min-demo.md"]
)
def test_factory_examples_pass_both_gates(example: str) -> None:
    markdown = (SKILL / "examples" / example).read_text(encoding="utf-8")
    fails, warns, _ = _load_cli().validate(markdown)
    assert fails == [], f"{example}: {fails}"
    assert [issue for issue in check_episode(markdown) if issue.level == "fail"] == []
    assert warns == [], f"{example}: {warns}"
