"""短剧单集机检 E12–E14：拍数、道具行、人物外观。"""

from __future__ import annotations

from lib.short_drama_quality import QualityIssue, check_episode


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
    assert check_episode(markdown) == []


def test_density_reports_shortfall_in_beats() -> None:
    markdown = _episode(10, 3, characters="甲（杀手｜短发，旧夹克，右眉有疤）")
    issues = check_episode(markdown)
    assert [issue.code for issue in issues] == ["E12"]
    assert issues[0].level == "fail"
    assert "缺 2 拍" in issues[0].message


def test_declared_duration_defaults_to_profile_when_missing() -> None:
    markdown = _episode(10, 5, characters="甲（杀手｜短发，旧夹克，右眉有疤）").replace(
        "【本集时长】：10 秒\n", ""
    )
    # 未声明时长时按标准档 105 秒兜底，5 拍远不够。
    assert "E12" in _codes(check_episode(markdown))


def test_prop_line_requires_appearance() -> None:
    issues = check_episode(_episode(10, 5, characters="甲（杀手｜短发，旧夹克，右眉有疤）", props="铁门"))
    assert [issue.code for issue in issues] == ["E13"]
    assert "铁门" in issues[0].message


def test_duplicate_prop_fails() -> None:
    markdown = _episode(10, 5, characters="甲（杀手｜短发，旧夹克，右眉有疤）", props="铁门｜锈迹斑斑")
    markdown = markdown.replace("[道具]：铁门｜锈迹斑斑", "[道具]：铁门｜锈迹斑斑\n[道具]：铁门｜掉漆")
    assert any("重复登记" in issue.message for issue in check_episode(markdown))


def test_prop_absent_from_body_is_warning_only() -> None:
    markdown = _episode(10, 5, characters="甲（杀手｜短发，旧夹克，右眉有疤）", props="玉佩｜羊脂白玉，编绳发黑")
    warnings = [issue for issue in check_episode(markdown) if issue.level == "warn"]
    assert [issue.code for issue in warnings] == ["E13"]
    assert "未在动作或台词中出现" in warnings[0].message


def test_prop_name_clashing_with_scene_is_warning() -> None:
    markdown = _episode(10, 5, characters="甲（杀手｜短发，旧夹克，右眉有疤）", props="仓库｜铁皮顶棚")
    warnings = [issue for issue in check_episode(markdown) if issue.level == "warn"]
    assert any("重名" in issue.message for issue in warnings)


def test_character_without_appearance_fails() -> None:
    issues = check_episode(_episode(10, 5, characters="甲（杀手）"))
    assert [issue.code for issue in issues] == ["E14"]
    assert "甲" in issues[0].message


def test_character_appearance_shorter_than_eight_chars_fails() -> None:
    issues = check_episode(_episode(10, 5, characters="甲（杀手｜短发旧夹克）"))
    assert [issue.code for issue in issues] == ["E14"]


def test_appearance_must_be_copied_from_bible_verbatim() -> None:
    bible = "【角色】：甲\n【外形】：短发，旧夹克，右眉有疤\n"
    markdown = _episode(10, 5, characters="甲（杀手｜短发，旧夹克，右眉有疤）")
    assert check_episode(markdown, bible=bible) == []

    rewritten = _episode(10, 5, characters="甲（杀手｜短发，旧夹克，左眉有疤）")
    issues = check_episode(rewritten, bible=bible)
    assert [issue.code for issue in issues] == ["E14"]
    assert "被改写或漏抄" in issues[0].message


def test_character_missing_from_bible_fails() -> None:
    bible = "【角色】：乙\n【外形】：圆脸，光头\n"
    issues = check_episode(_episode(10, 5, characters="甲（杀手｜短发，旧夹克，右眉有疤）"), bible=bible)
    assert [issue.code for issue in issues] == ["E14"]
    assert "抄无可抄" in issues[0].message


def test_render_matches_cli_line_format() -> None:
    assert QualityIssue("E12", "fail", "拍数不足").render() == "E12 拍数不足"
    assert QualityIssue("E13", "fail", "道具行缺少外观", 7).render() == "E13 第7行 道具行缺少外观"
