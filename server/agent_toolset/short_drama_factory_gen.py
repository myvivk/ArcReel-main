"""工厂文本生成工具：把剧本/工件写作交给用户配置的强文本模型。

短剧工厂的写作（核心原则①「写作交文本模型」）由本工具经 ``TextGenerator`` 完成，
Agent 只负责组装 prompt、编排、机检与落盘，不自己写正文。
"""

from __future__ import annotations

from server.agent_toolset.declaration import BLOCKED, ToolDeclaration
from server.tool_runtime import ShortDramaFactoryGenRequest, generate_short_drama_factory

GENERATE_SHORT_DRAMA_FACTORY = ToolDeclaration(
    name="generate_short_drama_factory",
    description=(
        "短剧工厂文本生成：把某一类工厂工件的写作交给用户配置的强文本模型。"
        "传入组装好的 prompt（含 references / 模板 / 本集上下文 / 指令）与可选 system_prompt，"
        "工具经 TextGenerator 用 COMPLEX 档文本模型产出该工件 Markdown 原文并返回；"
        "不解析、不转写为 ArcReel JSON。Agent 不得自己写正文，须用本工具产出后由 "
        "ingest_short_drama_artifacts 原样落盘。"
    ),
    request_model=ShortDramaFactoryGenRequest,
    migration=BLOCKED,
    domain_key="script",
    handler=generate_short_drama_factory,
)

__all__ = ["GENERATE_SHORT_DRAMA_FACTORY"]
