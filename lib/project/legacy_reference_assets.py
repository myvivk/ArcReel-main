"""Historical reference-video image relationships used for artifact comparison."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from lib.infra.path_safety import PathTraversalError, safe_join
from lib.project.asset_types import AssetSpec, asset_name_comparison_key
from lib.references.reference_admission import (
    SHEET_REQUIRED_ASSET_TYPES,
)
from lib.references.reference_catalog import build_reference_catalog
from lib.references.text_parser import derive_references_from_text
from lib.script.script_models import ReferenceResource

if TYPE_CHECKING:
    pass


@dataclass(frozen=True)
class ResolvedReferenceAsset:
    """一个逻辑引用展开出的图片候选；可用性由注入的资产适配器判断。"""

    path: Path
    reference: ReferenceResource
    kind: str = "asset"


class ReferenceAssetAvailability(Protocol):
    """资产可用性适配器；生产实现检查项目内文件并按产物清单认领，测试可用内存替身。"""

    def is_available(self, asset: ResolvedReferenceAsset) -> bool:
        raise NotImplementedError


@dataclass(frozen=True)
class ReferenceAssetHydration:
    available: tuple[ResolvedReferenceAsset, ...]
    missing: tuple[ReferenceResource, ...]


def hydrate_reference_assets(
    declared: Sequence[ReferenceResource],
    resolved_assets: Sequence[ResolvedReferenceAsset],
    availability: ReferenceAssetAvailability,
) -> ReferenceAssetHydration:
    """把候选按声明范围过滤并给出实际可用图片与缺图逻辑引用。"""

    declared_keys = {(ref.type, asset_name_comparison_key(ref.name)) for ref in declared}
    candidates = tuple(asset for asset in resolved_assets if _asset_key(asset) in declared_keys)
    available = tuple(asset for asset in candidates if availability.is_available(asset))
    available_keys = {_asset_key(asset) for asset in available}
    missing = tuple(ref for ref in declared if (ref.type, asset_name_comparison_key(ref.name)) not in available_keys)
    return ReferenceAssetHydration(available=available, missing=missing)


class FilesystemReferenceAssets:
    """以项目目录为边界检查图片候选实际存在且为普通文件。"""

    def __init__(self, project_path: Path) -> None:
        self._project_path = project_path

    def is_available(self, asset: ResolvedReferenceAsset) -> bool:
        try:
            safe_join(self._project_path, asset.path, require_file=True)
        except (FileNotFoundError, OSError, PathTraversalError, TypeError):
            return False
        return True


def _candidate_path(project_path: Path, value: object) -> Path | None:
    if not isinstance(value, (str, Path)) or not str(value):
        return None
    try:
        return safe_join(project_path, value)
    except (OSError, PathTraversalError, TypeError):
        return None


def unit_reference_declarations(project: dict, unit: dict) -> tuple[ReferenceResource, ...]:
    """视频单元正文 → 该单元生成所用的逻辑参考图引用，按首次提及顺序。

    正文是唯一真相：引用不落盘，读侧一律经本函数派生，商品与其它资产走同一条规则、
    没有类型优先级（见 ADR 0064）。未登记的名字不产生引用——它只在渲染与预览侧发一条
    非阻断 warning，不挡住这次生成。
    """

    raw_text = unit.get("text")
    text = raw_text if isinstance(raw_text, str) else ""
    references, _missing = derive_references_from_text(text, project)
    return tuple(references)


def resolve_reference_assets(project: dict, project_path: Path, unit: dict) -> tuple[ResolvedReferenceAsset, ...]:
    """把正文派生的逻辑引用展开为图片候选，不把「路径已登记」误当成「文件存在」。

    有资产图就用资产图。没有资产图时只有商品退到它的全部原图——原图是商品的保真验收锚点
    （见 ADR 0034）；角色、场景、道具的原图只是生成资产图的输入，不进视频请求（见 ADR 0073），
    没有资产图就不产生候选、由 projector 报成阻断。有资产图时任何类型都不额外注入原图，
    也不按类型排序（见 ADR 0064）。缺字段、未登记或越界路径不制造候选，由 projector 对照
    派生引用统一产出 ``reference_asset_missing``。
    """

    catalog = build_reference_catalog(project)
    result: list[ResolvedReferenceAsset] = []
    for reference in unit_reference_declarations(project, unit):
        catalog_entry = catalog.lookup(reference.type, reference.name)
        if catalog_entry is None:
            continue
        entry = catalog_entry.asset
        if not isinstance(entry, dict):
            continue
        spec = catalog_entry.spec
        sheet = _candidate_path(project_path, entry.get(spec.sheet_field))
        if sheet is not None:
            result.append(ResolvedReferenceAsset(path=sheet, reference=reference, kind="sheet"))
            continue
        if catalog_entry.asset_type in SHEET_REQUIRED_ASSET_TYPES:
            continue
        for raw_path in _original_image_paths(entry, spec):
            original = _candidate_path(project_path, raw_path)
            if original is not None:
                result.append(ResolvedReferenceAsset(path=original, reference=reference, kind="original"))
    return tuple(result)


def _original_image_paths(entry: dict, spec: AssetSpec) -> list[object]:
    """该资产条目登记的全部原图路径，按声明顺序。"""

    paths: list[object] = []
    for field_name in spec.original_image_fields:
        value = entry.get(field_name)
        if isinstance(value, list):
            paths.extend(value)
        elif value:
            paths.append(value)
    return paths


def clamp_reference_assets(
    assets: Sequence[ResolvedReferenceAsset], max_references: int | None
) -> tuple[ResolvedReferenceAsset, ...]:
    """超过上限时按正文的提及顺序保留前若干张——没有类型优先级。"""

    if max_references is None or len(assets) <= max_references:
        return tuple(assets)
    return tuple(assets[: max(0, max_references)])


def _asset_key(asset: ResolvedReferenceAsset) -> tuple[str, str]:
    return asset.reference.type, asset_name_comparison_key(asset.reference.name)
