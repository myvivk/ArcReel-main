"""正式分镜文件定位、内容指纹与覆盖影响，供制作、删除和历史迁移共用。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lib.episode.episode_paths import episode_script_filename
from lib.infra.content_digest import canonical_json_digest
from lib.infra.json_io import load_json_or_none
from lib.infra.path_safety import try_safe_join
from lib.project.project_manager import ProjectManager, find_episode
from lib.script.script_editor import ScriptEditError, resolve_items
from lib.script.script_models import get_generated_assets


def content_fingerprint_of_data(data: object) -> str:
    """已解析 JSON 对象的指纹：规范化 dump（键序 / 空白重排不改指纹）的 sha256。

    供调用方对已读入内存的对象直接取指纹（如迁移前 snapshot），不经二次磁盘读取——指纹须
    对应调用方手里这份内容本身。与 ``content_fingerprint`` 的 JSON 分支同一套规范化逻辑，
    仅入参从路径换成已解析对象，故对同一份内容两者取值相同。
    """
    return canonical_json_digest(data)


def content_fingerprint(path: Path) -> str | None:
    """script_plan 内容指纹：合法 JSON 取规范化 dump 的 sha256（键序 / 空白重排不改指纹、语义变更才改），
    非 JSON 退化为原始字节 sha256；文件不存在（FileNotFoundError）时 None。

    只把「文件不存在」降级为 None（→ no_script_plan、gate 放行）；权限不足、目录占位、短暂 I/O 等其它
    OSError 一律向上抛，避免把真实文件系统故障静默当成「script_plan 未产出」而误放行 prompt_authoring。

    对已读入内存的对象取指纹（如迁移前 snapshot）用 ``content_fingerprint_of_data``，不要对同一
    文件再调一次本函数——两次独立读取之间的并发写入会被此函数的第二次读取吞掉，见其 docstring。
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return hashlib.sha256(raw).hexdigest()
    return content_fingerprint_of_data(parsed)


@dataclass(frozen=True, slots=True)
class OverwrittenScriptEntry:
    """覆盖式确认将移除的一条正式脚本条目，及其名下无法在项目内恢复的产物与归属。"""

    entry_id: str
    has_storyboard: bool
    has_video: bool
    has_narration_audio: bool
    has_end_frame: bool
    grid_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.entry_id,
            "has_storyboard": self.has_storyboard,
            "has_video": self.has_video,
            "has_narration_audio": self.has_narration_audio,
            "has_end_frame": self.has_end_frame,
            "grid_id": self.grid_id,
        }


@dataclass(frozen=True, slots=True)
class FormalScriptOverwrite:
    """内容确认将覆盖的正式脚本：确认后旧条目全部移除，产物按分镜移除的口径撤登记。

    ``fingerprint`` 是读取时正式脚本的内容指纹，对外作 ``revision``：调用方认可覆盖时回传它，
    与确认时读到的正式脚本不符即视为未认可、按新清单重新拒绝；覆盖写入也以它为基线，认可只对应
    这一份被列出的内容。
    """

    fingerprint: str
    entries: tuple[OverwrittenScriptEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.fingerprint,
            "entries": [entry.to_dict() for entry in self.entries],
            "storyboard_count": sum(entry.has_storyboard for entry in self.entries),
            "video_count": sum(entry.has_video for entry in self.entries),
            "narration_audio_count": sum(entry.has_narration_audio for entry in self.entries),
            "end_frame_count": sum(entry.has_end_frame for entry in self.entries),
            "grid_member_count": sum(entry.grid_id is not None for entry in self.entries),
            "grid_count": len({entry.grid_id for entry in self.entries if entry.grid_id is not None}),
        }


#: 覆盖清单里逐条目列出的产物种类：``to_dict`` 的条目键 → 文案 key。顺序即行文顺序。
_OVERWRITE_ENTRY_KINDS: tuple[tuple[str, str], ...] = (
    ("has_storyboard", "script_overwrite_kind_storyboard"),
    ("has_video", "script_overwrite_kind_video"),
    ("has_narration_audio", "script_overwrite_kind_narration_audio"),
    ("has_end_frame", "script_overwrite_kind_end_frame"),
    ("grid_id", "script_overwrite_kind_grid"),
)

#: 覆盖清单的汇总项：（计数键，文案 key）。计数为 0 的项不出现。
_OVERWRITE_LOSS_TOTALS: tuple[tuple[str, str], ...] = (
    ("storyboard_count", "script_overwrite_loss_storyboard"),
    ("video_count", "script_overwrite_loss_video"),
    ("narration_audio_count", "script_overwrite_loss_narration_audio"),
    ("end_frame_count", "script_overwrite_loss_end_frame"),
    ("grid_member_count", "script_overwrite_loss_grid"),
)


def render_overwrite_loss_text(overwrite: Mapping[str, Any], translate: Callable[..., str]) -> str:
    """把 ``FormalScriptOverwrite.to_dict()`` 渲染成丢失清单文本。

    Web 确认框与 Agent 的 ``script_overwrite_required`` 回执都取这一份文本，文案只登记在 i18n key 表里；
    调用方不再各自拼装清单。
    """
    entries: list[Mapping[str, Any]] = list(overwrite.get("entries") or ())
    separator = translate("script_overwrite_separator")
    lines = [translate("script_overwrite_summary", count=len(entries))]
    totals = [
        translate(key, count=count, grids=overwrite.get("grid_count", 0))
        for count_key, key in _OVERWRITE_LOSS_TOTALS
        if (count := int(overwrite.get(count_key) or 0)) > 0
    ]
    if totals:
        lines.append(translate("script_overwrite_loss", items=separator.join(totals)))
    if entries:
        lines.append(translate("script_overwrite_history"))
        rendered: list[str] = []
        for entry in entries:
            kinds = [translate(key) for field, key in _OVERWRITE_ENTRY_KINDS if entry.get(field)]
            rendered.append(
                translate("script_overwrite_entry", id=entry.get("id", ""), kinds=separator.join(kinds))
                if kinds
                else str(entry.get("id", ""))
            )
        lines.append(translate("script_overwrite_entries", entries=separator.join(rendered)))
    return "\n".join(lines)


def overwrite_with_text(overwrite: Mapping[str, Any] | None, translate: Callable[..., str]) -> dict[str, Any] | None:
    """覆盖清单附上渲染好的丢失清单文本（``text``）；无覆盖时 None。"""
    if overwrite is None:
        return None
    return {**overwrite, "text": render_overwrite_loss_text(overwrite, translate)}


def bound_script_filename(project: Mapping[str, Any], episode: int) -> str | None:
    """该集 project.json 绑定的剧本文件名（归一到 ``scripts/`` 下的名字）；未绑定时 None。"""

    entry = find_episode(project, episode)
    binding = entry.get("script_file") if isinstance(entry, dict) else None
    if not isinstance(binding, str) or not binding:
        return None
    return ProjectManager.normalize_script_filename(binding) or None


class ForeignFormalScriptError(ValueError):
    """该集没有可读写的正式脚本位置：绑定不在盘上，而规范路径上那份文件不是本集剧本。

    携带集 ID 与占位的文件名，供调用方给出可定位的提示。继承 ``ValueError`` 让尚未单独处置这一形态
    的调用点也按「拒绝」而不是按「成功」收场。
    """

    def __init__(self, episode: int, filename: str) -> None:
        super().__init__(
            f"集（id={episode}）的规范剧本路径 scripts/{filename} 上是另一集的剧本，不能当作本集正式脚本读写"
        )
        self.episode = episode
        self.filename = filename


def formal_script_filename(project_path: Path, project: Mapping[str, Any], episode: int) -> str:
    """该集正式脚本在 ``scripts/`` 下的文件名。

    project.json 绑定的 ``script_file`` 指向盘上文件时取绑定；绑定缺失、越界或文件不存在时回落规范
    文件名 ``episode_N.json``。内容确认读覆盖清单与写正式脚本都经这里，两者始终是同一份文件。

    回落只在规范路径空着、或那上面确是本集剧本时成立：迁移跳过的集绑着已消失的旧路径，而规范路径
    上放着别集剧本，无条件回落会让读拿到别集内容、让写整份重建别集的在世剧本并把本集绑上去。这一
    形态抛 ``ForeignFormalScriptError``；归属判据（读不成对象或内部集号不符）与 v14→v15 改名前的
    那次校验一致。
    """
    filename = bound_script_filename(project, episode)
    if filename is not None:
        path = try_safe_join(project_path / "scripts", filename)
        if path is not None and path.is_file():
            return filename
    canonical = episode_script_filename(episode)
    canonical_path = project_path / "scripts" / canonical
    if canonical_path.is_file():
        script = load_json_or_none(canonical_path)
        # 集号按正整数严格判：剧本是裸读进来的，JSON ``true`` 变成 Python ``True``，它既是 ``int``
        # 又等于 ``1``，按 ``!=`` 比会让脏文件冒充第 1 集通过归属校验。
        recorded = script.get("episode") if isinstance(script, dict) else None
        if type(recorded) is not int or recorded != episode:
            raise ForeignFormalScriptError(episode, canonical)
    return canonical


def formal_script_overwrite(
    project_path: Path, project: Mapping[str, Any], episode: int
) -> FormalScriptOverwrite | None:
    """读取内容确认将覆盖的正式脚本；该集尚无正式脚本时返回 None。

    读的是 ``formal_script_filename`` 解析出的那份剧本，即确认转换将写入的文件。文件存在但读不成
    剧本（非法 JSON、条目数组损坏）时照样算已有正式脚本、条目列表为空：覆盖它仍需认可。

    规范路径上是别集剧本时按无剧本返回 None——那份内容不属本集，不能列进本集的覆盖清单充数；
    确认转换会在写盘前以 ``ForeignFormalScriptError`` 拒绝。
    """
    try:
        filename = formal_script_filename(project_path, project, episode)
    except ForeignFormalScriptError:
        return None
    path = project_path / "scripts" / filename
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return FormalScriptOverwrite(fingerprint=hashlib.sha256(raw).hexdigest(), entries=())
    fingerprint = content_fingerprint_of_data(parsed)
    if not isinstance(parsed, dict):
        return FormalScriptOverwrite(fingerprint=fingerprint, entries=())
    try:
        items, id_field, _kind = resolve_items(parsed)
    except ScriptEditError:
        items, id_field = [], ""
    entries: list[OverwrittenScriptEntry] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get(id_field), str) or not item[id_field]:
            continue
        assets = get_generated_assets(item)
        grid_id = assets.get("grid_id")
        entries.append(
            OverwrittenScriptEntry(
                entry_id=item[id_field],
                has_storyboard=bool(assets.get("storyboard_image")),
                has_video=bool(assets.get("video_clip")),
                has_narration_audio=bool(assets.get("narration_audio")),
                has_end_frame=bool(item.get("end_frame_image")),
                grid_id=grid_id if isinstance(grid_id, str) and grid_id else None,
            )
        )
    return FormalScriptOverwrite(fingerprint=fingerprint, entries=tuple(entries))
