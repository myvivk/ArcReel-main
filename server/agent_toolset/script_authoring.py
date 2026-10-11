"""文本生成、内容确认与草稿工具的声明。

两个文本生成工具是长任务：ArcReel Agent 等到文本任务终态，拿到 ``text_generation``；外部 Agent 立即拿到
``generation_batch`` 句柄并轮询。``dry_run`` 两宿主都直接返回 prompt。
"""

from __future__ import annotations

from typing import Any

from lib.generation.generation_batch import GenerationBatchReadModel
from lib.script.draft_quarantine import OPEN_DRAFT_TOOL_NAME, PROMOTE_TOOL_NAME
from server.agent_toolset.declaration import BLOCKED, TEXT_OUTPUT_TRUNCATED_NOTE, ToolDeclaration
from server.agent_toolset.envelope import json_value
from server.tool_runtime import (
    ConfirmScriptReviewRequest,
    DiscardDraftRequest,
    DraftLocator,
    GenerateEpisodeScriptRequest,
    GenerateScriptPlanRequest,
    ImportShortDramaScriptRequest,
    ImportShortDramaSeriesRequest,
    PatchDraftRequest,
    PromoteDraftRequest,
    confirm_script_review,
    discard_draft,
    generate_episode_script,
    generate_script_plan,
    import_short_drama_script,
    import_short_drama_series,
    open_draft,
    patch_draft,
    promote_draft,
)

_TEXT_GENERATION = "text_generation"
_GENERATION_BATCH = "generation_batch"
_DRAFT = "draft"


def _text_generation_structured(value: Any) -> dict[str, Any]:
    """批次句柄放在 ``generation_batch`` 下，生成结果与 dry_run 的 prompt 放在 ``text_generation`` 下。"""
    if isinstance(value, GenerationBatchReadModel):
        return {_GENERATION_BATCH: json_value(value)}
    return {_TEXT_GENERATION: json_value(value)}


GENERATE_EPISODE_SCRIPT = ToolDeclaration(
    name="generate_episode_script",
    description=(
        "提示词编写：为正式脚本中待编写的分镜 / 视频单元补出视觉层，输入是正式脚本自身的内容，不读脚本规划。"
        "默认补缺：范围是全部待编写条目或 entry_ids 点名的条目，图片提示词与视频提示词各自整份判断，已有的保留、"
        "只补缺失的那一份；参考生视频按待编写标记展开单元正文。rewrite=true 显式重写范围内条目的全部视觉层；"
        "会覆盖已有内容时返回 prompt_overwrite_required，回执正文即服务端生成的丢失清单，"
        "params.prompt_overwrite.revision 是认可令牌；先向用户转述清单，得到同意后才以该 revision 作为 overwrite_revision 重新调用。"
        "内容字段、备注、尾帧与已生成产物原样保留。"
        "广告/短片尚无正式脚本时整份生成，结果直接成为正式脚本；引用里的新角色 / 场景 / 道具按 new_assets 的"
        "处理决定随之登记为待生成资产，回执列出这些新资产。regenerate=true 整份重做已有的正式脚本："
        "先返回 script_overwrite_required 与丢失清单，params.script_overwrite.revision 是认可令牌，确认流程同上。"
        "整份生成的产出违约时任务失败（ad_script_rejected），正式脚本、资产与草稿都不变，可带针对性的附加指令重试。"
        "dry_run=true 时直接返回 prompt，不提交生成任务。" + TEXT_OUTPUT_TRUNCATED_NOTE
    ),
    request_model=GenerateEpisodeScriptRequest,
    migration=BLOCKED,
    domain_key=_TEXT_GENERATION,
    handler=generate_episode_script,
    long_task=True,
    projection=_text_generation_structured,
)

GENERATE_SCRIPT_PLAN = ToolDeclaration(
    name="generate_script_plan",
    description=(
        "按项目创作类型生成结构化 script_plan：剧情分镜、旁白分镜或参考生视频单元，"
        "同时在 new_assets 里带出本集未登记的资产与各自的处理决定。"
        "广告/短片项目无 script_plan。dry_run=true 时直接返回 prompt，不提交生成任务。" + TEXT_OUTPUT_TRUNCATED_NOTE
    ),
    request_model=GenerateScriptPlanRequest,
    migration=BLOCKED,
    domain_key=_TEXT_GENERATION,
    handler=generate_script_plan,
    long_task=True,
    projection=_text_generation_structured,
)

CONFIRM_SCRIPT_REVIEW = ToolDeclaration(
    name="confirm_script_review",
    description=(
        "确认本集 script_plan：整份转为正式脚本（全部分镜待编写），按 new_assets 的处理决定登记本集新增资产，"
        "放行 prompt_authoring 视觉生成。"
        "仅在用户已明确认可进入视觉生成时调用。该集已有正式脚本时确认会整份覆盖它，未认可时返回"
        " script_overwrite_required，回执正文即服务端生成的丢失清单（与 Web 确认框同一份文本），"
        "params.script_overwrite.revision 是认可令牌；先向用户转述清单，得到同意后才以该 revision 作为 overwrite_revision 重新确认。"
    ),
    request_model=ConfirmScriptReviewRequest,
    migration=BLOCKED,
    domain_key=_TEXT_GENERATION,
    handler=confirm_script_review,
)

IMPORT_SHORT_DRAMA_SCRIPT = ToolDeclaration(
    name="import_short_drama_script",
    description=(
        "把 short-drama-factory 产出的单集剧本 Markdown 转写为本项目的正式剧情演绎剧本并落盘。"
        "前置：项目须为剧情演绎（drama）且目标集已存在。转换只处理产物（Markdown → 剧本 JSON）："
        "动作行与神态进 scene_description，台词进 utterances，单集按所选视频模型的时长档位切分为多幕"
        "（每幕不超过最大档位）。档位读不出时**中止导入**（不降级成 60 秒）：先到设置里选定视频模型，"
        "或稍后重试、在分集页手动分镜；时长不由 ArcReel 驱动的端点（如 ComfyUI）按旧口径处理。"
        "视觉层留空并置 pending_authoring 待 generate_episode_script 补出，无原生字段的短剧元信息进顶层"
        " short_drama_meta，集尾断章卡点对齐 hook。回执给 episode 与 scenes；若本集声明时长在档位内"
        "排不满（剧本拍数不足或【本集时长】有误），另给 declared_seconds / actual_seconds /"
        " duration_shortfall，此时应补写剧本行或修正时长后重新导入，不要拿缩水的剧本往下走。"
        "不重述短剧工厂的编剧规则——其 Skill 原样复刻于"
        " agent_runtime_profile/.claude/skills/short-drama-factory/。dry_run 不支持。"
    ),
    request_model=ImportShortDramaScriptRequest,
    migration=BLOCKED,
    domain_key="script",
    handler=import_short_drama_script,
)

IMPORT_SHORT_DRAMA_SERIES = ToolDeclaration(
    name="import_short_drama_series",
    description=(
        "批量把 short-drama-factory 的多集剧本 Markdown 转写为本项目的正式剧情演绎剧本并落盘。"
        "每集独立解析转写（动作/神态进 scene_description、台词进 utterances、单集按所选视频模型的时长档位切分多幕、"
        "视觉层留空待 generate_episode_script 补出、短剧元信息进顶层 short_drama_meta、集尾断章对齐 hook），"
        "通过 asyncio.gather 并发执行，并发安全由 ProjectManager 的脚本锁与项目锁保证。"
        "每集明细除 episode / scenes / ok 外，声明时长排不满时另给 declared_seconds / actual_seconds"
        " / duration_shortfall，需补写剧本行或修正时长后重新导入该集。"
        "前置：项目须为 drama 且各目标集已存在。continue_on_error 控制单集失败是否跳过。不重述短剧工厂规则。"
    ),
    request_model=ImportShortDramaSeriesRequest,
    migration=BLOCKED,
    domain_key="script",
    handler=import_short_drama_series,
)

OPEN_DRAFT = ToolDeclaration(
    name=OPEN_DRAFT_TOOL_NAME,
    description="读取指定草稿；草稿不存在时从对应正式文档创建编辑副本。返回完整正文、违约与 canonical revision。",
    request_model=DraftLocator,
    migration=BLOCKED,
    domain_key=_DRAFT,
    handler=open_draft,
)

PATCH_DRAFT = ToolDeclaration(
    name="patch_draft",
    description=(
        "按 canonical revision 原子替换草稿正文；revision 冲突时拒绝且不写入。"
        "给出 accept_formal_revision 即接受正式文档的并发修改，给出 source 即更新重判范围，二者显式传 null 同样生效。"
    ),
    request_model=PatchDraftRequest,
    migration=BLOCKED,
    domain_key=_DRAFT,
    handler=patch_draft,
)

PROMOTE_DRAFT = ToolDeclaration(
    name=PROMOTE_TOOL_NAME,
    description="重新全量校验指定草稿；通过则晋升为正式文件并清除草稿，不通过则刷新违约报告。可反复调用。",
    request_model=PromoteDraftRequest,
    migration=BLOCKED,
    domain_key=_DRAFT,
    handler=promote_draft,
)

DISCARD_DRAFT = ToolDeclaration(
    name="discard_draft",
    description="按 canonical revision 丢弃指定草稿；正式文档保持不变。",
    request_model=DiscardDraftRequest,
    migration=BLOCKED,
    domain_key=_DRAFT,
    handler=discard_draft,
)

SCRIPT_AUTHORING_TOOLS = (
    GENERATE_EPISODE_SCRIPT,
    GENERATE_SCRIPT_PLAN,
    CONFIRM_SCRIPT_REVIEW,
    IMPORT_SHORT_DRAMA_SCRIPT,
    IMPORT_SHORT_DRAMA_SERIES,
    OPEN_DRAFT,
    PATCH_DRAFT,
    PROMOTE_DRAFT,
    DISCARD_DRAFT,
)

__all__ = [
    "CONFIRM_SCRIPT_REVIEW",
    "DISCARD_DRAFT",
    "GENERATE_EPISODE_SCRIPT",
    "GENERATE_SCRIPT_PLAN",
    "OPEN_DRAFT",
    "PATCH_DRAFT",
    "PROMOTE_DRAFT",
    "SCRIPT_AUTHORING_TOOLS",
]
