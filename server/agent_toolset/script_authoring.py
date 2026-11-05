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
        "分镜提示词编写：为机械分镜产物的正式脚本补写唯一的 image_prompt。"
        "输入是上一步落盘的分镜结构（分镜号、时长、出场资产、逐字口播、原文锚、视觉改编），"
        "不重新分镜、不改台词与结构。参考素材与视频生成都直接消费该提示词，不再另写 video_prompt。"
        "rewrite=true 显式重写点名条目的 image_prompt；"
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
    """机械分镜完成后的一句话摘要，让模型不必从长 JSON 里自己数。"""
    if not isinstance(value, dict):
        return None
    scenes = value.get("scenes")
    pending = value.get("pending_authoring")
    if not isinstance(scenes, int):
        return None
    head = f"机械分镜完成：{scenes} 个分镜"
    if isinstance(pending, int) and pending:
        return f"{head}，{pending} 个待编写分镜提示词；下一步请执行分镜提示词编写。"
    return f"{head}，分镜提示词均已就绪。"


PLAN_SHORT_DRAMA_STORYBOARDS = ToolDeclaration(
    name="plan_short_drama_storyboards",
    description=(
        "机械分镜 / 重新分镜：从已入库的工厂 Markdown 源稿确定性生成分镜结构 scripts/episode_N.json。"
        "边界、时长、出场资产、逐字口播与原文锚由代码计算，不调用文本模型，也没有返修轮次。"
        "image_prompt 留空并置待编写，随后必须用 generate_episode_script 作为下一步编写分镜提示词。"
        "新分镜省略 regenerate_scene_ids；部分重切传 regenerate_scene_ids；全量重切传 force_regenerate=true。"
        "本集已有分镜图或视频时，必须先取得用户同意并传 confirm_artifacts=true，否则拒绝且不写入。"
        "重切会覆盖结构与提示词；参考素材和视频会随之过期，媒体保留但需重做。"
        "Agent 不自行改写分镜；完成后刷新计划并结束本轮。"
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
