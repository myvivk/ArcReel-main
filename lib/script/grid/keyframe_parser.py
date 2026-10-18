"""分镜提示词 scene 的镜头序列解析、K 引用边界与视频提示词分段。

真相源只有一处：``image_prompt.scene``（ADR 0098 D-A）。宫格出图与视频提示词编写都从这里
现解析，不再有第二份可编辑的逐格文本（D-1 / D-3）。

scene 形态：可选导语 + 镜头序列，``【镜头N｜景别】正文``，一格一镜；整集批量与单分镜同一
个解析出口。

容错口径（0098 D-A）：镜头序号不连续按出现序重排、重复序号留首、景别可缺省；scene 里没有
任何镜头标记时抛 ``ValueError``（0098 D-F：旧单帧格式全部废弃，不兜底）。

本模块另外承载两个纯函数族，供视频提示词使用：

- :func:`keyframe_reference_problems`：正文里的 ``K{n}`` 不得越界（D-17）；
- :func:`split_action_segments` / :func:`join_action_segments`：``0-2秒：…`` 的分段与回写
  （11.8），规范与编辑器同一只手，不会出现"规范要求分段、界面却是一整块"。
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

#: 正文里的 K 引用：``K0`` / ``K12``；小写 ``k4`` 同样算引用（写错的大小写不该绕过边界检查）。
_KEYFRAME_REF = re.compile(r"[Kk](\d+)")

#: 视频提示词的时间段标记：``0-2秒：``，兼容半角冒号、空格与小数秒。
_SEGMENT_MARK = re.compile(r"^[ \t]*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*秒\s*[：:][ \t]*", re.MULTILINE)


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


@dataclass(frozen=True, slots=True)
class ActionSegment:
    """视频提示词里的一段：时间段（秒）与该段写的动作。"""

    start: float
    end: float
    text: str


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
    空列表（保存允许空提示词，出图守卫据此拒绝）。
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

    :func:`parse_grid_prompt_cells` 的逆操作：AI 产出的结构化帧按它规整后回填弹窗输入框，
    序号只数真正写出的格、从 1 起，正文为空的格丢弃——与解析口径一致，同一个序列写出的
    文本因此唯一（PUT 的幂等判定逐字比它）。
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
    """``image_prompt.scene`` 的指纹：SHA-256 前 12 位。"""

    return hashlib.sha256(scene_text.encode("utf-8")).hexdigest()[:SCENE_REVISION_LENGTH]


def keyframe_reference_problems(text: str, cell_count: int) -> list[str]:
    """正文里越界的 K 引用，按出现序去重返回（``["K4", "K9"]``）。

    ``K0`` 合法，``K{cell_count - 1}`` 是最后一格；``n >= cell_count`` 的那一帧并不存在，
    写进正文只会变成"声明了不存在的参考图"（D-17）。越界即拒，不给\"忽略这一条\"的降级。
    """

    if cell_count <= 0:
        return []
    seen: set[str] = set()
    problems: list[str] = []
    for match in _KEYFRAME_REF.finditer(text):
        index = int(match.group(1))
        if index < cell_count:
            continue
        token = f"K{index}"
        if token in seen:
            continue
        seen.add(token)
        problems.append(token)
    return problems


def _format_seconds(value: float) -> str:
    """时间段里的秒数：整秒不写小数点，非整秒最多两位。"""

    rounded = round(value, 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def _segment_windows(duration_seconds: float, count: int) -> list[tuple[float, float]]:
    """把总时长均分给各段：4 格 / 8 秒 → 每段 2 秒，末段收口到总时长。"""

    if count <= 0:
        return []
    step = duration_seconds / count
    return [
        (round(index * step, 2), round(duration_seconds if index == count - 1 else (index + 1) * step, 2))
        for index in range(count)
    ]


def split_action_segments(action: str, duration_seconds: float, cell_count: int) -> list[ActionSegment] | None:
    """把 ``0-2秒：…`` 形态的正文切成逐段；切不出来时返回 ``None``（编辑器退化为单框）。

    - 段数按当前宫格格数对齐：多出来的段并进末段（不静默丢字），不足的段留空；
    - 时间段按 :func:`_segment_windows` 均分重排，正文不动（改时长不丢内容）；
    - 没有任何时间段标记时返回 ``None``——存量条目不强转、不清空（11.8 退化）。
    """

    if cell_count <= 0:
        return None
    text = action.strip()
    if not text:
        return None
    marks = list(_SEGMENT_MARK.finditer(text))
    if not marks:
        return None

    texts: list[str] = []
    for position, mark in enumerate(marks):
        end = marks[position + 1].start() if position + 1 < len(marks) else len(text)
        texts.append(_clean_lines(text[mark.end() : end].splitlines()))
    if len(texts) > cell_count:
        texts = [*texts[: cell_count - 1], "\n".join(part for part in texts[cell_count - 1 :] if part)]
    texts.extend([""] * (cell_count - len(texts)))
    windows = _segment_windows(duration_seconds, cell_count)
    return [ActionSegment(start=start, end=end, text=body) for (start, end), body in zip(windows, texts, strict=True)]


def join_action_segments(segments: Sequence[ActionSegment], duration_seconds: float) -> str:
    """把逐段正文写回 ``0-2秒：…`` 形态，时间段按当前时长重排。

    与 :func:`split_action_segments` 同一出口：来回一遍正文不漂移，只有时间段跟着时长走。
    """

    lines: list[str] = []
    for index, segment in enumerate(segments):
        start, end = (
            _segment_windows(duration_seconds, len(segments))[index]
            if duration_seconds > 0
            else (segment.start, segment.end)
        )
        body = segment.text.strip()
        lines.append(f"{_format_seconds(start)}-{_format_seconds(end)}秒：{body}")
    return "\n".join(lines)


__all__ = [
    "SCENE_REVISION_LENGTH",
    "ActionSegment",
    "SceneShots",
    "ShotFrame",
    "format_grid_prompt",
    "join_action_segments",
    "keyframe_reference_problems",
    "parse_grid_prompt_cells",
    "parse_scene_shots",
    "scene_revision_of",
    "shot_scene_text",
    "split_action_segments",
]
