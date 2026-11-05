"""
Helpers for storyboard sequence ordering and dependency planning.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from lib.infra.path_safety import safe_join, try_safe_join
from lib.project.asset_types import ASSET_SPECS
from lib.project.resource_paths import END_FRAME_RESOURCE_TYPE, resource_relative_path
from lib.script.script_editor import resolve_items
from lib.script.script_models import get_generated_assets
from lib.script.script_skeleton import SKELETONS
from lib.script.shot_prompt_standard import lint_shot_prompt, storyboard_motion_prompt_problem

StoryboardVideoAssetKind = Literal["character", "scene", "prop"]
StoryboardVideoAssetImage = tuple[StoryboardVideoAssetKind, str, Path]

_VIDEO_ASSET_FIELDS: tuple[tuple[StoryboardVideoAssetKind, str], ...] = (
    ("character", "characters_in_scene"),
    ("scene", "scenes"),
    ("prop", "props"),
)


def storyboard_motion_prompt(item: Mapping[str, Any]) -> str:
    """返回同时供参考素材与视频生成使用的唯一动态分镜提示词。"""

    prompt = item.get("image_prompt")
    if isinstance(prompt, str):
        text = prompt.strip()
    elif isinstance(prompt, Mapping):
        scene = prompt.get("scene")
        text = scene.strip() if isinstance(scene, str) else ""
    else:
        text = ""
    if not text:
        item_id = item.get("scene_id") or item.get("segment_id") or item.get("shot_id")
        raise ValueError(f"分镜缺少 image_prompt 动态分镜提示词: {item_id}")
    return text


def storyboard_motion_prompt_item_problem(project: Mapping[str, Any], item: Mapping[str, Any]) -> str | None:
    """按项目登记资产、分镜出场清单和时长预检唯一动态分镜提示词。"""

    try:
        prompt = storyboard_motion_prompt(item)
    except ValueError as exc:
        return str(exc)
    registered: list[str] = []
    for spec in ASSET_SPECS.values():
        bucket = project.get(spec.bucket_key)
        if isinstance(bucket, Mapping):
            registered.extend(str(name) for name in bucket if str(name).strip())
    allowed: list[str] = []
    for _kind, field in _VIDEO_ASSET_FIELDS:
        names = item.get(field)
        if isinstance(names, list):
            allowed.extend(str(name) for name in names if isinstance(name, str) and name.strip())
    products = item.get("products_in_shot")
    if isinstance(products, list):
        allowed.extend(str(name) for name in products if isinstance(name, str) and name.strip())
    characters = project.get(ASSET_SPECS["character"].bucket_key)
    character_names = tuple(str(name) for name in characters) if isinstance(characters, Mapping) else ()
    if problem := lint_shot_prompt(
        prompt,
        registered_names=registered,
        allowed_names=allowed,
        character_names=character_names,
    ):
        return problem
    return storyboard_motion_prompt_problem(prompt, item.get("duration_seconds"))


def require_confirmed_storyboard_grid(item: Mapping[str, Any], resource_id: str) -> Mapping[str, Any]:
    """要求分镜绑定一份已确认、带完整 K 帧路径的 ShotGrid。"""

    assets = item.get("generated_assets")
    binding = assets.get("shot_grid") if isinstance(assets, Mapping) else None
    if not isinstance(binding, Mapping):
        raise StoryboardImageBindingRequired(f"confirmed shot grid binding missing: {resource_id}")
    grid_id = binding.get("grid_id")
    confirmed_at = binding.get("confirmed_at")
    cell_paths = binding.get("cell_paths")
    if (
        not isinstance(grid_id, str)
        or not grid_id.strip()
        or not isinstance(confirmed_at, str)
        or not confirmed_at.strip()
        or not isinstance(cell_paths, list)
        or not cell_paths
        or any(not isinstance(path, str) or not path.strip() for path in cell_paths)
    ):
        raise StoryboardImageBindingRequired(f"confirmed shot grid binding incomplete: {resource_id}")
    return binding


def resolve_storyboard_video_asset_images(
    *,
    project_path: Path,
    project: dict[str, Any],
    item: dict[str, Any],
) -> list[StoryboardVideoAssetImage]:
    """解析参考生视频要随请求下发的资产图，按「角色 → 场景 → 道具」并保持清单顺序。

    返回 ``(asset_type, 登记名, 路径)``。缺登记、缺资产图或文件不在的条目直接略过——它们
    不进请求，也就不能被正文编号指认。数组顺序即编号顺序：资产图在前、K 帧在后。
    """

    resolved: list[StoryboardVideoAssetImage] = []
    seen: set[str] = set()
    for asset_type, field in _VIDEO_ASSET_FIELDS:
        names = item.get(field)
        if not isinstance(names, list):
            continue
        spec = ASSET_SPECS[asset_type]
        bucket = project.get(spec.bucket_key)
        if not isinstance(bucket, dict):
            continue
        for name in names:
            if not isinstance(name, str) or not name.strip():
                continue
            entry = bucket.get(name)
            if not isinstance(entry, dict):
                continue
            relative = entry.get(spec.sheet_field)
            if not isinstance(relative, str) or not relative.strip():
                continue
            path = try_safe_join(project_path, relative)
            if path is None or not path.is_file() or str(path) in seen:
                continue
            seen.add(str(path))
            resolved.append((asset_type, name.strip(), path))
    return resolved


def split_storyboard_video_asset_images(
    asset_images: Sequence[StoryboardVideoAssetImage],
    *,
    max_reference_images: int | None,
    frame_count: int,
) -> tuple[list[StoryboardVideoAssetImage], list[StoryboardVideoAssetImage]]:
    """按 ``max_reference_images`` 的剩余名额切分资产图，返回 ``(随请求下发, 被裁掉)``。

    K 帧优先占用名额，资产图只拿剩下的；``max_reference_images`` 为 ``None`` / ``0`` 表示
    后端不按数量裁剪，全部保留。切分与裁剪口径只有这一处，准入、执行、预览共用。
    """

    limit = max_reference_images or 0
    room = max(0, limit - frame_count) if limit else len(asset_images)
    return list(asset_images[:room]), list(asset_images[room:])


@dataclass(frozen=True)
class StoryboardTaskPlan:
    resource_id: str
    script_file: str | None
    dependency_resource_id: str | None
    dependency_group: str
    dependency_index: int


class StoryboardImageUnavailable(ValueError):
    """The storyboard required for video input is unavailable."""


class StoryboardImageBindingRequired(StoryboardImageUnavailable):
    """A manifest-active project cannot infer a formal storyboard by filename."""


class EndFrameImageUnavailable(ValueError):
    """The optional end-frame binding is invalid or its snapshot is unavailable."""


@dataclass(frozen=True, slots=True)
class StoryboardFrameSource:
    """A request frame and the durable source from which it is obtained."""

    path: Path
    resource_id: str
    kind: str
    keyframe_index: int | None = None


@dataclass(frozen=True, slots=True)
class StoryboardVideoSlots:
    """Ordered, non-overlapping video slots with stable source K identities."""

    start: StoryboardFrameSource
    references: tuple[StoryboardFrameSource, ...]
    end: StoryboardFrameSource | None
    cell_count: int
    continues_previous: bool
    #: 参考图模式：所有帧都按 reference_image 按序下发，不发首/尾帧。
    reference_only: bool = False

    @property
    def frames(self) -> tuple[tuple[str, StoryboardFrameSource], ...]:
        if self.reference_only:
            frames: list[tuple[str, StoryboardFrameSource]] = []
            # 参考图模式只送分镜格；首帧来自上一镜视频时那是一条视频，不能当参考图。
            if self.start.kind != "video_last_frame":
                frames.append(("reference_image", self.start))
            frames.extend(("reference_image", frame) for frame in self.references)
            return tuple(frames)
        return (
            ("start_image", self.start),
            *(("reference_image", frame) for frame in self.references),
            *((("end_image", self.end),) if self.end is not None else ()),
        )


def apply_storyboard_frame_mode(
    slots: StoryboardVideoSlots,
    mode: str,
) -> StoryboardVideoSlots:
    """按本次帧模式收缩槽位。

    ``first_last``：只留首帧与尾帧，K1/K2 等中间参考图全部丢掉——供应商不接受
    「参考图 + 首尾帧」混用时，这是保证起止承接的模式。
    ``reference``：只把当前分镜的完整 K0…Kn 按参考图下发，不混入上一镜末帧，也不再声明首/尾帧。
    """

    if mode == "reference":
        references = list(slots.references)
        if slots.end is not None:
            references.append(slots.end)
        if not slots.continues_previous:
            references.insert(0, slots.start)
        if not references:
            raise StoryboardImageUnavailable("reference mode requires current storyboard frames")
        return replace(
            slots,
            start=references[0],
            references=tuple(references[1:]),
            end=None,
            reference_only=True,
        )
    return replace(slots, references=(), reference_only=False)


def resolve_storyboard_video_slots(
    *,
    project_path: Path,
    items: Sequence[Any],
    id_field: str,
    resource_id: str,
) -> StoryboardVideoSlots:
    """Project the complete episode sequence without extracting or writing media."""
    resolved = find_storyboard_item(items, id_field, resource_id)
    if resolved is None:
        raise KeyError(f"scene/segment not found: {resource_id}")
    item, _ = resolved
    cells = resolve_storyboard_video_cells(project_path=project_path, resource_id=resource_id, item=item)
    previous = resolve_previous_storyboard_source(project_path, items, id_field, resource_id)
    if previous is not None:
        start = previous
    else:
        opening, _ = resolve_storyboard_video_inputs(project_path=project_path, resource_id=resource_id, item=item)
        start = StoryboardFrameSource(
            opening, resource_id, "grid_cell" if cells else "storyboard", 0 if cells else None
        )
    end = (
        StoryboardFrameSource(cells[-1], resource_id, "grid_cell", len(cells) - 1)
        if cells and (previous is not None or len(cells) > 1)
        else None
    )
    indices = range(0 if previous else 1, max(0, len(cells) - 1))
    return StoryboardVideoSlots(
        start=start,
        references=tuple(StoryboardFrameSource(cells[index], resource_id, "grid_cell", index) for index in indices),
        end=end,
        cell_count=len(cells),
        continues_previous=previous is not None,
    )


def get_storyboard_items(script: dict) -> tuple[list[Any], str, str | None, str, str]:
    """返回 旁白/解说、剧情演绎与广告/短片剧本的分镜列表 + 各引用字段名。

    ``video_units`` 骨架没有 storyboard 一说（视频按 unit 直出，见
    ``server/media_tools/videos.py`` 的参考生视频分支），这里硬返回空列表是
    「该骨架下不存在 storyboard 任务」的明示，调用方据此跳过。判别只看剧本实际骨架、不看项目
    生成模式：本函数是查看 / 编辑 / 生成共用的结构访问器，对存量失配剧本也要如实回答；生成分派按
    项目生成模式在各生成入口做，失配由 ``lib.script.script_skeleton.ensure_route_skeleton`` 显式拒绝。
    该分支的 ``char_field`` 取 ``SKELETONS`` 声明的缺位（``None``）——``video_units`` 无逐条
    角色名单（角色以 ``references`` 条目形态存在），不返回假字段名让调用方 ``get()`` 静默取空。

    分镜族骨架委托给 ``lib.script.script_editor.resolve_items``——与写盘咽喉
    / 编辑核心 / 元数据重算共用同一判别（``narration→segments``、``drama→scenes``、
    以及 narration 数据落 scenes 键的历史兼容）。``char_field`` 改查 ``SKELETONS`` 单一
    真相源（``.get(kind, ...)`` 静默兜底删除），第五种骨架出现时未登记即随查表报错。
    ``segments`` / ``scenes`` 键存在但值非 list（如 ``null``）时 ``resolve_items`` 抛
    ``ScriptEditError``——读取侧的调用方（``cost_estimation`` / 路由 / enqueue 工具）应让
    异常上冒，避免脏数据被静默吞成 ``TypeError: 'NoneType' is not iterable``。
    """
    items, id_field, kind = resolve_items(script)
    # 角色引用字段名查 SKELETONS 单一真相源（video_units→None 强制显式决策）；id_field 由
    # resolve_items 按同一 kind 一并给出，各分支共用。
    char_field = SKELETONS[kind].chars_field
    if kind == "video_units":
        return ([], id_field, char_field, "scenes", "props")
    return (items, id_field, char_field, "scenes", "props")


def find_storyboard_item(
    items: Sequence[Any],
    id_field: str,
    resource_id: str,
) -> tuple[Any, int] | None:
    for index, item in enumerate(items):
        # 条目来自磁盘剧本 JSON，只有外层 list 被校验过：非映射条目跳过而不是抛 AttributeError。
        if isinstance(item, dict) and str(item.get(id_field)) == str(resource_id):
            return item, index
    return None


def resolve_storyboard_image_ref(project_path: Path, storyboard_rel: object) -> Path | None:
    """校验 ``generated_assets.storyboard_image`` 字段值，返回解析后落在 ``storyboards/``
    内的绝对路径；字段未设置（``None``/``""``）返回 ``None``，由调用方按各自默认路径回退。

    该字段来自磁盘上的 project.json / 剧本 JSON，不可信任（归档导入、外部编辑、脏数据都能
    落值）：绝对路径 / ``..`` 会把项目外任意文件当作分镜图使用，须先做类型检查、显式拒绝
    绝对路径（含 Windows 无盘符根路径），再用 ``try_safe_join`` 解析并确认结果落在
    ``storyboards/`` 目录内。旧宫格项目 ``storyboard_image`` 指向非 canonical 文件名
    （``scene_{id}_first.png``）仍需正常解析，故只做目录归属校验，不与 canonical 路径逐一
    比对（这点与 ``end_frame_image`` 不同）。

    Raises:
        ValueError: 字段值非字符串、是绝对路径、越界、或解析结果不在 ``storyboards/`` 内。
    """
    if storyboard_rel in (None, ""):
        return None
    if not isinstance(storyboard_rel, str):
        raise ValueError(f"invalid storyboard image path: {storyboard_rel!r}")
    # `os.path.join` 遇到绝对路径会丢弃 base，若该绝对路径恰好落在项目 storyboards/ 内仍会
    # 通过越界检查，须在解析前显式挡掉。Windows 原生运行时无盘符的「根路径」（如 `\Users\...`）
    # 对 `Path.is_absolute()` 不算绝对，但 os.path.join 遇到它同样会丢弃 base（仅保留 base 的
    # 盘符），需按正斜杠归一化后再查是否以根分隔符开头。
    if Path(storyboard_rel).is_absolute() or storyboard_rel.replace("\\", "/").startswith("/"):
        raise ValueError(f"invalid storyboard image path: {storyboard_rel!r}")
    storyboards_root = safe_join(project_path, "storyboards", allow_base=True)
    storyboard_file = try_safe_join(project_path, storyboard_rel)
    if storyboard_file is None:
        raise ValueError(f"invalid storyboard image path: {storyboard_rel!r}")
    try:
        storyboard_file.relative_to(storyboards_root)
    except ValueError:
        # 与越界/脏数据分开措辞：这一支是「项目内但不在 storyboards/」，唯一能自然落进来的
        # 是外部编辑过的剧本，运维需要从失败原因直接看出是目录归属而非路径越界。
        raise ValueError(f"storyboard image path must stay under storyboards/: {storyboard_rel!r}") from None
    return storyboard_file


def resolve_storyboard_video_reference_images(
    *,
    project_path: Path,
    resource_id: str,
    item: dict[str, object],
) -> tuple[Path, ...]:
    """Resolve the ordered storyboard cells in stable ``K0``, ``K1`` order.

    The caller assigns the cells to the three video request slots. This helper
    intentionally returns the complete source sequence so admission and input
    claiming can validate the same registered storyboard artifact.
    """
    assets = get_generated_assets(item)
    grid = assets.get("shot_grid")
    if isinstance(grid, dict) and isinstance(grid.get("cell_paths"), list):
        cell_paths = grid.get("cell_paths")
        if not isinstance(cell_paths, list) or not cell_paths:
            raise StoryboardImageBindingRequired(f"shot grid cell bindings missing: {resource_id}")
        resolved: list[Path] = []
        for raw_path in cell_paths:
            path = resolve_storyboard_image_ref(project_path, raw_path)
            if path is None:
                raise StoryboardImageBindingRequired(f"shot grid cell binding missing: {resource_id}")
            if not path.is_file():
                raise StoryboardImageUnavailable(f"storyboard not found: {path.name}")
            resolved.append(path)
        return tuple(resolved)

    storyboard_rel = assets.get("storyboard_image")
    storyboard_file = resolve_storyboard_image_ref(project_path, storyboard_rel)
    if storyboard_file is None:
        raise StoryboardImageBindingRequired(f"storyboard binding missing: {resource_id}")
    if not storyboard_file.is_file():
        raise StoryboardImageUnavailable(f"storyboard not found: {storyboard_file.name}")
    return (storyboard_file,)


def resolve_storyboard_video_inputs(
    *,
    project_path: Path,
    resource_id: str,
    item: dict[str, object],
) -> tuple[Path, Path | None]:
    """Resolve the current shot's fallback opening image and final image cell."""
    cells = resolve_storyboard_video_cells(project_path=project_path, resource_id=resource_id, item=item)
    if cells:
        start = cells[0]
        end = cells[-1] if len(cells) > 1 else None
        return start, end

    assets = get_generated_assets(item)
    storyboard_rel = assets.get("storyboard_image")
    storyboard_file = resolve_storyboard_image_ref(project_path, storyboard_rel)
    if storyboard_file is None:
        raise StoryboardImageBindingRequired(f"storyboard binding missing: {resource_id}")
    if not storyboard_file.is_file():
        raise StoryboardImageUnavailable(f"storyboard not found: {storyboard_file.name}")
    return storyboard_file, None


def resolve_storyboard_video_cells(
    *,
    project_path: Path,
    resource_id: str,
    item: dict[str, object],
) -> tuple[Path, ...]:
    """Resolve confirmed grid cells in their temporal ``K0`` … ``Kn`` order."""
    assets = get_generated_assets(item)
    grid = assets.get("shot_grid")
    if not isinstance(grid, dict) or not isinstance(grid.get("cell_paths"), list):
        return ()
    raw_cells = grid["cell_paths"]
    if not raw_cells:
        raise StoryboardImageBindingRequired(f"shot grid cell bindings missing: {resource_id}")
    resolved: list[Path] = []
    for raw_path in raw_cells:
        path = resolve_storyboard_image_ref(project_path, raw_path)
        if path is None:
            raise StoryboardImageBindingRequired(f"shot grid cell binding missing: {resource_id}")
        if not path.is_file():
            raise StoryboardImageUnavailable(f"storyboard not found: {path.name}")
        resolved.append(path)
    return tuple(resolved)


def resolve_previous_storyboard_path(
    project_path: Path,
    items: Sequence[Any],
    id_field: str,
    resource_id: str,
) -> Path | None:
    """Return the durable source of the previous shot's continuity frame."""
    source = resolve_previous_storyboard_source(project_path, items, id_field, resource_id)
    return source.path if source is not None else None


def resolve_previous_storyboard_source(
    project_path: Path,
    items: Sequence[Any],
    id_field: str,
    resource_id: str,
) -> StoryboardFrameSource | None:
    """Prefer the selected video, then the user snapshot, then the last grid cell."""
    resolved = find_storyboard_item(items, id_field, resource_id)
    if resolved is None:
        raise KeyError(f"scene/segment not found: {resource_id}")

    _, index = resolved
    if index == 0:
        return None
    previous_item = items[index - 1]
    if not isinstance(previous_item, dict):
        return None
    previous_id = str(previous_item.get(id_field) or "").strip()
    if not previous_id:
        return None

    previous_assets = get_generated_assets(previous_item)
    video_rel = previous_assets.get("video_clip")
    if isinstance(video_rel, str) and video_rel:
        video_path = try_safe_join(project_path, video_rel)
        video_root = safe_join(project_path, "videos", allow_base=True)
        if video_path is None or not video_path.is_relative_to(video_root):
            raise ValueError("continuation video must stay under videos/")
        if video_path.is_file():
            return StoryboardFrameSource(video_path, previous_id, "video_last_frame")
    end_rel = previous_item.get("end_frame_image")
    if end_rel not in (None, ""):
        if not isinstance(end_rel, str):
            raise EndFrameImageUnavailable(f"invalid end frame snapshot path: {end_rel!r}")
        normalized = end_rel.strip().replace("\\", "/")
        candidate = normalized if "/" in normalized else f"{END_FRAME_RESOURCE_TYPE}/{normalized}"
        expected_rel = resource_relative_path(END_FRAME_RESOURCE_TYPE, previous_id)
        raw_expected = project_path / expected_rel
        if (
            candidate != expected_rel
            or raw_expected.is_symlink()
            or raw_expected.is_junction()
            or raw_expected.parent.is_symlink()
            or raw_expected.parent.is_junction()
        ):
            raise EndFrameImageUnavailable(f"invalid end frame snapshot path: {end_rel!r}")
        end_path = try_safe_join(project_path, candidate)
        expected = safe_join(project_path, expected_rel)
        if end_path is not None and end_path == expected and end_path.is_file():
            return StoryboardFrameSource(end_path, previous_id, "end_frame_snapshot")

    previous_grid = previous_assets.get("shot_grid")
    previous_cells = previous_grid.get("cell_paths") if isinstance(previous_grid, dict) else None
    if isinstance(previous_cells, list) and previous_cells:
        last = resolve_storyboard_image_ref(project_path, previous_cells[-1])
        if last is not None and last.is_file():
            return StoryboardFrameSource(last, previous_id, "grid_cell", len(previous_cells) - 1)
    return None


def group_scenes_by_segment_break(items: list[dict], id_field: str) -> list[list[dict]]:
    """Groups consecutive scene dicts, breaking at segment_break=True.

    Args:
        items: List of scene/segment dicts.
        id_field: Key in each dict for the item ID (unused but kept for API consistency).

    Returns:
        List of groups, each a list of consecutive scene dicts.
    """
    groups: list[list[dict]] = []
    current: list[dict] = []
    for item in items:
        if item.get("segment_break", False) and current:
            groups.append(current)
            current = []
        current.append(item)
    if current:
        groups.append(current)
    return groups


def build_storyboard_dependency_plan(
    items: Sequence[dict],
    id_field: str,
    selected_ids: Iterable[str],
    script_file: str | None,
) -> list[StoryboardTaskPlan]:
    selected_set = {str(item_id) for item_id in selected_ids}
    if not selected_set:
        return []

    plans: list[StoryboardTaskPlan] = []
    group_counter = 0
    current_group = ""
    current_group_index = 0

    for index, item in enumerate(items):
        resource_id = str(item.get(id_field) or "").strip()
        if not resource_id or resource_id not in selected_set:
            continue

        previous_resource_id: str | None = None
        if index > 0:
            previous_resource_id = str(items[index - 1].get(id_field) or "").strip() or None

        starts_new_group = (
            bool(item.get("segment_break")) or not previous_resource_id or previous_resource_id not in selected_set
        )

        if starts_new_group:
            group_counter += 1
            current_group = f"{script_file or 'storyboard'}:group:{group_counter}"
            current_group_index = 0
            dependency_resource_id = None
        else:
            current_group_index += 1
            dependency_resource_id = previous_resource_id

        plans.append(
            StoryboardTaskPlan(
                resource_id=resource_id,
                script_file=script_file,
                dependency_resource_id=dependency_resource_id,
                dependency_group=current_group,
                dependency_index=current_group_index,
            )
        )

    return plans
