"""Agent 工件入库保留七类原文，拒绝不完整的质量交付。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from lib.backends.text_generator import TextGenerator
from lib.episode.episode_management import create_episode
from lib.project.project_manager import ProjectManager
from lib.script.short_drama_import import beat_catalog, parse_short_drama_episode
from lib.short_drama_ingest import ingest_short_drama_artifacts
from server.agent_runtime.entry_pipeline import SessionEntryPipeline
from server.agent_runtime.event_log import EventLogStore, build_quality_gate_entry
from server.agent_runtime.tool_progress import QualityGateRound
from server.agent_toolset.script_authoring import SCRIPT_AUTHORING_TOOLS
from server.tool_runtime import (
    CallerContext,
    IngestShortDramaArtifactsRequest,
    PlanShortDramaStoryboardsRequest,
    ProjectScope,
    Services,
    ToolRequest,
    ingest_short_drama_artifacts_tool,
    plan_short_drama_storyboards,
)
from tests.fakes import FakeTextGenerator
from tests.short_drama_support import factory_package

pytestmark = pytest.mark.usefixtures("factory_profile")


class _NoCapabilities:
    async def video_capabilities_for_project(self, project):
        raise AssertionError("artifact ingestion must not resolve video capabilities")


def _services(tmp_path: Path) -> Services:
    manager = ProjectManager(tmp_path / "projects")
    manager.create_project("demo", content_mode="drama")
    manager.create_project_metadata("demo", "Demo", "Anime", "drama")
    create_episode(manager, "demo")
    return Services(projects=manager, workflow_planner=object(), capabilities=_NoCapabilities())


def _request(package):
    return ToolRequest(
        IngestShortDramaArtifactsRequest(
            artifacts=[{"path": path, "content_base64": base64.b64encode(raw).decode("ascii")} for path, raw in package]
        )
    )


def _draft_request(paths):
    return ToolRequest(IngestShortDramaArtifactsRequest(artifacts=[{"path": path} for path in paths]))


def _stage_drafts(services: Services, package) -> None:
    drafts = services.projects.get_project_path("demo") / "drafts/short_drama"
    for path, raw in package:
        target = drafts / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)


async def test_agent_ingests_original_artifact_bytes_without_native_script(tmp_path):
    services = _services(tmp_path)
    package = factory_package()
    outcome = await ingest_short_drama_artifacts_tool(
        _request(package),
        ProjectScope(project_name="demo", data_root=services.projects.data_root),
        CallerContext(user_id="test", source="mcp"),
        services,
    )
    assert outcome.problem is None
    assert outcome.value["imported"] == 7
    project_path = services.projects.get_project_path("demo")
    for path, raw in package:
        assert (project_path / "source/short_drama" / path).read_bytes() == raw
    assert not (project_path / "scripts/episode_1.json").exists()
    project = services.projects.load_project("demo")
    assert project["episodes"][0]["source_origin"] == "own"
    assert not project.get("characters")


async def test_agent_ingests_drafts_by_path_without_base64(tmp_path):
    services = _services(tmp_path)
    package = factory_package()
    _stage_drafts(services, package)
    outcome = await ingest_short_drama_artifacts_tool(
        _draft_request([path for path, _ in package]),
        ProjectScope(project_name="demo", data_root=services.projects.data_root),
        CallerContext(user_id="test", source="mcp"),
        services,
    )
    assert outcome.problem is None
    assert outcome.value["imported"] == 7
    project_path = services.projects.get_project_path("demo")
    for path, raw in package:
        assert (project_path / "source/short_drama" / path).read_bytes() == raw


async def test_agent_rejects_missing_draft_path(tmp_path):
    services = _services(tmp_path)
    outcome = await ingest_short_drama_artifacts_tool(
        _draft_request(["character-bible.md"]),
        ProjectScope(project_name="demo", data_root=services.projects.data_root),
        CallerContext(user_id="test", source="mcp"),
        services,
    )
    assert outcome.problem is not None
    assert outcome.problem.code == "short_drama_import_refused"
    assert "drafts/short_drama/character-bible.md" in outcome.problem.detail
    assert not list((services.projects.get_project_path("demo") / "source/short_drama").rglob("*.md"))


async def test_agent_storyboard_requires_factory_source(tmp_path):
    services = _services(tmp_path)
    outcome = await plan_short_drama_storyboards(
        ToolRequest(PlanShortDramaStoryboardsRequest(episode_id=1)),
        ProjectScope(project_name="demo", data_root=services.projects.data_root),
        CallerContext(user_id="test", source="mcp"),
        services,
    )
    assert outcome.problem is not None
    assert outcome.problem.code == "short_drama_source_missing"
    assert outcome.problem.action == "collect_project_input"


async def test_agent_rejects_incomplete_quality_package_without_writing(tmp_path):
    services = _services(tmp_path)
    outcome = await ingest_short_drama_artifacts_tool(
        _request([item for item in factory_package() if not item[0].startswith("compliance/")]),
        ProjectScope(project_name="demo", data_root=services.projects.data_root),
        CallerContext(user_id="test", source="mcp"),
        services,
    )
    assert outcome.problem is not None
    assert outcome.problem.code == "short_drama_import_refused"
    assert not list((services.projects.get_project_path("demo") / "source/short_drama").rglob("*.md"))


def test_legacy_markdown_conversion_tools_are_not_exposed():
    names = {tool.name for tool in SCRIPT_AUTHORING_TOOLS}
    assert "ingest_short_drama_artifacts" in names
    assert "import_short_drama_script" not in names
    assert "import_short_drama_series" not in names


def _episode_markdown() -> str:
    """合规单集：105 秒 53 拍，角色与道具都带外观，能通过工厂机检。"""
    lines = [
        "第【1】集：雨夜重逢",
        "【本集时长】：105 秒",
        "[场景]：旧公寓｜水泥楼道，声控灯忽明忽暗",
        "[人物]：林夏（女主｜齐肩短发，米色风衣，右腕有旧疤）、周明（前夫｜寸头，深灰西装，领口松开）",
        "[道具]：铁门｜锈迹斑斑，门轴下垂",
        "△ 铁门被砸得连响。",
    ]
    lines += [f"△ 铁门第{index}下被敲响。" for index in range(1, 49)]
    lines += [
        "林夏（压声）：谁在外面？",
        "周明：开门，我回来了。",
        "林夏：你走错门了。",
        "周明：我带了钥匙。",
        "【本集断章卡点】：门缝里塞进一张旧照片。（黑屏：下滑解锁第2集）",
    ]
    return "\n".join(lines) + "\n"


def _shot_prompt(header: str = "【镜头1｜中景】平视机位，缓推。") -> str:
    return "\n".join(
        [
            header,
            "林夏攥紧门把手，侧耳贴门屏息，指节因用力而发白；",
            "旧公寓@[旧公寓]的水泥楼道里声控灯忽明忽暗，铁门的锈迹映出她绷紧的侧脸，一缕冷光扫过门缝。",
        ]
    )


def _plan_payload(markdown: str, *, header: str = "【镜头1｜中景】平视机位，缓推。", group_size: int = 10) -> str:
    """按源稿 beats 生成一份结构合规的规划回复。"""
    beats = beat_catalog(parse_short_drama_episode(markdown))
    groups = [
        list(range(start, min(start + group_size, len(beats) + 1))) for start in range(1, len(beats) + 1, group_size)
    ]
    return json.dumps(
        {
            "scenes": [
                {
                    "beat_ids": group,
                    "duration_seconds": 12,
                    "segment_break": False,
                    "scene_description": "林夏攥紧门把手，隔着铁门屏息听门外动静。",
                    "image_prompt": _shot_prompt(header),
                    "utterances": [],
                    "characters_in_scene": ["林夏"],
                    "scenes": ["旧公寓"],
                    "props": ["铁门"],
                }
                for group in groups
            ]
        },
        ensure_ascii=False,
    )


def _two_scene_markdown() -> str:
    """全集场景清单格式：先列两个 [场景]，再写人物/道具与 53 个 beat。"""
    lines = [
        "第【1】集：双景",
        "【本集时长】：105 秒",
        "[场景]：旧公寓｜水泥楼道，声控灯忽明忽暗",
        "[场景]：天台｜铁栏围边，城市夜灯铺开",
        "[人物]：林夏（女主｜齐肩短发，米色风衣，右腕有旧疤）、周明（前夫｜寸头，深灰西装，领口松开）",
        "[道具]：铁门｜锈迹斑斑，门轴下垂",
        "[道具]：旧照片｜边缘泛黄，折痕明显",
        "△ 林夏在旧公寓推开铁门。",
        "林夏：谁在外面？",
    ]
    lines += [f"△ 旧公寓的铁门第{index}下被敲响。" for index in range(1, 26)]
    lines += ["△ 林夏攥着旧照片冲上天台。", "周明：把照片还给我。"]
    lines += [f"△ 天台的风掀起旧照片第{index}道折痕。" for index in range(1, 23)]
    lines += ["林夏：这本账你改过。", "周明：我没有。"]
    lines += ["【本集断章卡点】：旧照片背面露出一行字。（黑屏）"]
    return "\n".join(lines) + "\n"


def _two_scene_plan(
    markdown: str,
    first_prompt: str,
    second_prompt: str,
) -> str:
    beats = beat_catalog(parse_short_drama_episode(markdown))
    split = len(beats) // 2
    return json.dumps(
        {
            "scenes": [
                {
                    "beat_ids": list(range(1, split + 1)),
                    "duration_seconds": 13,
                    "segment_break": False,
                    "scene_description": "林夏在旧公寓推门听动静。",
                    "image_prompt": first_prompt,
                    "utterances": [],
                    "characters_in_scene": [],
                    "scenes": ["旧公寓"],
                    "props": [],
                },
                {
                    "beat_ids": list(range(split + 1, len(beats) + 1)),
                    "duration_seconds": 13,
                    "segment_break": False,
                    "scene_description": "林夏在天台与周明对峙。",
                    "image_prompt": second_prompt,
                    "utterances": [],
                    "characters_in_scene": [],
                    "scenes": ["天台"],
                    "props": [],
                },
            ]
        },
        ensure_ascii=False,
    )


def _two_scene_markdown_plan(first_prompt: str, second_prompt: str) -> str:
    """与 ``_two_scene_plan`` 同分组的轻量 Markdown 规划回复（MiniMax 路径）。"""
    return "\n".join(
        [
            "【分镜1｜beats 1-26｜13秒】",
            "描述：林夏在旧公寓推门听动静。",
            first_prompt,
            "",
            "【分镜2｜beats 27-53｜13秒】",
            "描述：林夏在天台与周明对峙。",
            second_prompt,
        ]
    )


def _prepare_storyboard_episode(services: Services, markdown: str) -> None:
    """入库工厂包并登记源稿提到的全部资产（AI 分镜的前置条件）。"""
    from lib.script.short_drama_import import referenced_asset_descriptions

    ingest_short_drama_artifacts(services.projects, "demo", factory_package(markdown))
    for asset_type, descriptions in referenced_asset_descriptions(parse_short_drama_episode(markdown)).items():
        services.projects.upsert_assets(
            "demo",
            {"character": "characters", "scene": "scenes", "prop": "props"}[asset_type],
            {name: {"description": description} for name, description in descriptions.items()},
        )


async def test_plan_emits_one_quality_gate_round_per_attempt(monkeypatch, tmp_path):
    """假文本生成器「拒 2 次、第 3 次通过」：每轮都产出结构化进度，最终成功。"""
    services = _services(tmp_path)
    markdown = _episode_markdown()
    _prepare_storyboard_episode(services, markdown)
    bad = _plan_payload(markdown, header="【镜头1】景别，机位，运镜。")
    good = _plan_payload(markdown)
    model = FakeTextGenerator(bad, bad, good)
    monkeypatch.setattr(TextGenerator, "create", model.create)

    reported: list[QualityGateRound] = []

    async def record(round_: QualityGateRound) -> None:
        reported.append(round_)

    outcome = await plan_short_drama_storyboards(
        ToolRequest(PlanShortDramaStoryboardsRequest(episode_id=1)),
        ProjectScope(project_name="demo", data_root=services.projects.data_root),
        CallerContext(user_id="test", source="embedded", report_progress=record),
        services,
    )

    assert outcome.problem is None
    assert len(model.requests) == 3
    assert [(round_.attempt, round_.outcome) for round_ in reported] == [
        (1, "rejected"),
        (2, "rejected"),
        (3, "passed"),
    ]
    assert all(round_.max_attempts == 5 for round_ in reported)
    assert "缺景别" in (reported[0].reason or "")
    assert reported[2].reason is None
    assert all(round_.elapsed_ms >= 0 for round_ in reported)

    rounds = outcome.value["planning_rounds"]
    assert [item["outcome"] for item in rounds] == ["rejected", "rejected", "passed"]
    assert [item["attempt"] for item in rounds] == [1, 2, 3]
    assert all(item["max_attempts"] == 5 for item in rounds)
    assert rounds[0]["reason"] == reported[0].reason
    assert all(item["elapsed_ms"] >= 0 for item in rounds)


async def test_plan_progress_reaches_the_session_event_log_while_the_tool_runs(monkeypatch, tmp_path, file_db_factory):
    """3 轮场景写 3 条 `system/quality_gate_round` 条目并逐条广播（SSE 来源）。"""
    services = _services(tmp_path)
    markdown = _episode_markdown()
    _prepare_storyboard_episode(services, markdown)
    bad = _plan_payload(markdown, header="【镜头1】景别，机位，运镜。")
    good = _plan_payload(markdown)
    monkeypatch.setattr(TextGenerator, "create", FakeTextGenerator(bad, bad, good).create)

    session_id = "gate-round-session"
    store = EventLogStore(session_factory=file_db_factory)
    broadcasts: list[dict] = []
    seen_at_report: list[int] = []
    pipeline = SessionEntryPipeline(
        store,
        session_id_provider=lambda: session_id,
        broadcast=broadcasts.append,
    )

    async def report(round_: QualityGateRound) -> None:
        await pipeline.append_progress_entry(build_quality_gate_entry(round_))
        # 条目在工具返回前已落库——用户侧不必等工具结束才看到这一轮
        seen_at_report.append(len(await store.list_after(session_id)))

    outcome = await plan_short_drama_storyboards(
        ToolRequest(PlanShortDramaStoryboardsRequest(episode_id=1)),
        ProjectScope(project_name="demo", data_root=services.projects.data_root),
        CallerContext(user_id="test", source="embedded", report_progress=report),
        services,
    )

    assert outcome.problem is None
    assert seen_at_report == [1, 2, 3]
    entries = await store.list_after(session_id)
    gate_entries = [entry for entry in entries if entry.get("subtype") == "quality_gate_round"]
    assert len(gate_entries) == 3
    assert [entry["attempt"] for entry in gate_entries] == [1, 2, 3]
    assert [entry["outcome"] for entry in gate_entries] == ["rejected", "rejected", "passed"]
    assert [entry["seq"] for entry in entries] == sorted(entry["seq"] for entry in entries)
    assert len(broadcasts) == 3
    assert all(item["type"] == "log_entry" for item in broadcasts)


async def test_plan_rejects_dialogue_lines_and_repairs_metadata(monkeypatch, tmp_path):
    """首轮 image_prompt 含台词行被闸门拒绝并回喂；通过后按幕落场景/人物/道具。"""
    services = _services(tmp_path)
    markdown = _two_scene_markdown()
    _prepare_storyboard_episode(services, markdown)
    second_prompt = _shot_prompt().replace("旧公寓@[旧公寓]", "天台@[天台]")
    dirty = _two_scene_plan(markdown, f"{_shot_prompt()}\n台词：林夏：“谁在外面？”", second_prompt)
    clean = _two_scene_plan(markdown, _shot_prompt(), second_prompt)
    monkeypatch.setattr(TextGenerator, "create", FakeTextGenerator(dirty, clean).create)
    reported: list[QualityGateRound] = []

    async def record(round_: QualityGateRound) -> None:
        reported.append(round_)

    outcome = await plan_short_drama_storyboards(
        ToolRequest(PlanShortDramaStoryboardsRequest(episode_id=1)),
        ProjectScope(project_name="demo", data_root=services.projects.data_root),
        CallerContext(user_id="test", source="embedded", report_progress=record),
        services,
    )

    assert outcome.problem is None
    assert len(reported) == 2
    assert reported[0].outcome == "rejected"
    assert "台词行" in (reported[0].reason or "")
    script = services.projects.load_script("demo", "episode_1.json")
    first, second = script["scenes"]
    assert first["scenes"] == ["旧公寓"]
    assert first["characters_in_scene"] == ["林夏"]
    assert first["props"] == ["铁门"]
    assert second["scenes"] == ["天台"]
    assert second["characters_in_scene"] == ["周明", "林夏"]
    assert second["props"] == ["旧照片", "铁门"]
    assert "台词：" not in first["image_prompt"]


async def test_markdown_plan_rejects_dialogue_lines_and_retries(monkeypatch, tmp_path):
    """MiniMax Markdown 路径同样：含台词行被质量闸拒绝并回喂，第二轮干净才落盘。"""
    services = _services(tmp_path)
    markdown = _two_scene_markdown()
    _prepare_storyboard_episode(services, markdown)
    second_prompt = _shot_prompt().replace("旧公寓@[旧公寓]", "天台@[天台]")
    dirty = _two_scene_markdown_plan(f"{_shot_prompt()}\n台词：林夏：“谁在外面？”", second_prompt)
    clean = _two_scene_markdown_plan(_shot_prompt(), second_prompt)
    model = FakeTextGenerator(dirty, clean)
    model.model = "minimax-test"
    monkeypatch.setattr(TextGenerator, "create", model.create)
    reported: list[QualityGateRound] = []

    async def record(round_: QualityGateRound) -> None:
        reported.append(round_)

    outcome = await plan_short_drama_storyboards(
        ToolRequest(PlanShortDramaStoryboardsRequest(episode_id=1)),
        ProjectScope(project_name="demo", data_root=services.projects.data_root),
        CallerContext(user_id="test", source="embedded", report_progress=record),
        services,
    )

    assert outcome.problem is None
    assert len(model.requests) == 2
    assert reported[0].outcome == "rejected"
    assert "台词行" in (reported[0].reason or "")
    script = services.projects.load_script("demo", "episode_1.json")
    assert script["scenes"][0]["scene_id"] == "E1S1"
    assert script["scenes"][0]["scenes"] == ["旧公寓"]
    assert script["scenes"][0]["characters_in_scene"] == ["林夏"]
    assert script["scenes"][1]["scenes"] == ["天台"]


async def test_plan_accepts_scene_token_when_plan_scene_matches(monkeypatch, tmp_path):
    """JSON-schema 路径的 plan.scenes 与提示词场景 token 一致时通过，且不误判未登记。"""
    services = _services(tmp_path)
    markdown = _two_scene_markdown()
    _prepare_storyboard_episode(services, markdown)
    second_prompt = _shot_prompt().replace("旧公寓@[旧公寓]", "天台@[天台]")
    plan = _two_scene_plan(markdown, _shot_prompt(), second_prompt)
    monkeypatch.setattr(TextGenerator, "create", FakeTextGenerator(plan).create)

    outcome = await plan_short_drama_storyboards(
        ToolRequest(PlanShortDramaStoryboardsRequest(episode_id=1)),
        ProjectScope(project_name="demo", data_root=services.projects.data_root),
        CallerContext(user_id="test", source="embedded"),
        services,
    )

    assert outcome.problem is None
    script = services.projects.load_script("demo", "episode_1.json")
    assert [scene["scenes"] for scene in script["scenes"]] == [["旧公寓"], ["天台"]]


async def test_plan_rejects_scene_declaration_contradicting_prompt_token(monkeypatch, tmp_path):
    services = _services(tmp_path)
    markdown = _two_scene_markdown()
    _prepare_storyboard_episode(services, markdown)
    second_prompt = _shot_prompt().replace("旧公寓@[旧公寓]", "天台@[天台]")
    plan = json.loads(_two_scene_plan(markdown, _shot_prompt(), second_prompt))
    plan["scenes"][0]["scenes"] = ["天台"]
    dirty = json.dumps(plan, ensure_ascii=False)
    monkeypatch.setattr(TextGenerator, "create", FakeTextGenerator(*([dirty] * 5)).create)

    outcome = await plan_short_drama_storyboards(
        ToolRequest(PlanShortDramaStoryboardsRequest(episode_id=1)),
        ProjectScope(project_name="demo", data_root=services.projects.data_root),
        CallerContext(user_id="test", source="embedded"),
        services,
    )

    assert outcome.problem is not None
    assert "不一致" in outcome.problem.detail


async def test_plan_failure_keeps_every_rejected_round(monkeypatch, tmp_path):
    """5 轮全拒：原错误语义不变，全部轮次随 problem.params.rounds 回到 Agent。"""
    services = _services(tmp_path)
    markdown = _episode_markdown()
    _prepare_storyboard_episode(services, markdown)
    bad = _plan_payload(markdown, header="【镜头1】景别，机位，运镜。")
    monkeypatch.setattr(TextGenerator, "create", FakeTextGenerator(*([bad] * 5)).create)
    reported: list[QualityGateRound] = []

    async def record(round_: QualityGateRound) -> None:
        reported.append(round_)

    outcome = await plan_short_drama_storyboards(
        ToolRequest(PlanShortDramaStoryboardsRequest(episode_id=1)),
        ProjectScope(project_name="demo", data_root=services.projects.data_root),
        CallerContext(user_id="test", source="embedded", report_progress=record),
        services,
    )

    assert outcome.problem is not None
    assert outcome.problem.code == "short_drama_ai_planning_failed"
    assert "缺景别" in outcome.problem.detail
    assert len(reported) == 5
    rounds = outcome.problem.params["rounds"]
    assert len(rounds) == 5
    assert [item["outcome"] for item in rounds] == ["rejected"] * 5
    assert rounds[-1]["attempt"] == 5
    assert not (services.projects.get_project_path("demo") / "scripts/episode_1.json").exists()


async def test_cached_plan_does_not_report_rounds(monkeypatch, tmp_path):
    """缓存命中不是本轮跑出来的返修，不写 planning_rounds，也不触发进度回调。"""
    services = _services(tmp_path)
    markdown = _episode_markdown()
    _prepare_storyboard_episode(services, markdown)
    good = _plan_payload(markdown)
    model = FakeTextGenerator(good)
    monkeypatch.setattr(TextGenerator, "create", model.create)
    scope = ProjectScope(project_name="demo", data_root=services.projects.data_root)
    reported: list[QualityGateRound] = []

    async def record(round_: QualityGateRound) -> None:
        reported.append(round_)

    first = await plan_short_drama_storyboards(
        ToolRequest(PlanShortDramaStoryboardsRequest(episode_id=1)),
        scope,
        CallerContext(user_id="test", source="embedded", report_progress=record),
        services,
    )
    assert first.problem is None
    reported.clear()
    second = await plan_short_drama_storyboards(
        ToolRequest(PlanShortDramaStoryboardsRequest(episode_id=1)),
        scope,
        CallerContext(user_id="test", source="embedded", report_progress=record),
        services,
    )

    assert second.problem is None
    assert second.value["planning_cached"] is True
    assert "planning_rounds" not in second.value
    assert reported == []
    assert len(model.requests) == 1
