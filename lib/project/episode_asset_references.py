"""本集引用的资产：一集剧本（工厂源稿 / 正式脚本）与故事规划里写到的资产与衍生。

「本集要用到哪些资产」只在这里算一次：集层批量生成资产图、Agent 按集列出与生成待生成资产、
制作状态里「本集引用的资产缺资产图」都取这一份，三处不各自拼集合。

引用落点与级联改名、引用状态共用 :func:`lib.script.script_references.payload_reference_names`
（引用数组、``speaker``、正文 ``@[名称]``），名字归属由 :class:`ReferenceCatalog` 决议，未登记的
名字不算。衍生引用 ``本体/衍生`` 同时算引用了本体：衍生资产图是对本体资产图的一次编辑，
本体没有可用资产图时衍生无从生成。

抽取输入是**本集剧本（工厂源稿）+ 故事规划**（方案 §2.2）：正式脚本的引用之外，工厂源稿的
``[人物]/[场景]/[道具]`` 标记行（``名称｜外观描述``）与故事规划（人物圣经 / 全剧大纲 /
连续性台账 / 分集规划）定义或提及的名字同样算本集引用——剧本尚未落成引用数组、但源稿与
规划已点名的资产也要进本集视图与批量计划，否则「登记了却看不见、出了描述不出图」。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lib.project.asset_derivatives import derivative_artifact_id, derivative_table
from lib.project.asset_types import resolve_asset_key
from lib.project.story_plan_docs import marker_asset_names
from lib.references.reference_catalog import build_reference_catalog, split_derivative_reference
from lib.script.script_references import payload_reference_names
from lib.script.short_drama_import import fragment_scene_names

#: 故事规划名字按类型的分组键；与源稿标记行、登记口径同一三类。
_STORY_PLAN_TYPES: tuple[str, ...] = ("character", "scene", "prop")


@dataclass(frozen=True, slots=True)
class ReferencedAsset:
    """一条被本集引用的资产或衍生。"""

    asset_type: str
    #: 资产表里的落盘真名；衍生写作 ``本体/衍生``（两段都是落盘真名）。
    name: str
    #: 衍生所属本体的落盘真名；本体条目为 ``None``。
    owner: str | None = None


def episode_referenced_assets(
    project: Mapping[str, Any],
    script: Mapping[str, Any] | None,
    *,
    source_markdown: str | None = None,
    story_plan_names: Mapping[str, Sequence[str]] | None = None,
) -> frozenset[ReferencedAsset]:
    """本集剧本（工厂源稿 / 正式脚本）与故事规划引用的全部已登记资产与衍生。

    ``source_markdown`` 是本集工厂源稿全文（标记行 ``名称｜外观描述`` 的名字计入）；
    ``story_plan_names`` 是故事规划里定义 / 提及的资产名（按类型分组，见
    :func:`lib.project.story_plan_docs.story_plan_asset_names`）。两个补充来源的名字同样
    经 :class:`ReferenceCatalog` 决议——只算已登记的，未登记的名字不算引用。
    """

    # 工厂源稿存在时不从可能已过期的视觉制作状态补入剧情资产。
    names: list[tuple[str | None, str]] = (
        [(None, name) for name in payload_reference_names(script)] if script is not None and not source_markdown else []
    )
    if source_markdown:
        markers = marker_asset_names(source_markdown)
        for asset_type in _STORY_PLAN_TYPES:
            names.extend((asset_type, name) for name in markers.get(asset_type, ()))
        # 碎片空间：已登记、在本集叙事里出现、却未用 [场景] 声明的地点（钩子 / 回忆里的旧地）。
        # 它们同样要出资产图，否则分镜里只能靠文字硬编；口径与分镜侧的 fragment_scene_names 同源。
        names.extend(
            ("scene", name) for name in fragment_scene_names((project.get("scenes") or {}).keys(), source_markdown)
        )
    if story_plan_names:
        for asset_type in _STORY_PLAN_TYPES:
            names.extend((asset_type, name) for name in story_plan_names.get(asset_type, ()))
    if not names:
        return frozenset()
    catalog = build_reference_catalog(project)
    referenced: set[ReferencedAsset] = set()
    for declared_type, name in names:
        entry = catalog.resolve(name)
        if entry is None or (declared_type is not None and entry.asset_type != declared_type):
            continue
        bucket = project.get(entry.spec.bucket_key)
        owner_key = resolve_asset_key(bucket, entry.asset_name)
        if owner_key is None:
            continue
        referenced.add(ReferencedAsset(asset_type=entry.asset_type, name=owner_key))
        _base, derivative_name = split_derivative_reference(entry.name)
        if not derivative_name:
            continue
        table = derivative_table(bucket.get(owner_key) if isinstance(bucket, Mapping) else None)
        derivative_key = resolve_asset_key(table, derivative_name)
        if derivative_key is None:
            continue
        referenced.add(
            ReferencedAsset(
                asset_type=entry.asset_type,
                name=derivative_artifact_id(owner_key, derivative_key),
                owner=owner_key,
            )
        )
    return frozenset(referenced)


__all__ = ["ReferencedAsset", "episode_referenced_assets"]
