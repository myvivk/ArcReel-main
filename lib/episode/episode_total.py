"""全剧规划总集数的单一真相源：字段名、硬区间、读时解析。

项目级可选设置，落在 ``project.json`` 顶层。它描述**整部剧计划写多少集**，供「AI 原创剧本」
（剧情演绎 drama 模式）的连写 / 续写流程告诉 Agent 全剧体量、铺排四幕与付费墙位置、判断本批次
是否写到结尾。与 ``episode_target_duration``（单集成片秒数）是两个尺度：前者是「集数」量级，
后者是「秒数」量级。

写入侧（创建 / PATCH 请求模型、``patch_project``）与读取侧共用 ``is_valid_total_episodes`` 这一把尺；
消费方直接读 ``project.json`` 的 ``total_episodes`` 字段，不另设读时辅助（前端与后端都只做区间守卫）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: 全剧规划总集数在 ``project.json`` 的顶层字段名。
TOTAL_EPISODES_FIELD: str = "total_episodes"

#: 全剧规划总集数的硬区间（闭区间，集）。下界 1：少于 1 集不成其为「连续剧」；
#: 上界 1000：远超短视频连续剧的实际体量，更大的数值只可能是误输入（填了总分钟数等）。
MIN_TOTAL_EPISODES: int = 1
MAX_TOTAL_EPISODES: int = 1000


def is_valid_total_episodes(value: Any) -> bool:
    """该数值是否是落在硬区间内的正整数（``1 <= value <= 1000``）。

    写入侧（请求模型、``patch_project``）与读取侧守卫共用这一把尺。只接受 ``int``：
    集数是整数量纲，浮点值一律判为不可用而非静默取整；``bool`` 是 ``int`` 子类，显式排除，
    避免 JSON 里的 ``true`` 被当成 1 集。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return MIN_TOTAL_EPISODES <= value <= MAX_TOTAL_EPISODES


def project_total_episodes(project: Mapping[str, Any] | None) -> int | None:
    """从 project.json 解析全剧规划总集数，未填 / 脏值 / 越界一律返回 ``None``。

    返回 ``None`` 即「未设总集数」，连写流程按「已存在集数 + 本次续写集数」推算，不做硬约束。
    写入侧已按同一把尺拒绝越界值，这里的守卫是对手改 project.json 与历史脏数据的读时兜底。
    """
    if not isinstance(project, Mapping):
        return None
    raw = project.get(TOTAL_EPISODES_FIELD)
    return raw if is_valid_total_episodes(raw) else None
