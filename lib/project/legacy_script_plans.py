"""Historical script-plan paths and confirmation metadata for migration and asset rename."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lib.episode.episode_paths import REFERENCE_VIDEO_SCRIPT_PLAN_FILENAME, SCRIPT_PLAN_FILENAMES, episode_drafts_dir
from lib.project.project_manager import find_episode, is_reference_video_project
from lib.script.script_plan_entries import ScriptPlanKind

REVIEW_FIELD = "script_plan_review"


def script_plan_kind(project: dict[str, Any]) -> ScriptPlanKind | None:
    """项目的 script_plan 变体；无结构化 script_plan 中间态（如 ad）时返回 None。

    reference_video 是 generation_mode 维度、跨 content_mode（narration / drama 均可），按项目
    生成模式优先判定；否则按 content_mode 落 drama / narration。content_mode 非
    SCRIPT_PLAN_FILENAMES 成员（ad）即无 script_plan，reference_video 亦不适用。变体由项目两轴唯一决定，
    不随集号变化。
    """
    content_mode = project.get("content_mode")
    if content_mode not in SCRIPT_PLAN_FILENAMES:
        return None
    if is_reference_video_project(project):
        return "reference_video"
    return content_mode  # "drama" | "narration"（SCRIPT_PLAN_FILENAMES 成员）


def script_plan_path(project_path: Path, project: dict[str, Any], episode: int) -> Path | None:
    """该集结构化 script_plan 中间态文件路径；不适用 gate 时返回 None。"""
    kind = script_plan_kind(project)
    if kind is None:
        return None
    filename = REFERENCE_VIDEO_SCRIPT_PLAN_FILENAME if kind == "reference_video" else SCRIPT_PLAN_FILENAMES[kind]
    return episode_drafts_dir(project_path, episode) / filename


def stored_review(project: dict[str, Any], episode: int) -> dict[str, Any]:
    """该集已存的确认记录（``episodes[i].script_plan_review``），缺失或形状坏时返回空 dict。"""
    ep = find_episode(project, episode)
    review = ep.get(REVIEW_FIELD) if ep else None
    return review if isinstance(review, dict) else {}


def apply_confirmation(project: dict[str, Any], episode: int, fingerprint: str, confirmed_at: str) -> bool:
    """就地把确认记录写入 project ``episodes[i].script_plan_review``；集条目不存在返回 False。

    供 service 层在 ProjectManager.update_project 的 RMW 回调内调用，确认指纹的持久化 shape
    单一真相源在此。
    """
    ep = find_episode(project, episode)
    if ep is None:
        return False
    ep[REVIEW_FIELD] = {"fingerprint": fingerprint, "confirmed_at": confirmed_at}
    return True


def carry_confirmation_through_migration(project: dict[str, Any], episode: int, before: str, after: str) -> bool:
    """存量 script_plan 草稿的时长收编迁移是机械格式收编、不是内容编辑，但回写会让内容指纹漂移。

    若该集确认指纹恰好记的是迁移前内容（``before``），就把它平移到迁移后的值（``after``），
    避免一个早已确认的分集仅因被迁移回写就重新等待确认；指纹本就对不上（script_plan 在迁移外确实被
    改过）时不动，返回 False，照常按待确认处理。

    仅适用于迁移无 warnings 的情形（纯格式收编，时长取值未被 clamp 改写）：调用方须在迁移
    产生 warnings 时跳过本函数，让确认照常失效——那种情形下 ``after`` 携带的时长已不是用户
    确认时看到的值，平移确认等于替用户默许了一次未经审阅的内容变更。

    供调用方在 ``ProjectManager.update_project`` 的锁内回调中调用（确保比对的是加锁后重读的
    最新 project）——``server/services/project/script_review.py`` 与 ``lib/script/script_generator.py`` 的两处
    迁移写回入口共用本函数，不各自重复这段判断。资产改名、衍生改名与合并的级联改写同样只换名字、
    不改内容，也经本函数平移（``ProjectManager._carry_script_plan_confirmations``）。
    """
    stored = stored_review(project, episode)
    if stored.get("fingerprint") != before:
        return False
    apply_confirmation(project, episode, after, str(stored.get("confirmed_at") or ""))
    return True
