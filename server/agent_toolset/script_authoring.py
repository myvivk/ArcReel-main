"""工厂生成、工件入库、AI 分镜与提示词编写工具的声明。

文本生成工具是长任务：ArcReel Agent 等到文本任务终态，拿到 ``text_generation``；外部 Agent 立即拿到
``generation_batch`` 句柄并轮询。``dry_run`` 两宿主都直接返回 prompt。
"""

from __future__ import annotations

from typing import Any

from lib.generation.generation_batch import GenerationBatchReadModel
from server.agent_toolset.declaration import BLOCKED, TEXT_OUTPUT_TRUNCATED_NOTE, ToolDeclaration
from server.agent_toolset.envelope import json_value
from server.agent_toolset.short_drama_factory_gen import GENERATE_SHORT_DRAMA_FACTORY
from server.tool_runtime import (
    GenerateEpisodeScriptRequest,
    IngestShortDramaArtifactsRequest,
    PlanShortDramaStoryboardsRequest,
    generate_episode_script,
    ingest_short_drama_artifacts_tool,
    plan_short_drama_storyboards,
)

_TEXT_GENERATION = "text_generation"
_GENERATION_BATCH = "generation_batch"


def _text_generation_structured(value: Any) -> dict[str, Any]:
    """批次句柄放在 ``generation_batch`` 下，生成结果与 dry_run 的 prompt 放在 ``text_generation`` 下。"""
    if isinstance(value, GenerationBatchReadModel):
        return {_GENERATION_BATCH: json_value(value)}
    return {_TEXT_GENERATION: json_value(value)}


GENERATE_EPISODE_SCRIPT = ToolDeclaration(
    name="generate_episode_script",
    description=(
        "提示词编写：为工厂 AI 分镜派生的正式脚本补写视觉层。默认只补缺失的 image_prompt；"
        'video_prompt 依赖已确认参考素材，必须以 fields=["video_prompt"] 单独请求。'
        "同一次请求不能同时写两类提示词。rewrite=true 显式重写点名条目的 fields；"
        "覆盖已有提示词时返回 prompt_overwrite_required 与丢失清单，用户同意后将 revision 作为 overwrite_revision 重试。"
        "工厂正文、台词、备注、尾帧和已生成产物保留。"
        "dry_run=true 时直接返回 prompt，不提交生成任务。" + TEXT_OUTPUT_TRUNCATED_NOTE
    ),
    request_model=GenerateEpisodeScriptRequest,
    migration=BLOCKED,
    domain_key=_TEXT_GENERATION,
    handler=generate_episode_script,
    long_task=True,
    projection=_text_generation_structured,
)

INGEST_SHORT_DRAMA_ARTIFACTS = ToolDeclaration(
    name="ingest_short_drama_artifacts",
    description=(
        "短剧工厂唯一新交稿入口：将七类 Markdown 工件按传入相对路径原样写入 source/short_drama。"
        "每项默认只传 path：服务端读取项目 drafts/short_drama/<path> 中由 Write 暂存的原文，"
        "不要把正文或 Base64 抄进工具参数；外部宿主无法共享文件系统时才传 content_base64。"
        "安全与质量校验后零改写保存，不生成原生剧本 JSON。"
        "系列工件缺失时须随首批提交；每集同批必须提交分集规划、正文、机检报告与合规报告。"
        "两类报告须有明确最终结论（结果: PASSED）；入库复跑完整单集与系列机检，失败整包拒绝。"
        "每集 episodes/episode_NN.md 会同时登记为该集 OWN 源文本（类型记作剧本），使分集页本集正文"
        "与后续独立制作步骤读取工厂正文。集号至少补齐两位。允许路径：character-bible.md、series-outline.md、"
        "ledger.md、episode-plan/episode_NN.plan.md、episodes/episode_NN.md、reports/episode_NN.machine.md、"
        "compliance/episode_NN.compliance.md。"
    ),
    request_model=IngestShortDramaArtifactsRequest,
    migration=BLOCKED,
    domain_key="script",
    handler=ingest_short_drama_artifacts_tool,
)


def _storyboard_summary(value: Any) -> str | None:
    """AI 分镜完成后的一句话返修摘要，让模型不必从长 JSON 里自己数轮次。"""
    if not isinstance(value, dict):
        return None
    rounds = value.get("planning_rounds")
    scenes = value.get("scenes")
    if not isinstance(rounds, list) or not rounds or not isinstance(rounds[-1], dict):
        return None
    final = rounds[-1]
    attempt = final.get("attempt")
    max_attempts = final.get("max_attempts")
    rejected = sum(1 for item in rounds if isinstance(item, dict) and item.get("outcome") == "rejected")
    if not isinstance(attempt, int) or not isinstance(max_attempts, int):
        return None
    head = f"AI 分镜完成：{scenes} 个分镜" if isinstance(scenes, int) else "AI 分镜完成"
    if rejected == 0:
        return f"{head}，第 1/{max_attempts} 轮一次通过。"
    return f"{head}，第 {attempt}/{max_attempts} 轮通过（前 {rejected} 轮被拒，原因见 planning_rounds）。"


PLAN_SHORT_DRAMA_STORYBOARDS = ToolDeclaration(
    name="plan_short_drama_storyboards",
    description=(
        "AI 分镜 / 重新分镜：从已入库的工厂 Markdown 源稿生成视觉派生状态 scripts/episode_N.json。"
        "新分镜省略 regenerate_scene_ids；部分重写传 regenerate_scene_ids；全量重写传 force_regenerate=true。"
        "本集已有分镜图或视频时，必须先取得用户同意并传 confirm_artifacts=true，否则拒绝且不写入。"
        "已有分镜会被覆盖结构与提示词；参考素材、视频提示词和视频会随之过期，媒体保留但需重做。"
        "服务端执行结构、格式、源稿覆盖、各幕时长合计（±5%）校验与最多 5 轮（首轮 + 4 次返修）；"
        "失败整批拒绝、不落盘、不机械回落。规划输入含本集事实卡（黄金钩子、情绪流变、"
        "已登记的场景/人物/道具外形与来路），模型据此写画面，不得自创卡片外的事实。"
        "结果 script.planning_rounds 记录每轮 attempt/outcome/reason/elapsed_ms；有 rejected 轮次时，"
        "用一句向用户说明第几轮因何被拒、最终第几轮通过。Agent 不自行改写分镜。完成后刷新计划并结束本轮。"
    ),
    request_model=PlanShortDramaStoryboardsRequest,
    migration=BLOCKED,
    domain_key="script",
    handler=plan_short_drama_storyboards,
    summary=_storyboard_summary,
)

SCRIPT_AUTHORING_TOOLS = (
    GENERATE_EPISODE_SCRIPT,
    INGEST_SHORT_DRAMA_ARTIFACTS,
    PLAN_SHORT_DRAMA_STORYBOARDS,
    GENERATE_SHORT_DRAMA_FACTORY,
)

__all__ = [
    "GENERATE_EPISODE_SCRIPT",
    "INGEST_SHORT_DRAMA_ARTIFACTS",
    "PLAN_SHORT_DRAMA_STORYBOARDS",
    "SCRIPT_AUTHORING_TOOLS",
]
