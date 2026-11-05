"""动态分镜提示词的对象选择：本次编写哪些条目，显式重写会替换哪些已有内容。

分镜类条目只编写 ``image_prompt``。它是参考素材与视频生成共同消费的唯一动态分镜提示词；
历史 ``video_prompt`` 可继续随脚本读取，但不再属于提示词编写能力。补缺保留已有正文，只写空缺；
参考生视频单元的正文即视觉层，按待编写标记展开。点名条目只划定范围，不等于授权覆盖。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from lib.script.script_models import PENDING_AUTHORING_FIELD

#: 各条目形态的视觉层字段。参考生视频单元的正文即其视觉层。
VISUAL_LAYER_FIELDS: dict[str, tuple[str, ...]] = {
    "segments": ("image_prompt",),
    "scenes": ("image_prompt",),
    "shots": ("image_prompt",),
    "video_units": ("text",),
}

#: 省略 ``fields`` 时的编写字段。
DEFAULT_AUTHORING_FIELDS: dict[str, tuple[str, ...]] = {
    "segments": ("image_prompt",),
    "scenes": ("image_prompt",),
    "shots": ("image_prompt",),
    "video_units": ("text",),
}

PromptAuthoringField = Literal["image_prompt"]


def visual_field_filled(value: Any) -> bool:
    """视觉层字段已有内容：非空白字符串，或非空的结构化提示词。"""
    return bool(value.strip() if isinstance(value, str) else value)


def visual_layer_complete(kind: str, item: Mapping[str, Any]) -> bool:
    """条目的视觉层字段都已写入内容。"""
    return all(_field_filled(item, field) for field in VISUAL_LAYER_FIELDS[kind])


def _field_filled(item: Mapping[str, Any], field: str) -> bool:
    return visual_field_filled(item.get(field))


@dataclass(frozen=True, slots=True)
class AuthoringEntry:
    """一个编写条目：本次写回的视觉层字段，及其中会替换已有内容的字段。"""

    entry_id: str
    fields: tuple[str, ...]
    overwritten: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PromptAuthoringSelection:
    """一次提示词编写的对象（按剧本顺序），与范围内因视觉层已齐而不写的条目。"""

    entries: tuple[AuthoringEntry, ...]
    skipped: tuple[str, ...]

    @property
    def overwritten(self) -> tuple[AuthoringEntry, ...]:
        return tuple(entry for entry in self.entries if entry.overwritten)


def _fill_fields(kind: str, item: Mapping[str, Any]) -> tuple[str, ...]:
    if kind == "video_units":
        return VISUAL_LAYER_FIELDS[kind] if item.get(PENDING_AUTHORING_FIELD) is True else ()
    return tuple(field for field in VISUAL_LAYER_FIELDS[kind] if not _field_filled(item, field))


def _limited_fields(kind: str, fields: Iterable[str] = ()) -> tuple[str, ...]:
    requested = tuple(dict.fromkeys(fields))
    if not requested:
        return DEFAULT_AUTHORING_FIELDS[kind]
    allowed = set(VISUAL_LAYER_FIELDS[kind])
    unknown = [field for field in requested if field not in allowed]
    if unknown:
        raise ValueError(f"{kind} 不支持这些提示词字段: {unknown}")
    return requested


def select_prompt_authoring(
    items: Sequence[Mapping[str, Any]],
    *,
    kind: str,
    id_field: str,
    entry_ids: Iterable[str] = (),
    rewrite: bool = False,
    fields: Iterable[str] = (),
) -> PromptAuthoringSelection:
    """按范围与模式选出编写对象。

    ``entry_ids`` 为空时范围是全部带待编写标记的条目，否则是这些条目（调用方先确认它们都在剧本里）。
    补缺只写本次字段范围内缺失的视觉层字段；``rewrite`` 写范围内条目的本次字段，其中补缺本不会写、
    却已有内容的本次字段记为 ``overwritten``，即显式重写需要确认的丢失项。
    """
    target_fields = _limited_fields(kind, fields)
    requested = set(entry_ids)
    entries: list[AuthoringEntry] = []
    skipped: list[str] = []
    for item in items:
        entry_id = str(item[id_field])
        in_scope = entry_id in requested if requested else item.get(PENDING_AUTHORING_FIELD) is True
        if not in_scope:
            continue
        fill = tuple(field for field in _fill_fields(kind, item) if field in target_fields)
        if rewrite:
            overwritten = tuple(field for field in target_fields if field not in fill and _field_filled(item, field))
            entries.append(AuthoringEntry(entry_id, target_fields, overwritten))
        elif fill:
            entries.append(AuthoringEntry(entry_id, fill))
        else:
            skipped.append(entry_id)
    return PromptAuthoringSelection(entries=tuple(entries), skipped=tuple(skipped))


@dataclass(frozen=True, slots=True)
class PromptOverwrite:
    """显式重写将替换的已有视觉层内容。

    ``fingerprint`` 是读取时正式脚本的内容指纹，对外作 ``revision``：调用方认可覆盖时回传它，
    正式脚本此后又有变化即视为未认可，按新清单重新拒绝。
    """

    fingerprint: str | None
    entries: tuple[AuthoringEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.fingerprint,
            "entries": [{"id": entry.entry_id, "fields": list(entry.overwritten)} for entry in self.entries],
        }


class PromptOverwriteRequired(ValueError):
    """显式重写会替换已有视觉层内容，而调用方没有给出与当前正式脚本相符的覆盖令牌。"""

    def __init__(self, overwrite: PromptOverwrite) -> None:
        ids = "、".join(entry.entry_id for entry in overwrite.entries)
        super().__init__(f"显式重写会替换这些条目已有的视觉层内容，需先确认覆盖: {ids}")
        self.overwrite = overwrite


#: 丢失清单里视觉层字段的文案 key。
_FIELD_KEYS: dict[str, str] = {
    "image_prompt": "prompt_overwrite_field_image_prompt",
    "text": "prompt_overwrite_field_text",
}


def render_prompt_overwrite_text(overwrite: Mapping[str, Any], translate: Callable[..., str]) -> str:
    """把 ``PromptOverwrite.to_dict()`` 渲染成丢失清单文本，Web 确认框与 Agent 回执同用这一份。"""
    entries: list[Mapping[str, Any]] = list(overwrite.get("entries") or ())
    separator = translate("script_overwrite_separator")
    rendered = [
        translate(
            "prompt_overwrite_entry",
            id=entry.get("id", ""),
            fields=separator.join(translate(_FIELD_KEYS[field]) for field in entry.get("fields") or ()),
        )
        for entry in entries
    ]
    return "\n".join(
        [
            translate("prompt_overwrite_summary", count=len(entries)),
            translate("prompt_overwrite_entries", entries=separator.join(rendered)),
        ]
    )


def prompt_overwrite_with_text(overwrite: Mapping[str, Any], translate: Callable[..., str]) -> dict[str, Any]:
    """覆盖清单附上渲染好的丢失清单文本（``text``）。"""
    return {**overwrite, "text": render_prompt_overwrite_text(overwrite, translate)}


__all__ = [
    "DEFAULT_AUTHORING_FIELDS",
    "VISUAL_LAYER_FIELDS",
    "AuthoringEntry",
    "PromptAuthoringField",
    "PromptAuthoringSelection",
    "PromptOverwrite",
    "PromptOverwriteRequired",
    "prompt_overwrite_with_text",
    "render_prompt_overwrite_text",
    "select_prompt_authoring",
    "visual_field_filled",
    "visual_layer_complete",
]
