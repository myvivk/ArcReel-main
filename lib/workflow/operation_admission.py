"""AI 操作的结构准入：每个操作一份谓词，制作状态与操作入口调用同一份（见 ``docs/adr/0091``）。

准入属于 AI 操作，不属于内容，也不属于流水线位置：每个谓词只检查该操作自己的输入是否成立。
界面上的置灰原因、Agent 被拒的理由与建议下一步的依据都取自同一份结论，理由码是稳定的闭集。

这里只算**结构准入**：只读项目目录里已落盘的内容，不报价、不问供应商、不求视频请求事实。
需要这些外部事实的**提交准入**仍在预览与提交时计算（整批准入见 ``docs/adr/0061``）。
生成入口的引用准入维持 ``docs/adr/0073``，由 ``lib.references.reference_admission`` 承担。

谓词的输入是事实而不是路径：同一条事实由本模块的事实函数统一取得，制作状态与入口各自读盘时
经同一个事实函数，不各写一份「有没有源文」的判据。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from lib.script.script_models import PENDING_AUTHORING_FIELD
from lib.script.script_skeleton import SKELETONS


class AdmissionState(StrEnum):
    """一个 AI 操作此刻的准入结论。"""

    ADMITTED = "admitted"
    REFUSED = "refused"
    #: 该操作对此类项目不适用（如广告/短片没有分集规划），不是缺了什么输入。
    NOT_APPLICABLE = "not_applicable"


class AdmissionReason(StrEnum):
    """不能执行的理由码闭集；界面按码取文案，Agent 按码转述。"""

    NOT_APPLICABLE = "operation_not_applicable"

    FORMAL_SCRIPT_MISSING = "formal_script_missing"

    NO_PENDING_AUTHORING = "no_pending_authoring"
    PROMPT_AUTHORING_DRAFT_PENDING = "prompt_authoring_draft_pending"

    NO_AVAILABLE_VIDEO = "no_available_video"
    NO_MISSING_SHOT_GRID = "no_missing_shot_grid"
    NO_MISSING_VIDEO_PROMPT = "no_missing_video_prompt"


class OperationAdmission(BaseModel):
    """一个 AI 操作的结构准入结论：放行时 ``reason`` 为空，其余两态必带理由码。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: AdmissionState
    reason: AdmissionReason | None = None

    @property
    def admitted(self) -> bool:
        return self.state is AdmissionState.ADMITTED


ADMITTED = OperationAdmission(state=AdmissionState.ADMITTED)
NOT_APPLICABLE = OperationAdmission(state=AdmissionState.NOT_APPLICABLE, reason=AdmissionReason.NOT_APPLICABLE)


def _refused(reason: AdmissionReason) -> OperationAdmission:
    return OperationAdmission(state=AdmissionState.REFUSED, reason=reason)


# ---------------------------------------------------------------------------
# 事实函数：制作状态与操作入口取同一条事实的唯一口径
# ---------------------------------------------------------------------------


def pending_authoring_entry_ids(items: Sequence[Any], kind: str | None) -> list[str]:
    """正式脚本里带待编写标记的条目 id，按脚本顺序；四种骨架都由提示词编写补写。"""
    if kind not in SKELETONS:
        return []
    id_field = SKELETONS[kind].id_field
    return [
        str(item[id_field])
        for item in items
        if isinstance(item, Mapping)
        and isinstance(item.get(id_field), str)
        and item[id_field]
        and item.get(PENDING_AUTHORING_FIELD) is True
    ]


# ---------------------------------------------------------------------------
# 准入谓词：每个 AI 操作一份
# ---------------------------------------------------------------------------


def admit_author_prompts(
    *,
    formal_script: bool,
    pending_ids: Sequence[str],
    draft_pending: bool,
    explicit_ids: Sequence[str] = (),
) -> OperationAdmission:
    """提示词编写：正式脚本中有待编写条目；点名补缺或重写的条目不看待编写标记。

    编写自身的草稿在场时先处置草稿，编写结果无处落地。
    """
    if draft_pending:
        return _refused(AdmissionReason.PROMPT_AUTHORING_DRAFT_PENDING)
    if not formal_script:
        return _refused(AdmissionReason.FORMAL_SCRIPT_MISSING)
    if pending_ids or explicit_ids:
        return ADMITTED
    return _refused(AdmissionReason.NO_PENDING_AUTHORING)


def admit_edit_timeline(*, available_videos: int) -> OperationAdmission:
    """剪辑（新建剪辑时间线、交给 Agent 剪辑）：本集至少有一个可用视频（current 或 stale）。"""
    return ADMITTED if available_videos > 0 else _refused(AdmissionReason.NO_AVAILABLE_VIDEO)


def admit_shot_grids(
    content_mode: object,
    *,
    generation_mode: object,
    formal_script: bool,
    missing_ids: Sequence[str],
) -> OperationAdmission:
    """参考素材（单分镜宫格）：分镜图项目里，正式脚本在位且有分镜缺参考素材。

    与「分镜图」行同一把尺子：没有正式脚本就无从谈产物；一个都不缺时这条操作无事可做。
    参考生视频与广告不走这一步，报「不适用」而不是「缺东西」。
    """
    if content_mode != "drama" or generation_mode != "storyboard":
        return NOT_APPLICABLE
    if not formal_script:
        return _refused(AdmissionReason.FORMAL_SCRIPT_MISSING)
    if not missing_ids:
        return _refused(AdmissionReason.NO_MISSING_SHOT_GRID)
    return ADMITTED


def admit_author_video_prompts(
    content_mode: object,
    *,
    generation_mode: object,
    formal_script: bool,
    target_ids: Sequence[str],
) -> OperationAdmission:
    """视频提示词编写：分镜图项目里，宫格已确认且视频提示词还空着的分镜至少一个。

    没有已确认的参考素材就无从按 K 帧写动作；一个都不缺时这条操作无事可做。
    """

    if content_mode != "drama" or generation_mode != "storyboard":
        return NOT_APPLICABLE
    if not formal_script:
        return _refused(AdmissionReason.FORMAL_SCRIPT_MISSING)
    if not target_ids:
        return _refused(AdmissionReason.NO_MISSING_VIDEO_PROMPT)
    return ADMITTED


__all__ = [
    "ADMITTED",
    "NOT_APPLICABLE",
    "AdmissionReason",
    "AdmissionState",
    "OperationAdmission",
    "admit_author_prompts",
    "admit_author_video_prompts",
    "admit_edit_timeline",
    "admit_shot_grids",
    "pending_authoring_entry_ids",
]
