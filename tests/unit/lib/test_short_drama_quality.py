"""短剧单集机检 E12–E14：拍数、道具行、人物外观。"""

from __future__ import annotations

import pytest

from lib.short_drama_quality import QualityIssue, check_episode, check_series, report_passed
from tests.short_drama_support import factory_episode

pytestmark = pytest.mark.usefixtures("factory_profile")


def _check_markers(markdown: str, *, bible: str | None = None) -> list[QualityIssue]:
    return [issue for issue in check_episode(markdown, bible=bible) if issue.code in {"E12", "E13", "E14"}]


def _episode(duration: int, beats: int, *, characters: str, props: str = "", body: str = "") -> str:
    lines = [
        "第【1】集：测试",
        f"【本集时长】：{duration} 秒",
        "[场景]：仓库｜水泥地，单侧顶光",
        f"[人物]：{characters}",
    ]
    if props:
        lines.append(f"[道具]：{props}")
    lines.extend(f"△ 铁门第{index}下被敲响。" for index in range(beats))
    if body:
        lines.append(body)
    lines.append("【本集断章卡点】：门外传来巨响。（黑屏）")
    return "\n".join(lines) + "\n"


def _codes(issues: list[QualityIssue]) -> list[str]:
    return [issue.code for issue in issues]


def test_valid_episode_has_no_issues() -> None:
    markdown = _episode(
        10,
        5,
        characters="甲（杀手｜短发，旧夹克，右眉有疤）",
        props="铁门｜锈迹斑斑，门轴下垂",
    )
    assert _check_markers(markdown) == []


def test_density_reports_shortfall_in_beats() -> None:
    markdown = _episode(10, 3, characters="甲（杀手｜短发，旧夹克，右眉有疤）")
    issues = _check_markers(markdown)
    assert [issue.code for issue in issues] == ["E12"]
    assert issues[0].level == "fail"
    assert "缺 2 拍" in issues[0].message


def test_declared_duration_defaults_to_profile_when_missing() -> None:
    markdown = _episode(10, 5, characters="甲（杀手｜短发，旧夹克，右眉有疤）").replace("【本集时长】：10 秒\n", "")
    # 未声明时长时按标准档 105 秒兜底，5 拍远不够。
    assert "E12" in _codes(_check_markers(markdown))


def test_prop_line_requires_appearance() -> None:
    issues = _check_markers(_episode(10, 5, characters="甲（杀手｜短发，旧夹克，右眉有疤）", props="铁门"))
    assert [issue.code for issue in issues] == ["E13"]
    assert "铁门" in issues[0].message


def test_duplicate_prop_fails() -> None:
    markdown = _episode(10, 5, characters="甲（杀手｜短发，旧夹克，右眉有疤）", props="铁门｜锈迹斑斑")
    markdown = markdown.replace("[道具]：铁门｜锈迹斑斑", "[道具]：铁门｜锈迹斑斑\n[道具]：铁门｜掉漆")
    assert any("重复登记" in issue.message for issue in _check_markers(markdown))


def test_prop_absent_from_body_is_warning_only() -> None:
    markdown = _episode(10, 5, characters="甲（杀手｜短发，旧夹克，右眉有疤）", props="玉佩｜羊脂白玉，编绳发黑")
    warnings = [issue for issue in _check_markers(markdown) if issue.level == "warn"]
    assert [issue.code for issue in warnings] == ["E13"]
    assert "未在动作或台词中出现" in warnings[0].message


def test_prop_name_clashing_with_scene_is_warning() -> None:
    markdown = _episode(10, 5, characters="甲（杀手｜短发，旧夹克，右眉有疤）", props="仓库｜铁皮顶棚")
    warnings = [issue for issue in _check_markers(markdown) if issue.level == "warn"]
    assert any("重名" in issue.message for issue in warnings)


def test_character_without_appearance_fails() -> None:
    issues = _check_markers(_episode(10, 5, characters="甲（杀手）"))
    assert [issue.code for issue in issues] == ["E14"]
    assert "甲" in issues[0].message


def test_character_appearance_shorter_than_eight_chars_fails() -> None:
    issues = _check_markers(_episode(10, 5, characters="甲（杀手｜短发旧夹克）"))
    assert [issue.code for issue in issues] == ["E14"]


def test_appearance_must_be_copied_from_bible_verbatim() -> None:
    bible = "【角色】：甲\n【外形】：短发，旧夹克，右眉有疤\n"
    markdown = _episode(10, 5, characters="甲（杀手｜短发，旧夹克，右眉有疤）")
    assert _check_markers(markdown, bible=bible) == []

    rewritten = _episode(10, 5, characters="甲（杀手｜短发，旧夹克，左眉有疤）")
    issues = _check_markers(rewritten, bible=bible)
    assert [issue.code for issue in issues] == ["E14"]
    assert "被改写或漏抄" in issues[0].message


def test_character_missing_from_bible_fails() -> None:
    bible = "【角色】：乙\n【外形】：圆脸，光头\n"
    issues = _check_markers(_episode(10, 5, characters="甲（杀手｜短发，旧夹克，右眉有疤）"), bible=bible)
    assert [issue.code for issue in issues] == ["E14"]
    assert "抄无可抄" in issues[0].message


def test_render_matches_cli_line_format() -> None:
    assert QualityIssue("E12", "fail", "拍数不足").render() == "E12 拍数不足"
    assert QualityIssue("E13", "fail", "道具行缺少外观", 7).render() == "E13 第7行 道具行缺少外观"


@pytest.mark.parametrize(
    ("before", "after", "code"),
    [
        ("【本集断章卡点】", "【末尾】", "E6"),
        ("△ 巴掌扇来，被反手攥住。", "△ 起床，拉窗帘。", "E4"),
        ("△ 巴掌扇来，被反手攥住。", "△ 巴掌扇来，断肢落地。", "E10"),
    ],
)
def test_full_episode_rules_are_enforced(before: str, after: str, code: str) -> None:
    issues = check_episode(factory_episode().replace(before, after))
    assert any(issue.code == code and issue.level == "fail" for issue in issues)


def test_long_format_requires_mid_hook_and_full_length() -> None:
    failures = {issue.code for issue in check_episode(factory_episode(), profile="long") if issue.level == "fail"}
    assert {"E1", "E11"} <= failures


def test_series_checks_dead_character_dialogue_and_missing_tables() -> None:
    ledger = "# 台账\n当前集: 2\n## 人物账\n|角色|首出|状态|关系|变动集|\n|---|---|---|---|---|\n|林辰|1|死亡|主角|1|\n"
    failures = {issue.code for issue in check_series(ledger, scripts={2: factory_episode()}) if issue.level == "fail"}
    assert {"S3", "S7"} <= failures


@pytest.mark.parametrize(
    ("report", "passed"),
    [
        ("# 合规报告\n结果: PASSED\n", True),
        ("**结论：通过**\n", True),
        ("红线修复后可以通过", False),
        ("结果: FAILED\n", False),
        ("结果: PASSED\n结论: 不通过\n", False),
    ],
)
def test_report_requires_explicit_passing_conclusion(report: str, passed: bool) -> None:
    assert report_passed(report) is passed
