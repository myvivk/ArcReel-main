"""分镜提示词 scene 与宫格镜头提示词的镜头序列解析。

两种输入形态，产同一种 :class:`ShotFrame` 序列：

- **分镜提示词**（``image_prompt.scene``，ADR 0098 D-A）：可选导语 + 镜头序列，
  ``【镜头N｜景别】正文``，一格一镜；整集批量从这里现解析（ADR 0099 D-11）。
- **宫格镜头提示词**（``ShotGrid.prompt``，ADR 0099 D-2）：弹窗输入框的内容，
  ``第N格（景别）：正文`` 逐行排布；单分镜出图以它为真相源。

容错口径（0098 D-A）：镜头序号不连续按出现序重排、重复序号留首、景别可缺省；
scene 里没有任何镜头标记时抛 ``ValueError``（0098 D-F：旧单帧格式全部废弃，不兜底）。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: scene 指纹长度：只留前 12 位十六进制，够辨识「分镜提示词已变更」，不把整段摘要写进记录。
SCENE_REVISION_LENGTH = 12

#: scene 的镜头标记：``【镜头N｜景别】``，景别可缺省（``【镜头N】``）。分隔符兼容全角｜与半角 |。
_SHOT_MARK = re.compile(r"【\s*镜头\s*(\d+)\s*(?:[｜|]\s*([^】\n]*?)\s*)?】")

#: 宫格镜头提示词的逐格标记：``第N格（景别）：``，括号与冒号兼容全角/半角，景别可缺省。
_CELL_MARK = re.compile(r"^[ \t]*第\s*(\d+)\s*格\s*(?:[（(]\s*([^）)\n]*?)\s*[）)])?\s*[：:][ \t]*")


@dataclass(frozen=True, slots=True)
class ShotFrame:
    """一个镜头（= 宫格的一格）：序号按出现序从 1 起，景别可缺省，正文是自含的关键帧描述。"""

    number: int
    shot_size: str
    text: str


@dataclass(frozen=True, slots=True)
class SceneShots:
    """scene 的解析结果：导语段（可空）与镜头序列（至少一个）。"""

    opening: str
    shots: tuple[ShotFrame, ...]


def _clean_lines(parts: list[str]) -> str:
    """逐格正文的拼合：逐行去两端空白，丢掉空行，保留原有换行结构。"""

    return "\n".join(part.strip() for part in parts if part.strip())


def parse_scene_shots(scene: str) -> SceneShots:
    """解析分镜提示词 scene 为导语段与镜头序列。

    Raises:
        ValueError: scene 为空，或没有任何 ``【镜头N`` 标记（旧单帧格式，按 0098 D-F 拒绝）。
    """

    text = scene.strip()
    if not text:
        raise ValueError("image_prompt.scene must not be empty")
    marks = list(_SHOT_MARK.finditer(text))
    if not marks:
        raise ValueError("分镜提示词需按镜头序列重新编写：【镜头N｜景别】正文，一格一镜")
    opening = text[: marks[0].start()].strip()
    shots: list[ShotFrame] = []
    seen: set[int] = set()
    for position, mark in enumerate(marks):
        number = int(mark.group(1))
        if number in seen:
            continue
        end = marks[position + 1].start() if position + 1 < len(marks) else len(text)
        body = _clean_lines([text[mark.end() : end]])
        if not body:
            continue
        seen.add(number)
        shots.append(ShotFrame(number=len(shots) + 1, shot_size=(mark.group(2) or "").strip(), text=body))
    if not shots:
        raise ValueError("分镜提示词的镜头序列没有可用的画面描述")
    return SceneShots(opening=opening, shots=tuple(shots))


def parse_grid_prompt_cells(prompt: str) -> list[ShotFrame]:
    """解析宫格镜头提示词（``ShotGrid.prompt``）为逐格文本。

    标准形态是每行以 ``第N格（景别）：`` 开头（AI 产出与弹窗输入框的约定形态），标记行的
    续行并入该格；正文里没有任何 ``第N格`` 标记时按朴素形态处理——每个非空行即一格，
    景别缺省。序号不连续按出现序重排、重复序号留首、正文为空的格丢弃。prompt 为空时返回
    空列表（保存允许空提示词，出图守卫据此拒绝，见 ADR 0099 D-7）。
    """

    text = prompt.strip()
    if not text:
        return []
    marked: list[tuple[int, str, list[str]]] = []
    for line in text.splitlines():
        mark = _CELL_MARK.match(line)
        if mark is not None:
            marked.append((int(mark.group(1)), (mark.group(2) or "").strip(), [line[mark.end() :]]))
        elif marked and line.strip():
            marked[-1][2].append(line)
    if not marked:
        return [
            ShotFrame(number=number, shot_size="", text=body)
            for number, body in enumerate((line.strip() for line in text.splitlines() if line.strip()), start=1)
        ]
    frames: list[ShotFrame] = []
    seen: set[int] = set()
    for number, shot_size, parts in marked:
        if number in seen:
            continue
        body = _clean_lines(parts)
        if not body:
            continue
        seen.add(number)
        frames.append(ShotFrame(number=len(frames) + 1, shot_size=shot_size, text=body))
    return frames


def format_grid_prompt(frames: Sequence[ShotFrame]) -> str:
    """把镜头序列写回宫格镜头提示词的逐格形态：``第N格（景别）：正文``。

    :func:`parse_grid_prompt_cells` 的逆操作，两处共用同一个序列化出口：AI 产出的结构化帧
    按它规整后回填弹窗输入框，整集批量从 scene 现解析出的序列按它落 ``ShotGrid.prompt``
    （ADR 0099 D-2 / D-11）。序号只数真正写出的格、从 1 起，正文为空的格丢弃——与解析口径
    一致，同一个序列写出的文本因此是唯一的（PUT 的幂等判定逐字比它）。
    """

    lines: list[str] = []
    number = 0
    for frame in frames:
        text = frame.text.strip()
        if not text:
            continue
        number += 1
        shot_size = frame.shot_size.strip()
        prefix = f"第{number}格（{shot_size}）" if shot_size else f"第{number}格"
        lines.append(f"{prefix}：{text}")
    return "\n".join(lines)


def shot_scene_text(shot: Mapping[str, Any]) -> str:
    """分镜条目上的 ``image_prompt.scene`` 正文；结构化与纯文本两种形态同一出口。"""

    image_prompt = shot.get("image_prompt")
    if isinstance(image_prompt, Mapping):
        value = image_prompt.get("scene", "")
        return value if isinstance(value, str) else ""
    return image_prompt if isinstance(image_prompt, str) else ""


def scene_revision_of(scene_text: str) -> str:
    """``image_prompt.scene`` 的指纹：SHA-256 前 12 位（ADR 0099 D-3）。"""

    return hashlib.sha256(scene_text.encode("utf-8")).hexdigest()[:SCENE_REVISION_LENGTH]


__all__ = [
    "SCENE_REVISION_LENGTH",
    "SceneShots",
    "ShotFrame",
    "format_grid_prompt",
    "parse_grid_prompt_cells",
    "parse_scene_shots",
    "scene_revision_of",
    "shot_scene_text",
]
