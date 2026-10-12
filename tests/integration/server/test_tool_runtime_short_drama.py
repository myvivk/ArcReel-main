"""short-drama-factory 剧本导入的专项测试。

覆盖：Markdown 解析结构、转写产物形状（short_drama_meta / pending_authoring / 视觉层留空）、
单集导入登记资产、批量导入跨集登记资产、视频时长档位不可用时中止（不落盘）、
以及声明时长与实际切幕时长不一致时的 duration_shortfall 回执。

这些场景跨越 Agent 工具、后端转写与项目资产表，是 short-drama 流程最易回归的部分。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lib.episode.episode_management import create_episode
from lib.project.project_manager import ProjectManager
from lib.script.short_drama_import import (
    ParsedEpisode,
    convert_to_drama_script,
    parse_short_drama_episode,
)
from server import tool_runtime as tool_runtime_module
from server.tool_runtime import (
    CallerContext,
    CreateProjectToolRequest,
    ImportShortDramaScriptRequest,
    ImportShortDramaSeriesRequest,
    ProjectScope,
    Services,
    ToolRequest,
    create_project,
    import_short_drama_script,
    import_short_drama_series,
)


class _Unused:
    pass


_TIERS = [5, 10, 15, 30, 60]


class _FakeCapabilities:
    async def video_capabilities_for_project(self, project: dict[str, Any]) -> dict[str, Any]:
        return {"supported_durations": list(_TIERS)}


class _NoTierCapabilities:
    """模拟未选定视频模型：capabilities 解析直接抛配置类错误。"""

    async def video_capabilities_for_project(self, project: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("no video model selected")


def _services(tmp_path: Path, capabilities: Any = None) -> Services:
    return Services(
        projects=ProjectManager(tmp_path / "projects"),
        workflow_planner=_Unused(),
        capabilities=capabilities if capabilities is not None else _FakeCapabilities(),
    )


async def _setup_drama_project(services: Services, name: str = "demo") -> ProjectScope:
    caller = CallerContext(user_id="test", source="mcp")
    await create_project(
        ToolRequest(
            CreateProjectToolRequest(
                name=name, title="Demo", content_mode="drama", generation_mode="storyboard"
            )
        ),
        caller,
        services,
    )
    create_episode(services.projects, name)
    return ProjectScope(project_name=name, data_root=services.projects.data_root)


# --------------------------------------------------------------------------- #
# 纯 lib：解析
# --------------------------------------------------------------------------- #


def test_parse_short_drama_episode_extracts_structure() -> None:
    md = (
        "第【1】集：逆袭开局\n"
        "【本集时长】：120秒\n"
        "【黄金前3秒钩子】：他回来了\n"
        "【本集断章卡点】：一拳制敌\n"
        "\n"
        "[场景]：豪宅客厅\n"
        "[人物]：林辰（隐忍龙王）、陈峰（嚣张富二代）\n"
        "\n"
        "△ 林辰推开门。\n"
        "陈峰（嚣张）：你也有今天？\n"
        "林辰：滚。\n"
    )
    parsed = parse_short_drama_episode(md)
    assert parsed.episode_number == 1
    assert parsed.title == "逆袭开局"
    assert parsed.total_duration == 120
    assert parsed.opening_hook == "他回来了"
    assert parsed.cliffhanger == "一拳制敌"
    assert parsed.blocks[1].location == "豪宅客厅"
    assert parsed.blocks[1].characters == ["林辰", "陈峰"]
    speakers = [b.speaker for b in parsed.blocks[1].beats if b.kind == "dialogue"]
    assert speakers == ["陈峰", "林辰"]


def test_parse_short_drama_episode_accepts_web_and_skill_headings() -> None:
    # Web 弹窗占位符「# 第 N 集 标题」与 skill 的「第【N】集：标题」「第[N]集」均可解析出集号/标题。
    assert parse_short_drama_episode("# 第 3 集 标题").episode_number == 3
    assert parse_short_drama_episode("第【7】集：标题").episode_number == 7
    assert parse_short_drama_episode("第[2]集").episode_number == 2


# --------------------------------------------------------------------------- #
# 纯 lib：转写
# --------------------------------------------------------------------------- #


def test_convert_to_drama_script_sets_short_drama_meta_and_pending_authoring() -> None:
    md = (
        "第【1】集：逆袭开局\n"
        "【本集时长】：120秒\n"
        "【本集断章卡点】：一拳制敌\n"
        "\n"
        "[场景] 豪宅客厅\n"
        "[人物]：林辰\n"
        "\n"
        "△ 林辰推开门。\n"
    )
    script = convert_to_drama_script(parse_short_drama_episode(md), 1, supported_durations=_TIERS)
    assert script["content_mode"] == "drama"
    assert script["title"] == "逆袭开局"
    assert script["hook"] == "一拳制敌"
    assert script["short_drama_meta"]["duration_seconds"] == 120
    for scene in script["scenes"]:
        assert scene["pending_authoring"] is True
        assert scene["image_prompt"] is None
        assert scene["video_prompt"] is None


# --------------------------------------------------------------------------- #
# 后端：duration shortfall 回执
# --------------------------------------------------------------------------- #


def test_duration_shortfall_reported_when_actual_under_declared() -> None:
    parsed = ParsedEpisode(episode_number=1, title="", total_duration=120)
    script = {"scenes": [{"duration_seconds": 60}, {"duration_seconds": 30}]}
    out = tool_runtime_module._duration_shortfall(parsed, script)
    assert out == {
        "declared_seconds": 120,
        "actual_seconds": 90,
        "duration_shortfall": True,
    }


def test_duration_shortfall_empty_when_matched() -> None:
    parsed = ParsedEpisode(episode_number=1, title="", total_duration=90)
    script = {"scenes": [{"duration_seconds": 60}, {"duration_seconds": 30}]}
    assert tool_runtime_module._duration_shortfall(parsed, script) == {}


# --------------------------------------------------------------------------- #
# 后端：单集导入登记资产
# --------------------------------------------------------------------------- #


async def test_import_short_drama_script_registers_assets(tmp_path: Path) -> None:
    services = _services(tmp_path)
    scope = await _setup_drama_project(services)
    caller = CallerContext(user_id="test", source="mcp")
    md = (
        "第【1】集：逆袭开局\n"
        "【本集时长】：120秒\n"
        "【本集断章卡点】：一拳制敌\n"
        "\n"
        "[场景]：豪宅客厅\n"
        "[人物]：林辰（隐忍龙王）、陈峰（嚣张富二代）\n"
        "\n"
        "△ 林辰推开门。\n"
        "陈峰（嚣张）：你也有今天？\n"
        "林辰：滚。\n"
    )
    outcome = await import_short_drama_script(
        ToolRequest(ImportShortDramaScriptRequest(episode_id=1, markdown=md)),
        scope,
        caller,
        services,
    )
    assert outcome.problem is None
    assert outcome.value["episode"] == 1
    assert "registered_assets" in outcome.value
    reg = outcome.value["registered_assets"]
    assert "林辰" in reg.get("character", []) and "陈峰" in reg.get("character", [])
    assert "豪宅客厅" in reg.get("scene", [])
    project = services.projects.load_project(scope.project_name)
    assert "林辰" in project["characters"]
    assert "豪宅客厅" in project["scenes"]


# --------------------------------------------------------------------------- #
# 后端：批量导入跨集登记资产（单趟 update_project，结果并入每集明细）
# --------------------------------------------------------------------------- #


async def test_import_short_drama_series_registers_assets_across_episodes(tmp_path: Path) -> None:
    services = _services(tmp_path)
    scope = await _setup_drama_project(services)
    caller = CallerContext(user_id="test", source="mcp")
    create_episode(services.projects, scope.project_name)  # 第 2 集

    def _ep(n: int, char: str, scene: str) -> str:
        return (
            f"第【{n}】集：第{n}集\n"
            "【本集时长】：120秒\n"
            "【本集断章卡点】：卡点\n"
            f"\n[场景]：{scene}\n[人物]：{char}\n\n△ 动作。\n"
        )

    outcome = await import_short_drama_series(
        ToolRequest(
            ImportShortDramaSeriesRequest(
                episodes=[
                    {"episode_id": 1, "markdown": _ep(1, "林辰", "豪宅客厅")},
                    {"episode_id": 2, "markdown": _ep(2, "苏婉", "公司大楼")},
                ]
            )
        ),
        scope,
        caller,
        services,
    )
    assert outcome.problem is None
    value = outcome.value
    assert value["imported"] == 2
    for detail in value["details"]:
        assert detail["ok"] is True
        assert "registered_assets" in detail
    project = services.projects.load_project(scope.project_name)
    assert "林辰" in project["characters"]
    assert "苏婉" in project["characters"]
    assert "豪宅客厅" in project["scenes"]
    assert "公司大楼" in project["scenes"]


# --------------------------------------------------------------------------- #
# 后端：视频时长档位不可用 → 中止导入，不落盘
# --------------------------------------------------------------------------- #


async def test_import_short_drama_aborts_when_video_tier_unavailable(tmp_path: Path) -> None:
    services = _services(tmp_path, capabilities=_NoTierCapabilities())
    scope = await _setup_drama_project(services)
    caller = CallerContext(user_id="test", source="mcp")
    md = (
        "第【1】集：逆袭开局\n"
        "【本集时长】：120秒\n"
        "[场景] 豪宅客厅\n[人物] 林辰\n\n△ 动作。\n"
    )
    outcome = await import_short_drama_script(
        ToolRequest(ImportShortDramaScriptRequest(episode_id=1, markdown=md)),
        scope,
        caller,
        services,
    )
    assert outcome.problem is not None
    assert outcome.problem.code == "video_tier_unavailable"
    script_path = services.projects.get_project_path(scope.project_name) / "scripts" / "episode_1.json"
    assert not script_path.exists()


# --------------------------------------------------------------------------- #
# 后端：duration shortfall 进入导入回执
# --------------------------------------------------------------------------- #


async def test_import_short_drama_reports_duration_shortfall_in_result(tmp_path: Path) -> None:
    services = _services(tmp_path)
    scope = await _setup_drama_project(services)
    caller = CallerContext(user_id="test", source="mcp")
    md = (
        "第【1】集：逆袭开局\n"
        "【本集时长】：200秒\n"
        "【本集断章卡点】：卡点\n"
        "\n[场景] 豪宅客厅\n[人物] 林辰\n\n△ 动作。\n"
    )
    outcome = await import_short_drama_script(
        ToolRequest(ImportShortDramaScriptRequest(episode_id=1, markdown=md)),
        scope,
        caller,
        services,
    )
    assert outcome.problem is None
    assert outcome.value["duration_shortfall"] is True
    assert outcome.value["declared_seconds"] == 200
    assert outcome.value["actual_seconds"] < 200
