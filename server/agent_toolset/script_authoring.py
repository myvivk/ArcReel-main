"""工厂生成、工件入库与提示词编写工具的声明。

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
    generate_episode_script,
    ingest_short_drama_artifacts_tool,
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
        "content_base64 必须是工件 UTF-8 原文的 Base64 编码；安全与质量校验后零改写保存，不生成原生剧本 JSON。"
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

SCRIPT_AUTHORING_TOOLS = (
    GENERATE_EPISODE_SCRIPT,
    INGEST_SHORT_DRAMA_ARTIFACTS,
    GENERATE_SHORT_DRAMA_FACTORY,
)

__all__ = [
    "GENERATE_EPISODE_SCRIPT",
    "INGEST_SHORT_DRAMA_ARTIFACTS",
    "SCRIPT_AUTHORING_TOOLS",
]
