"""分镜提示词 → 视频请求文本的确定性投影。

分镜提示词是「可选导语 + 【镜头N｜景别】序列」，正文可以自带 ``0-3秒：`` 时间段
（11.8 的编写口径）。视频请求需要按目标模型规范（``lib.prompts.model_specs``）组织
正文时，先把镜头序列投影成：

- ``action``：``0-3秒：`` 时间段正文；已写时间码按原样保留，缺省时按 ``duration_seconds``
  分配；
- ``shot_cameras``：与分段一一对应的运镜枚举（方向不明时为 ``None``）；
- ``ambiance_audio``：各镜 ``声音：`` 子句按镜头序拼成的声景段；
- ``camera_motion``：留空，避免与逐镜运镜冲突。

解析失败（旧单帧格式、空正文）时返回原始字符串，由调用方按通用口径渲染。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from lib.prompts.model_specs.spec import CAMERA_BRACKETS
from lib.script.grid.keyframe_parser import parse_scene_shots, shot_scene_text

#: 镜头首句里的中文运镜 → ``CameraMotion`` 枚举。长词在前，避免「变焦推」被「推」抢先。
_CAMERA_MOTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("左摇", "Pan Left"),
    ("右摇", "Pan Right"),
    ("左移", "Truck Left"),
    ("右移", "Truck Right"),
    ("变焦推", "Zoom In"),
    ("变焦拉", "Zoom Out"),
    ("固定", "Static"),
    ("静止", "Static"),
    ("环绕", "Orbit"),
    ("跟", "Tracking Shot"),
    ("推", "Push In"),
    ("拉", "Pull Out"),
    ("升", "Pedestal Up"),
    ("降", "Pedestal Down"),
    ("晃", "Shake"),
)

#: 镜头正文开头的时间段标记：``0-3秒：``，兼容全角/半角冒号与小数秒；H3 允许前置方括号运镜。
_LEADING_SPAN = re.compile(r"^\s*(?:\[[^\]]+\]\s*)*(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)\s*秒\s*[：:]\s*")

#: 镜头正文里的声音子句：``；声音：…`` / 行首 ``声音：…`` / H3 的 ``环境声：…``。
_SOUND_SPLIT = re.compile(r"(?:^|[；;\n])\s*(?:声音|环境声)\s*[：:]\s*")


def _camera_motion(text: str) -> str | None:
    """镜头正文首句里的运镜；方向不明（只有「摇」「移」）时返回 ``None``。"""

    head = text.split("。", 1)[0]
    for motion, bracket in CAMERA_BRACKETS.items():
        if bracket in head:
            return motion
    for marker, motion in _CAMERA_MOTION_PATTERNS:
        if marker in head:
            return motion
    return None


def _format_seconds(value: float) -> str:
    """时间段里的秒数：整秒不写小数点，非整秒最多两位。"""

    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _split_leading_span(text: str) -> tuple[tuple[float, float] | None, str]:
    """摘掉正文开头的时间段标记，返回 ``((start, end), 正文)``；没有标记时 span 为 ``None``。"""

    match = _LEADING_SPAN.match(text)
    if match is None:
        return None, text.strip()
    return (float(match.group(1)), float(match.group(2))), text[match.end() :].strip()


def _split_sound(text: str) -> tuple[str, str]:
    """把镜头正文拆成「画面」与「声音」两半：``声音：`` 之后的内容归声景段。"""

    match = _SOUND_SPLIT.search(text)
    if match is None:
        return text.strip(), ""
    visual = text[: match.start()].strip(" ；;，,")
    return visual, text[match.end() :].strip()


def _segment_seconds(texts: list[str], duration_seconds: object) -> list[tuple[int, int]]:
    """按正文长度把总时长分到各镜：每镜至少 1 秒，总和等于总时长。"""

    count = len(texts)
    try:
        total = int(duration_seconds) if isinstance(duration_seconds, (int, float, str)) else 0
    except (TypeError, ValueError):
        total = 0
    total = max(count, total)
    weights = [max(1, len(text.strip())) for text in texts]
    weight_sum = sum(weights)
    seconds = [max(1, int(total * weight / weight_sum)) for weight in weights]
    difference = total - sum(seconds)
    index = 0
    while difference > 0:
        seconds[index % count] += 1
        difference -= 1
        index += 1
    while difference < 0:
        largest = max(range(count), key=lambda position: seconds[position])
        if seconds[largest] <= 1:
            break
        seconds[largest] -= 1
        difference += 1
    spans: list[tuple[int, int]] = []
    start = 0
    for value in seconds:
        spans.append((start, start + value))
        start += value
    return spans


def project_storyboard_prompt(
    item: Mapping[str, Any],
    *,
    duration_seconds: object = None,
) -> dict[str, Any] | str:
    """把分镜提示词投影成视频请求对象；无法解析时返回原文。"""

    scene = shot_scene_text(item)
    if not scene.strip():
        raise ValueError("分镜缺少 image_prompt（分镜提示词）")
    try:
        parsed = parse_scene_shots(scene)
    except ValueError:
        return scene
    bodies = [_split_leading_span(shot.text) for shot in parsed.shots]
    camera_sources = [shot.text for shot in parsed.shots]
    parts = [_split_sound(body) for _span, body in bodies]
    # fea_1026 写法用「画面：」标记画面层；投影成 action 时去掉标记本身，只留正文。
    visuals = [
        re.sub(r"^\s*(?:\[[^\]]+\]\s*)+", "", visual.replace("画面：", "").replace("画面:", "")).strip()
        for visual, _sound in parts
    ]
    if not visuals or not all(visuals):
        return scene
    authored = [span for span, _body in bodies]
    # 作者已经写了逐镜时间码时以手写为准：改写时长不应把「6-9 秒」重排到别的时刻。
    if all(span is not None for span in authored):
        spans: list[tuple[float, float]] = [(span[0], span[1]) for span in authored if span is not None]
    else:
        spans = [(float(start), float(end)) for start, end in _segment_seconds(visuals, duration_seconds)]
    action = "\n".join(
        f"{_format_seconds(start)}-{_format_seconds(end)}秒：{text}"
        for text, (start, end) in zip(visuals, spans, strict=True)
    )
    # 各镜的「声音：」子句按镜头序拼成声景段；H3 等方言把它渲染成 overall_soundscape。
    ambiance = "；".join(sound.strip().rstrip("。；;，,") for _visual, sound in parts if sound.strip())
    return {
        "action": action,
        "camera_motion": "",
        "shot_cameras": [_camera_motion(source) for source in camera_sources],
        "ambiance_audio": f"{ambiance}。" if ambiance else "",
    }


__all__ = ["project_storyboard_prompt"]
