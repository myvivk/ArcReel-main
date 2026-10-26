"""单分镜参考素材宫格的提示词拼装（D-2 四段式）。

单分镜与整集批量共用这一个拼装出口，逐格文本也只有一个来源：``image_prompt.scene``
现解析（D-1 / D-3）。第 1 / 2 / 4 段由系统按 rows/cols、项目画风、参考图声明与 scene 导语
渲染——没有任何可编辑文本承载网格排版指令，改规格不会与正文打架（P3）。

比例分两个口径下发（D-6）：整图比例由 :func:`grid_aspect_ratio_for` 按 (rows, cols, 画布比例)
算出，单格比例再由整图比例与行列数推算；2×3 这类非方形档的单格不再被错当成视频画布比例。
"""

from __future__ import annotations

from collections.abc import Sequence
from difflib import SequenceMatcher
from itertools import pairwise

from lib.prompts.prompt_style import normalize_style_value
from lib.prompts.prompt_templates.builtin import builtin_templates
from lib.prompts.reference_image_numbering import (
    ReferenceImageSlot,
    link_asset_mentions,
    reference_images_declaration,
    render_reference_mentions,
)
from lib.script.grid.keyframe_parser import ShotFrame
from lib.script.grid.layout import grid_aspect_ratio_for, panel_aspect_ratio_for
from lib.script.grid.shot_models import ShotGridCell

#: 镜头数不足格数时，尾格延续最后一个镜头的动作（ADR 0098 D2 的建议分支）。
CONTINUATION_TAIL = "直到自然结束，不新增事件、不切换场景。"


def _select_keyframes(frames: Sequence[ShotFrame], count: int) -> list[ShotFrame]:
    """镜头多于画格时按关键瞬间抽帧，绝不合并文本。

    首镜是开始画面、末镜是结束画面，二者必留；作者标了 ``关键帧`` 的镜头优先入选，
    剩余名额再从其余镜头里等距补齐。被抽掉的镜头仍留在分镜提示词里，由视频模型在帧间补——
    把两镜的话拼进同一格，出图必然画成两帧的混合体（ADR 0098「一格一瞬间」）。
    """

    if count <= 1:
        return [frames[0]]
    picked: dict[int, ShotFrame] = {0: frames[0], len(frames) - 1: frames[-1]}
    for index in (i for i, frame in enumerate(frames) if frame.keyframe and 0 < i < len(frames) - 1):
        if len(picked) >= count:
            break
        picked[index] = frames[index]
    if len(picked) < count:
        rest = [i for i in range(len(frames)) if i not in picked]
        need = count - len(picked)
        if rest:
            step = len(rest) / need
            for slot in range(need):
                index = rest[min(int(slot * step), len(rest) - 1)]
                picked.setdefault(index, frames[index])
    ordered = [picked[index] for index in sorted(picked)][:count]
    return [
        ShotFrame(number=index + 1, shot_size=frame.shot_size, text=frame.text, keyframe=frame.keyframe)
        for index, frame in enumerate(ordered)
    ]


def cell_duplication_problem(cells: Sequence[ShotFrame]) -> str | None:
    """相邻两格必须是不同瞬间。文本高度相似或只是复述上一格，出图必然是两张一样的图。"""

    for left, right in pairwise(cells):
        if "动作收住，画面定格" in right.text or right.text.startswith("延续第"):
            return f"第{right.number}格只是复述上一格，没有新画面"
        ratio = SequenceMatcher(None, left.text, right.text, autojunk=False).ratio()
        if ratio >= 0.9:
            return f"第{left.number}格与第{right.number}格几乎是同一画面（相似度 {ratio:.2f}）"
    return None


def resolve_grid_cells(frames: Sequence[ShotFrame], count: int) -> list[ShotFrame]:
    """把逐格文本落到宫格的|帧位|上——**不是一镜一格**。

    设计口径（不要再滑回“镜头对齐格数”）：

    - 宫格是帧位：首帧 → 延续帧 ×N → 尾帧；格数与镜头数无必然关系；
    - 镜头多于格数：抽帧（首尾必留、关键帧优先、其余等距），不合并多镜文本；
    - 镜头少于格数：正式链路会先让模型按帧位写逐格文本（首帧取首镜、尾帧取末镜定格、
      中间是承接上一帧的延续画面）；本函数只做兜底对齐——沿用末镜画面并写清“定格/尚未收住”，
      绝不只丢一句「延续」指令。

    图片模型要的是可画的描述，指令等于没给内容。手写逐格提示词的数量校验由提交入口负责。
    """

    if count < 1:
        raise ValueError("宫格格数必须为正整数")
    if len(frames) > count:
        return _select_keyframes(frames, count)
    resolved = list(frames)
    if resolved and len(resolved) < count:
        last = resolved[-1]
        body = last.text.rstrip("。；; ")
        pads = count - len(resolved)
        for offset in range(pads):
            is_tail = offset == pads - 1
            detail = "动作收住，画面定格在此刻的状态" if is_tail else "同一构图内动作尚未收住"
            resolved.append(
                ShotFrame(
                    number=len(resolved) + 1,
                    shot_size="尾帧" if is_tail else "延续",
                    text=f"{body}；{detail}，{CONTINUATION_TAIL}",
                )
            )
    return resolved


def shot_grid_cells(frames: Sequence[ShotFrame], rows: int, cols: int) -> list[ShotGridCell]:
    """本次出图的逐格留证：正文进 ``prompt``、景别进 ``continuity_notes``（D-3）。

    入队与执行两处都经本函数落 ``cells``，与拼装出口读到的序列同一个；它只作留证，不参与
    再编辑，也不回写 scene。
    """

    count = rows * cols
    return [
        ShotGridCell(
            index=index,
            prompt=frame.text,
            time_ratio=index / (count - 1) if count > 1 else 0.0,
            continuity_notes=frame.shot_size,
        )
        for index, frame in enumerate(resolve_grid_cells(frames, count))
    ]


def _render_cell_text(text: str, references: Sequence[ReferenceImageSlot]) -> str:
    """逐格正文：资产裸名自动链接后，再把 ``@[名称]`` 换成参考图编号「图N」。"""

    return render_reference_mentions(link_asset_mentions(text, references), references)


def build_shot_grid_prompt(
    *,
    rows: int,
    cols: int,
    canvas_ratio: str,
    cells: Sequence[ShotFrame],
    style: str = "",
    style_description: str = "",
    scene_opening: str = "",
    references: Sequence[ReferenceImageSlot] = (),
) -> str:
    """渲染单分镜宫格的最终提示词（四段式，``storyboard/shot_grid`` 模版）。

    Args:
        rows / cols: 宫格规格，限定 2×2 / 2×3 / 3×3。
        canvas_ratio: 项目视频画布比例；整图比例由它与规格算出，不直接当整图比例下发。
        cells: 本次出图的逐格镜头序列，按出现序即阅读序（左→右、上→下）。
        style / style_description: 项目画风与画风描述。
        scene_opening: 分镜提示词 scene 的导语段，作为跨格基调进「风格与锚点」。
        references: 实际随请求发出的参考图，其位置即「图N」编号。

    Raises:
        ValueError: 逐格序列为空（一个镜头都解析不出来）。
    """

    count = rows * cols
    resolved = resolve_grid_cells(cells, count)
    if not resolved:
        raise ValueError("宫格镜头提示词没有可用的逐格镜头文本")
    grid_aspect_ratio = grid_aspect_ratio_for(rows, cols, canvas_ratio)
    return builtin_templates.render(
        "storyboard/shot_grid",
        reference_images=reference_images_declaration(references) or None,
        rows=rows,
        cols=cols,
        cell_count=count,
        grid_aspect_ratio=grid_aspect_ratio,
        panel_aspect_ratio=panel_aspect_ratio_for(grid_aspect_ratio, rows, cols),
        scene_opening=_render_cell_text(scene_opening, references) if scene_opening.strip() else None,
        cells=[
            {
                "number": frame.number,
                "shot_size": frame.shot_size,
                "text": _render_cell_text(frame.text, references),
            }
            for frame in resolved
        ],
        style=normalize_style_value(style),
        style_description=normalize_style_value(style_description),
    )


__all__ = [
    "CONTINUATION_TAIL",
    "build_shot_grid_prompt",
    "resolve_grid_cells",
    "shot_grid_cells",
]
