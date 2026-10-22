"""单分镜参考素材宫格的提示词拼装（D-2 四段式）。

单分镜与整集批量共用这一个拼装出口，逐格文本也只有一个来源：``image_prompt.scene``
现解析（D-1 / D-3）。第 1 / 2 / 4 段由系统按 rows/cols、项目画风、参考图声明与 scene 导语
渲染——没有任何可编辑文本承载网格排版指令，改规格不会与正文打架（P3）。

比例分两个口径下发（D-6）：整图比例由 :func:`grid_aspect_ratio_for` 按 (rows, cols, 画布比例)
算出，单格比例再由整图比例与行列数推算；2×3 这类非方形档的单格不再被错当成视频画布比例。
"""

from __future__ import annotations

from collections.abc import Sequence

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


def resolve_grid_cells(frames: Sequence[ShotFrame], count: int) -> list[ShotFrame]:
    """把解析出的镜头序列对齐到宫格格数。

    叙事镜头多于画格时，按时间顺序分组覆盖整个段落；少则尾格延续最后一个镜头的动作。
    手写逐格提示词的数量校验由提交入口负责，此处只映射分镜段，不丢弃尾部事件。
    """

    if count < 1:
        raise ValueError("宫格格数必须为正整数")
    if len(frames) > count:
        resolved = []
        for index in range(count):
            group = frames[index * len(frames) // count : (index + 1) * len(frames) // count]
            resolved.append(
                ShotFrame(
                    number=index + 1,
                    shot_size=" / ".join(dict.fromkeys(frame.shot_size for frame in group if frame.shot_size)),
                    text="\n".join(frame.text for frame in group),
                )
            )
        return resolved
    resolved = list(frames)
    if resolved and len(resolved) < count:
        last = resolved[-1]
        resolved.extend(
            ShotFrame(number=index + 1, shot_size="延续", text=f"延续第{last.number}格的动作，{CONTINUATION_TAIL}")
            for index in range(len(resolved), count)
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
