"""
Prompt 工具函数

提供结构化 Prompt 到 YAML 格式的转换功能。
"""

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, get_args

import yaml

from lib.project.asset_types import (
    asset_name_comparison_key,
    normalize_asset_bucket,
    normalize_asset_name,
)
from lib.prompts.model_specs import VideoPromptSpec, camera_bracket, camera_phrase
from lib.prompts.prompt_style import normalize_style_value
from lib.prompts.prompt_templates.builtin import builtin_templates
from lib.prompts.reference_image_numbering import REFERENCE_IMAGES_KEY
from lib.references.text_parser import mention_spans
from lib.script.script_models import CameraMotion, ShotType

if TYPE_CHECKING:
    from lib.script.storyboard_sequence import StoryboardVideoSlots

logger = logging.getLogger(__name__)

# 提示词 YAML 的行宽上限。PyYAML 默认 80 列，超宽的纯量会在 ASCII 空格处折成多行——
# 英文 / 越南语提示词几乎每个值都超 80 列，折行会把原文塞进换行再喂给供应商。取一个任何
# 提示词都达不到的值，值内容与作者所写逐字一致。
_PROMPT_YAML_WIDTH = 1_000_000


def _dump_prompt_yaml(ordered: Mapping[str, Any]) -> str:
    """提示词各段共用的 YAML 序列化：键序保持插入序、Unicode 原样、块式布局、不折行。"""
    return yaml.dump(
        dict(ordered),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=_PROMPT_YAML_WIDTH,
    )


# 提示词正文里的排除项行：``Avoid:`` 顶行起，前缀后是本行的排除项措辞。识别按前缀而非整行——
# 纯文本回贴带回来的可能是任一历史版本的完整声明（如不含 Logo 的旧行），整行比对只认得出被写死
# 的那一版。冒号后只吃空格与制表符，换行不进前缀。
_AVOID_LINE = re.compile(r"^Avoid:[ \t]*(.*)$")

# 排除项拼接与追加到负向节点字面值时的分隔符。
_AVOID_SEPARATOR = "，"

# 正文里连续空行的塌缩上限：删掉 Avoid 行后留下的空行按此并成一个，与模版引擎对自身产生的连续
# 换行所做的塌缩同一口径。
_BLANK_RUN = re.compile(r"\n{3,}")

# 预设选项：真相源是 lib.script.script_models 的 Literal 词表，此处派生避免双写漂移
SHOT_TYPES: list[str] = list(get_args(ShotType))
CAMERA_MOTIONS: list[str] = list(get_args(CameraMotion))


def image_prompt_to_yaml(
    image_prompt: dict, project_style: str, *, reference_images: str = "", style_description: str = ""
) -> str:
    """
    将 imagePrompt 结构转换为 YAML 格式字符串

    Args:
        image_prompt: segment 中的 image_prompt 对象，结构为：
            {
                "scene": "场景描述",
                "composition": {
                    "shot_type": "镜头类型",
                    "lighting": "光线描述",
                    "ambiance": "氛围描述"
                }
            }
        project_style: 项目级风格设置（从 project.json 读取）
        reference_images: 参考图类型声明行的值（``lib.prompts.reference_image_numbering``），非空时作为
            ``Reference_Images`` 键插在风格块与 ``Scene`` 之间
        style_description: 项目风格描述

    Returns:
        YAML 格式字符串，键序 Style / Visual style / Reference_Images / Scene / Composition / Avoid
    """
    ordered: dict[str, Any] = {"Scene": image_prompt["scene"]}
    ordered["Composition"] = {
        "shot_type": image_prompt["composition"]["shot_type"],
        "lighting": image_prompt["composition"]["lighting"],
        "ambiance": image_prompt["composition"]["ambiance"],
    }
    return (
        builtin_templates.render(
            "storyboard/image",
            style=normalize_style_value(project_style),
            style_description=normalize_style_value(style_description),
            reference_images=yaml_section({REFERENCE_IMAGES_KEY: reference_images}) if reference_images else "",
            structured_body=_dump_prompt_yaml(ordered).rstrip(),
            text_body="",
        )
        + "\n"
    )


def require_storyboard_scene(image_prompt: Mapping[str, Any]) -> str:
    """Return the trimmed ``scene`` of a structured storyboard prompt, or raise ``ValueError``.

    Single criterion for what counts as a usable ``scene``: the enqueue guard
    (``TaskSpec.from_request``) and the rendering projection below both call it, so a prompt
    accepted at submission is exactly one the renderer can project.
    """

    scene = image_prompt.get("scene")
    if not isinstance(scene, str) or not scene.strip():
        raise ValueError("image_prompt.scene must be a non-empty string")
    return scene.strip()


def project_storyboard_image_prompt(image_prompt: object, project_style: str) -> tuple[str | dict[str, Any], str]:
    """Project one script prompt into the canonical semantics shared by rendering and currency."""

    if isinstance(image_prompt, str):
        prompt = image_prompt.strip()
        if not prompt:
            raise ValueError("image_prompt must not be empty")
        return prompt, project_style
    if not isinstance(image_prompt, Mapping):
        raise ValueError("image_prompt must be a string or object")
    scene = require_storyboard_scene(image_prompt)
    raw_composition = image_prompt.get("composition")
    composition = raw_composition if isinstance(raw_composition, Mapping) else {}
    return (
        {
            "scene": scene,
            "composition": {
                "shot_type": str(composition.get("shot_type") or "Medium Shot"),
                "lighting": str(composition.get("lighting") or ""),
                "ambiance": str(composition.get("ambiance") or ""),
            },
        },
        project_style,
    )


def video_prompt_to_yaml(video_prompt: dict, *, negative_style: str = "avoid") -> str:
    """
    将 videoPrompt 结构转换为 YAML 格式字符串

    Args:
        video_prompt: segment 中的 video_prompt 对象，结构为：
            {
                "action": "动作描述",
                "camera_motion": "摄像机运动",
                "ambiance_audio": "环境音效描述",
                "dialogue": [{"speaker": "角色名", "line": "台词"}],
                "voice_profiles": [{"Speaker": "角色名", "Voice_Style": "声音风格"}]
            }

    Returns:
        YAML 格式字符串，以 ``Avoid`` 反向约束键收尾
    """
    dialogue = [{"Speaker": d["speaker"], "Line": d["line"]} for d in video_prompt.get("dialogue", [])]
    voice_profiles = video_prompt.get("voice_profiles") or []

    ordered: dict[str, Any] = {}
    # Voice_Profiles 是集中声明段，须在顶部：调用方已按 dialogue speaker ∩ 非空 voice_style
    # 角色资产派生好列表，此处只负责按序注入，不做二次过滤。
    if voice_profiles:
        ordered["Voice_Profiles"] = voice_profiles
    ordered["Action"] = video_prompt["action"]
    ordered["Camera_Motion"] = video_prompt["camera_motion"]
    ordered["Ambiance_Audio"] = video_prompt.get("ambiance_audio", "")

    # 仅在有对话时添加 Dialogue 字段
    if dialogue:
        ordered["Dialogue"] = dialogue
    return builtin_templates.render(
        "storyboard/video", body=_dump_prompt_yaml(ordered).rstrip(), video_prompt_negative=negative_style
    ) + "\n"


def _render_shot_sequence_prompt(prompt: dict[str, Any]) -> str:
    shots = prompt.get("shot_sequence")
    if not isinstance(shots, list) or not shots:
        raise ValueError("prompt.shot_sequence must be a non-empty array")
    lines: list[str] = []
    for index, raw_shot in enumerate(shots, start=1):
        if not isinstance(raw_shot, Mapping):
            continue
        title = str(raw_shot.get("title") or f"镜头{index}").strip()
        lines.append(f"{title}：")
        fields = (
            ("时间线", "timeline"),
            ("景别", "shot_size"),
            ("焦段", "lens"),
            ("角度", "angle"),
            ("构图", "composition"),
            ("参考图", "references"),
            ("画面描述", "description"),
            ("运镜", "camera_motion"),
            ("声音/对白", "audio"),
        )
        for label, key in fields:
            value = raw_shot.get(key)
            if isinstance(value, list):
                value = ", ".join(str(entry) for entry in value if str(entry).strip())
            if value is not None and str(value).strip():
                lines.append(f"{label}：{str(value).strip()}")
        lines.append("")
    rendered = "\n".join(lines).strip()
    if not rendered:
        raise ValueError("prompt.shot_sequence must contain a valid shot")
    return rendered


def normalize_video_prompt(prompt: object, *, negative_style: str = "avoid") -> str:
    """Normalize the exact text sent to a video provider."""

    if isinstance(prompt, str):
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        body, _ = split_avoid_lines(prompt)
        return builtin_templates.render(
            "storyboard/video", body=body.rstrip(), video_prompt_negative=negative_style
        )
    if not isinstance(prompt, dict):
        raise ValueError("prompt must be a string or object")
    if "shot_sequence" in prompt:
        return builtin_templates.render(
            "storyboard/video",
            body=_render_shot_sequence_prompt(prompt).rstrip(),
            video_prompt_negative=negative_style,
        )
    if not is_structured_video_prompt(prompt):
        raise ValueError("prompt must be a string or include action/camera_motion")

    action_text = str(prompt.get("action", "")).strip()
    if not action_text:
        raise ValueError("prompt.action must not be empty")
    dialogue = prompt.get("dialogue", [])
    if dialogue is None:
        dialogue = []
    if not isinstance(dialogue, list):
        raise ValueError("prompt.dialogue must be an array")

    normalized_dialogue = []
    for item in dialogue:
        if not isinstance(item, dict):
            continue
        speaker = str(item.get("speaker", "") or "").strip()
        line = str(item.get("line", "") or "").strip()
        if speaker or line:
            normalized_dialogue.append({"speaker": speaker, "line": line})

    normalized_prompt: dict[str, Any] = {
        "action": action_text,
        "camera_motion": str(prompt.get("camera_motion", "") or "") or "Static",
        "ambiance_audio": str(prompt.get("ambiance_audio", "") or ""),
        "dialogue": normalized_dialogue,
        "voice_profiles": prompt.get("voice_profiles") or [],
    }
    return video_prompt_to_yaml(normalized_prompt, negative_style=negative_style).rstrip()


@dataclass(frozen=True, slots=True)
class VideoReferenceLabel:
    """随请求实际发出的一张参考图：正文标签与类型，编号（图N）即它在序列中的位置。"""

    label: str
    kind: Literal["character", "scene", "prop", "product", "keyframe", "frame", "extra"] = "extra"


_REFERENCE_KIND_DESCRIPTIONS: dict[str, str] = {
    "character": "角色参考图",
    "scene": "场景参考图",
    "prop": "道具参考图",
    "product": "商品参考图",
    "extra": "补充参考图",
}

_SEGMENT_HEAD = re.compile(r"(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)\s*秒\s*[：:]")


def _format_marker(seconds: float) -> str:
    total = max(0.0, float(seconds))
    minutes = int(total // 60)
    return f"{minutes:02d}:{total - minutes * 60:06.3f}"


def _action_segments(action: str) -> list[tuple[float, float, str]]:
    """把 ``0-3秒：…`` 正文拆成时间段；开头有非时间段文本即整体放弃（不猜）。"""

    matches = list(_SEGMENT_HEAD.finditer(action))
    if not matches or action[: matches[0].start()].strip():
        return []
    segments: list[tuple[float, float, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(action)
        body = action[match.end() : end].strip().strip("；;，,").strip()
        segments.append((float(match.group(1)), float(match.group(2)), body))
    return segments


def _label_connector(label: str) -> str:
    return ": " if label.isascii() else "："


def _is_english(language: str | None) -> bool:
    return bool(language) and language.strip().casefold().startswith(("en", "english"))


def _camera_sentence(motion: str, language: str | None) -> str:
    phrase = camera_phrase(motion, "English" if _is_english(language) else "中文")
    if not phrase:
        return ""
    if _is_english(language):
        return f"{phrase[:1].upper()}{phrase[1:]}."
    return f"{phrase}。"


def _spec_body(
    prompt: dict[str, Any] | str,
    spec: VideoPromptSpec,
    language: str | None,
    placements: Mapping[int, Sequence[str]] | None = None,
) -> str:
    """按 spec 的正文单位投影 ``action``（含运镜落点）；文本形态原样作正文。

    ``placements`` 是「段号 → 该段要内联的台词行」，只由 ``anchor="shot"`` 的方言传入；
    其余方言传 ``None``，行为与旧版逐字一致。
    """

    if isinstance(prompt, str):
        return prompt.strip()
    action = str(prompt.get("action") or "").strip()
    camera = str(prompt.get("camera_motion") or "").strip()

    if spec.body.unit == "seconds":
        body = action
        if camera and spec.camera.mode == "bracket":
            bracket = camera_bracket(camera)
            body = "\n".join(f"{bracket} {line}" for line in body.splitlines() if line.strip())
        elif camera and spec.camera.mode in {"inline", "natural"}:
            sentence = _camera_sentence(camera, language)
            if sentence:
                body = f"{body} {sentence}".strip()
        return body

    segments = _action_segments(action)
    if not segments:
        segments = [(0.0, 0.0, action)]
    blocks: list[str] = []
    for index, (start, _end, text) in enumerate(segments, start=1):
        header = spec.body.open.format(n=index) if spec.body.open else ""
        if spec.body.unit == "prose":
            block = text
        elif spec.body.unit == "shots_en":
            if index == 1 or spec.body.time != "marker":
                block = f"{header} {text}".strip()
            else:
                cut = "cut to " if _is_english(language) else ""
                block = f"{header} At {_format_marker(start)}, {cut}{text}"
        else:
            block = f"{header}{text}"
        inline = placements.get(index) if placements else None
        if inline:
            block = f"{block} {' '.join(inline)}"
        blocks.append(block)
    body = " ".join(blocks) if spec.body.unit == "prose" else "\n".join(blocks)

    if camera:
        if spec.camera.mode == "bracket":
            bracket = camera_bracket(camera)
            body = "\n".join(f"{bracket} {line}" for line in body.splitlines())
        elif spec.camera.mode in {"inline", "natural"}:
            sentence = _camera_sentence(camera, language)
            if sentence:
                body = f"{body} {sentence}"
    return body


def _spec_sound(prompt: dict[str, Any] | str, spec: VideoPromptSpec) -> str:
    if isinstance(prompt, str):
        return ""
    ambiance = str(prompt.get("ambiance_audio") or "").strip()
    if not ambiance:
        return ""
    return f"{spec.sound.label}{_label_connector(spec.sound.label)}{ambiance}"


def _speaker_picture_prefix(
    speaker: str, references: Sequence[VideoReferenceLabel] | None
) -> str:
    """说话人若在同一请求的参考图里，台词前挂 ``<Picture N>``；否则只留名字。"""

    if not speaker or not references:
        return ""
    for position, reference in enumerate(references, start=1):
        if reference.kind != "keyframe" and reference.label == speaker:
            return f"<Picture {position}>"
    return speaker


@dataclass(frozen=True, slots=True)
class _DialogueLine:
    """一条待落位的台词：渲染好的 `<d>` 文本 + 可选的段号 / 秒级锚点。"""

    speaker: str
    text: str
    at: float | None = None
    shot: int | None = None


def _as_seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    return seconds if seconds >= 0 else None


def _as_shot(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 1 else None


def _dialogue_lines(
    prompt: dict[str, Any] | str,
    language: str | None,
    references: Sequence[VideoReferenceLabel] | None,
) -> list[_DialogueLine]:
    """把 ``dialogue`` 渲染成 H3 台词行；保留 ``at`` / ``shot`` 锚点供内联使用。"""

    if isinstance(prompt, str):
        return []
    dialogue = prompt.get("dialogue")
    if not isinstance(dialogue, list):
        return []
    language_tag = "English" if _is_english(language) else "Chinese"
    lines: list[_DialogueLine] = []
    index = 0
    for entry in dialogue:
        if not isinstance(entry, Mapping):
            continue
        line = str(entry.get("line") or "").strip()
        if not line:
            continue
        speaker = str(entry.get("speaker") or "").strip()
        index += 1
        prefix = _speaker_picture_prefix(speaker, references)
        head = f"{prefix} " if prefix else ""
        lines.append(
            _DialogueLine(
                speaker=speaker,
                text=f"{head}(S{index}) says: <d>[{language_tag}] {line}</d>",
                at=_as_seconds(entry.get("at")),
                shot=_as_shot(entry.get("shot")),
            )
        )
    return lines


def _anchor_index(
    item: _DialogueLine,
    segments: Sequence[tuple[float, float, str]],
) -> int | None:
    """一条台词落在第几段：显式 ``shot`` → 显式 ``at`` → 说话人最后出现的段。

    没有锚点的老数据只能靠“说话人在正文里最后出现的那一段”兜底：台词通常在这句动作之后
    出口，取最后一段比取第一段更接近实际说话时刻；要精确对位请在 utterance 上补 ``at``。
    """

    count = len(segments)
    if item.shot is not None and 1 <= item.shot <= count:
        return item.shot
    if item.at is not None:
        for index, (start, end, _text) in enumerate(segments, start=1):
            if start <= item.at < end:
                return index
        # 时间码落在段外：取最近段，不丢台词。
        return min(range(1, count + 1), key=lambda i: abs(segments[i - 1][0] - item.at))
    if not item.speaker:
        return None
    mentions = [index for index, (_s, _e, text) in enumerate(segments, start=1) if item.speaker in text]
    return mentions[-1] if mentions else None


def _place_dialogue(
    lines: Sequence[_DialogueLine],
    segments: Sequence[tuple[float, float, str]],
) -> tuple[dict[int, list[str]], list[_DialogueLine]]:
    """把台词按锚点分到各分镜段；落不了位的（无锚点、说话人没出现在正文）留给兜底段落。"""

    placed: dict[int, list[str]] = {}
    pending: list[_DialogueLine] = []
    if not segments:
        return placed, list(lines)
    for item in lines:
        index = _anchor_index(item, segments)
        if index is None:
            pending.append(item)
            continue
        placed.setdefault(index, []).append(item.text)
    return placed, pending


def _spec_dialogue(
    prompt: dict[str, Any] | str,
    spec: VideoPromptSpec,
    language: str | None,
    references: Sequence[VideoReferenceLabel] | None = None,
) -> str:
    if isinstance(prompt, str):
        return ""
    if spec.dialogue.mode == "h3":
        # H3 只认原句位置的 <d>[语言] 原文</d>；台词另起一段或写成 YAML 都会被当成旁白文本。
        return "\n".join(item.text for item in _dialogue_lines(prompt, language, references))
    dialogue = prompt.get("dialogue")
    if not isinstance(dialogue, list):
        return ""
    entries = [
        (str(entry.get("speaker") or "").strip(), str(entry.get("line") or "").strip())
        for entry in dialogue
        if isinstance(entry, Mapping) and str(entry.get("line") or "").strip()
    ]
    if not entries:
        return ""
    if spec.dialogue.mode == "block":
        return yaml_section({"Dialogue": [{"Speaker": speaker, "Line": line} for speaker, line in entries]})
    lines: list[str] = []
    for speaker, line in entries:
        quoted = f"{spec.dialogue.open_quote}{line}{spec.dialogue.close_quote}"
        if _is_english(language):
            lines.append(f"{speaker} says: {quoted}")
        else:
            lines.append(f"{speaker}说：{quoted}")
    return "\n".join(lines)


def _spec_exclusion(spec: VideoPromptSpec) -> str:
    """配乐 / 排除项的那一行；``avoid`` 交给模版，这里只处理 section 与 sentence。"""

    if spec.music.mode == "section":
        return f"{spec.music.label}: {spec.music.empty_value}"
    if spec.music.mode == "sentence" and spec.music.text.strip():
        return spec.music.text.strip()
    return ""


def _video_reference_declaration(references: Sequence[VideoReferenceLabel]) -> str:
    """``Reference_Images`` 行：按类型分组编号，关键帧合并成区间并带上 K 号。"""

    if not references:
        return ""
    groups: list[tuple[str, list[int]]] = []
    keyframes: list[tuple[int, str]] = []
    for position, reference in enumerate(references, start=1):
        if reference.kind == "keyframe":
            keyframes.append((position, reference.label))
            continue
        description = _REFERENCE_KIND_DESCRIPTIONS.get(reference.kind, "补充参考图")
        for existing, positions in groups:
            if existing == description:
                positions.append(position)
                break
        else:
            groups.append((description, [position]))
    parts = [f"{'、'.join(f'图{position}' for position in positions)}为{description}" for description, positions in groups]
    if keyframes:
        positions = [position for position, _label in keyframes]
        labels = [label for _position, label in keyframes]
        span = f"图{positions[0]}" if len(positions) == 1 else f"图{positions[0]}–图{positions[-1]}"
        k_span = labels[0] if len(labels) == 1 else f"{labels[0]}–{labels[-1]}"
        parts.append(f"{span}为关键帧 {k_span}")
    return "Reference_Images: " + "；".join(parts) + "。"


def _figure_marker(position: int) -> str:
    """中文方言的参考图记号：``图N``。"""

    return f"图{position}"


def _picture_marker(position: int) -> str:
    """MiniMax H3 本地权重的参考图记号：``<Picture N>``（编号即实际连线顺序）。"""

    return f"<Picture {position}>"


def _apply_reference_numbers(
    text: str,
    references: Sequence[VideoReferenceLabel],
    *,
    marker: Callable[[int], str] = _figure_marker,
    keep_name: bool = True,
) -> str:
    """把 ``@名称`` / ``名称@[名称]`` 就地换成「名称图N」；名字只留一次，编号紧跟。

    旧式裸 ``@名称`` 会把后面连着的汉字一并吞进 mention（解析器按 CJK 连续段识别），
    这类 mention 按已登记标签做最长前缀匹配，只替换命中的名字，其余原样保留。
    """

    labels = [
        (reference.label, position)
        for position, reference in enumerate(references, start=1)
        if reference.kind != "keyframe"
    ]
    by_key = {asset_name_comparison_key(label): (label, position) for label, position in labels}
    ordered = sorted(labels, key=lambda item: -len(item[0]))
    spans = mention_spans(text)
    if not spans:
        return text
    parts: list[str] = []
    last = 0
    for start, end, name in spans:
        parts.append(text[last:start])
        consumed, rest, number = name, "", None
        hit = by_key.get(asset_name_comparison_key(name))
        if hit is not None:
            consumed, number = hit[0], hit[1]
        else:
            for label, position in ordered:
                if name.startswith(label):
                    consumed, rest, number = label, name[len(label) :], position
                    break
        prefix = text[last:start]
        if number is None:
            parts.append("" if prefix.rstrip().endswith(name) else name)
        elif prefix.rstrip().endswith(consumed):
            parts.append(f"{marker(number)}{rest}")
        elif keep_name:
            parts.append(f"{consumed}{marker(number)}{rest}")
        else:
            # <Picture N> 的指认已经写进 subject_definitions，正文里名字可以省掉：
            # 「萧炎<Picture 1>」在 H3 里读起来像两个主体，不如直接 <Picture 1>。
            parts.append(f"{marker(number)}{rest}")
        last = end
    parts.append(text[last:])
    return "".join(parts)


def _number_video_references(
    text: str,
    references: Sequence[VideoReferenceLabel],
    *,
    pictures: bool = False,
) -> str:
    if pictures:
        numbered = _apply_reference_numbers(
            text, references, marker=_picture_marker, keep_name=False
        )
        declaration = _picture_reference_declaration(references)
        if declaration and "subject_definitions:" not in numbered:
            numbered = f"{declaration}\n{numbered}"
        return numbered
    numbered = _apply_reference_numbers(text, references)
    declaration = _video_reference_declaration(references)
    if declaration and "Reference_Images:" not in numbered:
        numbered = f"{declaration}\n{numbered}"
    return numbered


def _picture_reference_declaration(references: Sequence[VideoReferenceLabel]) -> str:
    """H3 的参考图定义段：``<Picture N> is <名称>（角色参考图）``，编号即连线顺序。"""

    if not references:
        return ""
    lines: list[str] = []
    for position, reference in enumerate(references, start=1):
        if reference.kind == "keyframe":
            lines.append(f"<Picture {position}> is keyframe {reference.label}.")
            continue
        description = _REFERENCE_KIND_DESCRIPTIONS.get(reference.kind, "补充参考图")
        lines.append(f"<Picture {position}> is {reference.label} ({description}).")
    return "subject_definitions:\n" + "\n".join(lines)


def _prepare_video_prompt(
    prompt: object,
    item: Mapping[str, Any] | None,
    *,
    content_mode: str,
    voice_characters: dict[str, Any] | None,
) -> dict[str, Any] | str:
    """drama 发声声明合并后的提示词形态（结构或文本）。"""

    if isinstance(prompt, dict):
        prompt = strip_voice_profiles(prompt)
        if content_mode == "drama" and "shot_sequence" not in prompt:
            if isinstance(item, Mapping) and "utterances" in item:
                return build_drama_video_prompt(prompt, item.get("utterances"), characters=voice_characters)
            return build_drama_video_prompt_from_legacy_dialogue(prompt, characters=voice_characters)
        return prompt
    if isinstance(prompt, str) and content_mode == "drama":
        return _attach_drama_speech_text(prompt, item, characters=voice_characters)
    return prompt


def _render_spec_prompt(
    prompt: object,
    item: Mapping[str, Any] | None,
    *,
    content_mode: str,
    voice_characters: dict[str, Any] | None,
    slots: "StoryboardVideoSlots | None",
    asset_links: str,
    audio_speakers: tuple[str, ...],
    spec: VideoPromptSpec,
    references: Sequence[VideoReferenceLabel] | None,
    language: str | None,
) -> str:
    """按模型 spec 渲染最终提示词（非 generic 方言）。"""

    prepared = _prepare_video_prompt(prompt, item, content_mode=content_mode, voice_characters=voice_characters)
    placements: dict[int, list[str]] = {}
    has_dialogue = False
    if spec.dialogue.mode == "h3":
        lines = _dialogue_lines(prepared, language, references)
        has_dialogue = bool(lines)
        # anchor="shot" 的方言把台词内联进说这句话的分镜段；落不了位的仍走正文后的兜底段落。
        if spec.dialogue.anchor == "shot" and lines and isinstance(prepared, Mapping):
            segments = _action_segments(str(prepared.get("action") or "").strip())
            if segments:
                placements, lines = _place_dialogue(lines, segments)
        dialogue = "\n".join(item.text for item in lines)
    else:
        dialogue = _spec_dialogue(prepared, spec, language, references)
        has_dialogue = bool(dialogue)
    body = _spec_body(prepared, spec, language, placements=placements)
    sound = _spec_sound(prepared, spec)
    # H3 的台词属于正文的一部分，紧跟分镜段落之后；放到声景后面会被当成另一段描述。
    ordered = [body, dialogue, sound] if spec.dialogue.mode == "h3" else [body, sound, dialogue]
    sections = [*ordered, _spec_exclusion(spec)]
    # 无声镜头：台词段为空时补一句「谁都不出声」。H3 的 native 音轨在没有台词边界时
    # 会自己编人声；把「闭口、不出声」写进请求，至少不让模型面对一片空白。文本形态的
    # 正文无从判断有没有台词（台词可能已经写进正文），不追加，避免把有声镜头说成无声。
    if spec.silent_clause and isinstance(prepared, Mapping) and not has_dialogue and content_mode == "drama":
        sections.append(spec.silent_clause)
    rendered = "\n\n".join(section for section in sections if section)

    instruction = ""
    if slots is not None:
        instruction = _storyboard_slot_declaration(slots, references)
    elif isinstance(item, Mapping):
        assets = item.get("generated_assets")
        grid = assets.get("shot_grid") if isinstance(assets, Mapping) else None
        cells = grid.get("cell_paths") if isinstance(grid, Mapping) else None
        if isinstance(cells, list) and cells:
            last = len(cells) - 1
            parts = ["K0 is the opening frame of this shot, supplied as the first frame."]
            if last > 1:
                parts.append(f"The reference images are K1–K{last - 1} in their supplied order.")
            if last:
                parts.append(f"The final keyframe K{last} is supplied as the last frame.")
            instruction = " ".join(parts)
    if spec.reference.style == "pictures":
        audio = "\n".join(
            f"{speaker}: use the voice timbre of <Audio {index}>."
            for index, speaker in enumerate(audio_speakers, 1)
        )
    else:
        audio = "\n".join(
            f"{speaker}: use the voice in reference audio {index} (音频{index})."
            for index, speaker in enumerate(audio_speakers, 1)
        )
    # 资产描述块是中文 Markdown 链接，H3 不认；本地权重规范把它关掉，靠 <Picture N> 定义段指认。
    optional = (asset_links, audio, instruction) if spec.include_asset_links else (audio, instruction)
    for section in optional:
        if section and section not in rendered:
            rendered = f"{rendered.rstrip()}\n\n{section}"
    if references:
        return _number_video_references(
            rendered, references, pictures=spec.reference.style == "pictures"
        )
    return _strip_video_mentions(rendered)


_DIALOGUE_ONLY_AUDIO_CONSTRAINT = (
    "Audio: speak only the dialogue lines given above. Do not add narration, voice-over, "
    "extra speakers, or any unlisted human voice. Keep only diegetic sound effects explicitly described."
)


def _append_dialogue_only_audio_constraint(
    rendered: str,
    item: Mapping[str, Any] | None,
    content_mode: str,
) -> str:
    """给带角色台词的视频提示词补一条音轨约束。

    参考图生视频模型在提示词只写动作、没有显式台词边界时，容易自行补旁白/第二个人声；
    这条约束不保证模型服从，但把「只念列出的台词」写成请求的一部分，至少不留给它空白。
    """
    if content_mode != "drama" or not isinstance(item, Mapping):
        return rendered
    utterances = item.get("utterances")
    if not isinstance(utterances, list):
        return rendered
    has_dialogue = any(
        isinstance(entry, Mapping) and entry.get("kind") == "dialogue" for entry in utterances
    )
    if not has_dialogue or _DIALOGUE_ONLY_AUDIO_CONSTRAINT in rendered:
        return rendered
    return f"{rendered.rstrip()}\n\n{_DIALOGUE_ONLY_AUDIO_CONSTRAINT}"


def render_storyboard_video_prompt(
    prompt: object,
    item: Mapping[str, Any] | None = None,
    *,
    content_mode: str,
    voice_characters: dict[str, Any] | None,
    slots: "StoryboardVideoSlots | None" = None,
    asset_links: str = "",
    audio_speakers: tuple[str, ...] = (),
    spec: VideoPromptSpec | None = None,
    references: Sequence[VideoReferenceLabel] | None = None,
    language: str | None = None,
) -> str:
    """分镜生视频最终提示词文本的唯一出口。

    执行路径、批量准入、执行敏感摘要与预览接口共用本函数，四处产出逐字同构由此在结构上
    成立，而非各写一份靠约定对齐。``prompt`` 取条目当前的 ``video_prompt``（结构形态或文本
    形态），``item`` 供 drama 取分镜级 ``utterances``（``None`` 视同无 utterances 字段）。

    文本形态下条目正文即模版的正文槽位；drama 的发声序列仍由脚本规划的
    ``utterances`` 决定——正文不承载台词，台词与声音风格由本函数按同一门控追加到正文之后。

    ``spec`` 为模型提示词规范：``None`` 或 ``generic`` 时走现状逐字口径；其余按规范投影
    正文单位、运镜、声音、台词与排除项。``references`` 是本次请求实际发出的参考图序列
    （编号即其位置），非空时正文 ``@名称`` 渲染成「名称图N」并加 ``Reference_Images`` 行。
    """

    if spec is not None and not spec.is_generic:
        if isinstance(prompt, str) and spec.reference.style == "pictures":
            # 文本形态的正文已经按旧口径渲染过一遍（编号写死成「图N」），再套 H3 的
            # <Picture N> 只会把编号说错；退回逐字口径，保证入队快照与实发文本一致。
            spec = None
        else:
            rendered = _render_spec_prompt(
                prompt,
                item,
                content_mode=content_mode,
                voice_characters=voice_characters,
                slots=slots,
                asset_links=asset_links,
                audio_speakers=audio_speakers,
                spec=spec,
                references=references,
                language=language,
            )
            return normalize_video_prompt(
                _append_dialogue_only_audio_constraint(rendered, item, content_mode),
                negative_style=spec.negative_style,
            )

    prompt = _prepare_video_prompt(prompt, item, content_mode=content_mode, voice_characters=voice_characters)
    negative_style = spec.negative_style if spec is not None else "avoid"
    rendered = normalize_video_prompt(prompt, negative_style=negative_style)
    assets = item.get("generated_assets") if isinstance(item, Mapping) else None
    grid = assets.get("shot_grid") if isinstance(assets, Mapping) else None
    cells = grid.get("cell_paths") if isinstance(grid, Mapping) else None
    instruction = ""
    if slots is not None:
        instruction = _storyboard_slot_declaration(slots, references)
    elif isinstance(cells, list) and cells:
        last = len(cells) - 1
        parts = ["K0 is the opening frame of this shot, supplied as the first frame."]
        if last > 1:
            parts.append(f"The reference images are K1–K{last - 1} in their supplied order.")
        if last:
            parts.append(f"The final keyframe K{last} is supplied as the last frame.")
        instruction = " ".join(parts)
    audio = "\n".join(
        f"{speaker}: use the voice in reference audio {index} (音频{index})."
        for index, speaker in enumerate(audio_speakers, 1)
    )
    for section in (asset_links, audio, instruction):
        if section and section not in rendered:
            rendered = f"{rendered.rstrip()}\n\n{section}"
    if references:
        result = normalize_video_prompt(
            _number_video_references(rendered, references), negative_style=negative_style
        )
    else:
        # 视频模型没有「按名字绑参考图」的通道：参考图由 K 帧槽位直接下发。正文里的
        # ``@[名称]`` 去掉引用语法；前面已经写了同名裸名时不再重复补名。
        result = normalize_video_prompt(_strip_video_mentions(rendered), negative_style=negative_style)
    return normalize_video_prompt(
        _append_dialogue_only_audio_constraint(result, item, content_mode),
        negative_style=negative_style,
    )


def _strip_video_mentions(text: str) -> str:
    """去掉视频提示词里的 ``@[名称]`` 记号，保留正文里的裸名。

    分镜提示词的常见写法是 ``沈昭@[沈昭]``（裸名 + 引用记号）：记号直接删掉即可，不能把
    名字再补一遍，否则会变成「沈昭沈昭」。记号前没有同名裸名时补回名字，避免丢主体。
    """

    spans = mention_spans(text)
    if not spans:
        return text
    parts: list[str] = []
    last = 0
    for start, end, name in spans:
        prefix = text[last:start]
        parts.append(prefix)
        if not prefix.rstrip().endswith(name):
            parts.append(name)
        last = end
    parts.append(text[last:])
    return "".join(parts)


def _reference_only_slot_declaration(
    slots: "StoryboardVideoSlots",
    references: Sequence[VideoReferenceLabel] | None,
) -> str:
    """参考图模式：资产图排在 K 帧之前，位置必须按实际请求序列说清楚。"""

    keyframes = [
        (position, reference)
        for position, reference in enumerate(references or (), start=1)
        if reference.kind == "keyframe"
    ]
    asset_count = sum(1 for reference in references or () if reference.kind != "keyframe")
    fallback_keys = [f"K{frame.keyframe_index}" for _, frame in slots.frames if frame.keyframe_index is not None]
    if keyframes:
        key_labels = [reference.label for _position, reference in keyframes]
        frame_positions = [position for position, _reference in keyframes]
    else:
        key_labels = fallback_keys
        frame_positions = list(range(asset_count + 1, asset_count + 1 + len(fallback_keys)))
    if len(key_labels) == 1:
        key_range = key_labels[0]
    elif key_labels:
        key_range = f"{key_labels[0]}–{key_labels[-1]}"
    else:
        key_range = ""
    parts: list[str] = []
    if asset_count:
        if key_range:
            frame_span = (
                f"image {frame_positions[0]}"
                if len(frame_positions) == 1
                else f"images {frame_positions[0]}–{frame_positions[-1]}"
            )
            parts.append(
                f"Reference images 1–{asset_count} are asset reference images; "
                f"the keyframes {key_range} are supplied as {frame_span}, in that order."
            )
        else:
            parts.append(f"Reference images 1–{asset_count} are asset reference images.")
    elif key_range:
        parts.append(f"The reference images are {key_range} in their supplied order.")
    else:
        parts.append("The supplied images are reference images in supplied order.")
    if slots.continues_previous:
        if slots.start.kind == "video_last_frame":
            if key_range:
                parts.append(
                    "The previous shot's ending frame is not supplied as a reference; "
                    "continue the action from K0."
                )
            else:
                parts.append("The previous shot's ending frame is not supplied as a reference.")
        elif asset_count:
            parts.append(
                f"The first keyframe reference (image {asset_count + 1}) is the ending frame "
                "of the previous shot; continue from it."
            )
        else:
            parts.append("The first reference image is the ending frame of the previous shot; continue from it.")
    parts.append(
        "No separate opening or ending frame is supplied; preserve character, setting and action continuity "
        "across the references."
    )
    return " ".join(parts)


def _storyboard_slot_declaration(
    slots: "StoryboardVideoSlots",
    references: Sequence[VideoReferenceLabel] | None = None,
) -> str:
    """Describe precisely the frame references assigned to the request's slots."""

    if not slots.cell_count:
        return ""
    if slots.reference_only:
        return _reference_only_slot_declaration(slots, references)
    parts: list[str] = []
    if slots.continues_previous:
        parts.append("The first frame is the ending frame of the previous shot; continue from it.")
        if slots.cell_count > 1:
            parts.append("K0 is the opening frame of this shot.")
    else:
        parts.append("K0 is the opening frame of this shot, supplied as the first frame.")
    if slots.references:
        keys = [f"K{frame.keyframe_index}" for frame in slots.references]
        key_range = keys[0] if len(keys) == 1 else f"{keys[0]}–{keys[-1]}"
        parts.append(
            f"The reference images are {key_range} in their supplied order: "
            + (
                "the consecutive keyframes from the opening through to the penultimate moment."
                if slots.continues_previous
                else "the consecutive keyframes that follow."
            )
        )
    if slots.end is not None:
        parts.append(f"The final keyframe K{slots.end.keyframe_index} is supplied as the last frame.")
    parts.append("Follow this temporal order and preserve character, setting and action continuity.")
    return " ".join(parts)


def _attach_drama_speech_text(text: str, item: Mapping[str, Any] | None, *, characters: dict[str, Any] | None) -> str:
    """把 drama 的发声声明段追加到文本形态 video_prompt 正文之后。

    与结构形态共用 ``utterances_to_dialogue`` 与 ``_build_voice_profiles``，键名沿用
    ``video_prompt_to_yaml`` 的 ``Voice_Profiles`` / ``Dialogue``，两种形态对模型同一套词表。
    无台词、或无声门控下无声音风格可注入时不追加任何内容。两个声明段各自按内容判重：
    「结构化 → 文本」以当前渲染结果为初值，正文里已带着它们，整体判前缀会漏判而叠出第二份。
    """
    utterances = item.get("utterances") if isinstance(item, Mapping) else None
    dialogue = utterances_to_dialogue(utterances)
    sections: list[str] = []
    if characters is not None:
        voice_profiles = _build_voice_profiles(dialogue, characters)
        if voice_profiles:
            sections.append(yaml_section({"Voice_Profiles": voice_profiles}))
    if dialogue:
        sections.append(
            yaml_section({"Dialogue": [{"Speaker": entry["speaker"], "Line": entry["line"]} for entry in dialogue]})
        )
    pending = [section for section in sections if section not in text]
    if not pending:
        return text
    return f"{text.rstrip()}\n\n" + "\n".join(pending) + "\n"


def split_avoid_lines(text: str) -> tuple[str, str]:
    """把提示词正文里的排除项行拆出来，返回 ``(正文, 排除项文本)``。

    正文里的 ``Avoid:`` 行是各通道共享的排除项声明，由模版按产出媒体注入。多数供应商没有负向
    参数，这些行只能留在正文里当措辞；ComfyUI 端点有负向提示词的节点绑定，故要把它们从正文
    拆出来单独填。两侧共用这一份识别：正文该删哪些行的判据只有一条，通道之间不会一边删一边留。

    正文按行去掉全部排除项行后，连续空行塌缩成一个、首尾空白去掉——被删的行常自带前后空行，
    不塌缩会在正文中间留下一段空白。排除项文本按出现顺序以「，」拼接，逐行去掉前缀后的措辞
    原样保留，空措辞的行不进拼接（它只是个孤零零的 ``Avoid:``）。
    """
    kept: list[str] = []
    avoided: list[str] = []
    for line in text.split("\n"):
        match = _AVOID_LINE.match(line)
        if match is None:
            kept.append(line)
            continue
        phrase = match.group(1).strip()
        if phrase:
            avoided.append(phrase)
    body = _BLANK_RUN.sub("\n\n", "\n".join(kept)).strip()
    return body, _AVOID_SEPARATOR.join(avoided)


def append_avoid_text(literal: str, avoid_text: str) -> str:
    """把拆出的排除项文本追加到负向节点的字面值之后。

    追加而不覆盖：workflow 作者写在负向节点里的措辞是这份 workflow 的一部分（触发词、质量
    词），抹掉它等于改写作者的底稿。字面值为空时直接写入，不留一个前导分隔符。
    """
    if not literal.strip():
        return avoid_text
    if not avoid_text:
        return literal
    return f"{literal}{_AVOID_SEPARATOR}{avoid_text}"


def yaml_section(ordered: dict[str, Any]) -> str:
    """渲染一个可独立追加到文本形态提示词的 YAML 段（无尾随换行）。

    键名与缩进沿用 ``image_prompt_to_yaml`` / ``video_prompt_to_yaml``：发声声明段、参考图类型
    声明行在结构形态与文本形态下逐字同形，文本形态才能按内容判重。
    """
    return _dump_prompt_yaml(ordered).rstrip()


def strip_voice_profiles(video_prompt: dict[str, Any]) -> dict[str, Any]:
    """剥离入参自带的 ``voice_profiles`` 键。

    ``Voice_Profiles`` 声明段由编排层从角色资产机械派生，剧本 JSON / 调用方请求体均不
    承载它——两处注入点（worker 执行路径、SDK 入队路径）在决定是否调用
    :func:`build_drama_video_prompt` 之前都须先过一遍本函数，drama 之外的 content_mode
    或缺 ``utterances`` 的条目才不会让调用方自带（或剧本残留）的 ``voice_profiles`` 绕过
    C 类（真无声）门控直接进入 YAML。
    """
    return {k: v for k, v in video_prompt.items() if k != "voice_profiles"}


def build_drama_video_prompt(
    video_prompt: dict[str, Any],
    utterances: object,
    *,
    characters: dict[str, Any] | None,
) -> dict[str, Any]:
    """drama video_prompt 的 dialogue 与 Voice_Profiles 唯一注入出口。

    worker 执行路径与 SDK 入队路径共用本函数，两处注入点的产出同构由此在结构上成立，而非
    各写一份靠约定对齐。

    ``characters`` 为 ``None`` 表示不注入 Voice_Profiles（C 类真无声模型）。无论注入
    与否都先剥离入参自带的 ``voice_profiles``：该声明段由编排层从角色资产机械派生，剧本
    JSON 不承载它，剧本中的残留值不得绕过 C 类门控进入 YAML。
    """
    prompt = strip_voice_profiles(video_prompt)
    dialogue = utterances_to_dialogue(utterances)
    return _attach_voice_profiles(prompt, dialogue, characters=characters)


def build_drama_video_prompt_from_legacy_dialogue(
    video_prompt: dict[str, Any],
    *,
    characters: dict[str, Any] | None,
) -> dict[str, Any]:
    """legacy drama 场景（utterances 迁移前的旧剧本，台词仍留在 ``video_prompt.dialogue``、
    无分镜级 ``utterances`` 字段）的 Voice_Profiles 注入出口。

    ``load_script`` 按原始 JSON 读盘、不过 pydantic，这类存量剧本不会被
    ``DramaScene._migrate_legacy`` 自动补齐 ``utterances``；调用方须按 ``"utterances" not
    in item`` 识别后改走本函数，而非 :func:`build_drama_video_prompt`——旧结构的
    ``dialogue`` 已是 ``{speaker, line}`` 目标形态，无需 :func:`utterances_to_dialogue`
    转换，直接派生 Voice_Profiles。
    """
    prompt = strip_voice_profiles(video_prompt)
    dialogue = prompt.get("dialogue")
    if not isinstance(dialogue, list):
        dialogue = []
    return _attach_voice_profiles(prompt, dialogue, characters=characters)


def _attach_voice_profiles(
    prompt: dict[str, Any], dialogue: list[dict[str, Any]], *, characters: dict[str, Any] | None
) -> dict[str, Any]:
    prompt["dialogue"] = dialogue
    if characters is not None:
        voice_profiles = _build_voice_profiles(dialogue, characters)
        if voice_profiles:
            prompt["voice_profiles"] = voice_profiles
    return prompt


def _build_voice_profiles(dialogue: list[Any], characters: dict[str, Any]) -> list[dict[str, str]]:
    """从 dialogue speakers 与角色资产派生 Voice_Profiles 声明段。

    仅收录角色资产存在且 ``voice_style`` 非空者，一个 speaker 一条（按 dialogue 中首次
    出现的顺序去重）；speaker 未命中角色资产或资产 ``voice_style`` 为空，静默跳过
    （不建面向用户的提示通道，调用方按需记 logger）。

    speaker 与角色表 key 可能是 NFC/NFD 中的任一形态（存量剧本/桶无需迁移），按
    ``lib.project.asset_types`` 的比对坐标系归一后索引与去重，``Speaker`` 展示值保留 dialogue 原文。

    ``characters`` 来自明文 project.json，用户手编或外部脚本可能写成非 dict，逐层做类型
    收窄而非直接下标（同 ``lib.config.resolver._safe_dict`` 的取向）。
    """
    characters = normalize_asset_bucket(characters)
    seen: set[str] = set()
    profiles: list[dict[str, str]] = []
    for entry in dialogue:
        speaker = entry.get("speaker") if isinstance(entry, dict) else None
        if not isinstance(speaker, str) or not speaker:
            continue
        speaker_key = normalize_asset_name(speaker)
        if speaker_key in seen:
            continue
        seen.add(speaker_key)
        character = characters.get(speaker_key)
        if not isinstance(character, dict):
            logger.debug("Voice_Profiles 跳过：speaker %r 未命中角色资产", speaker)
            continue
        voice_style = character.get("voice_style")
        if isinstance(voice_style, str) and voice_style.strip():
            profiles.append({"Speaker": speaker, "Voice_Style": voice_style.strip()})
        else:
            logger.debug("Voice_Profiles 跳过：角色 %r 的 voice_style 为空", speaker)
    return profiles


def utterances_to_dialogue(utterances: object) -> list[dict[str, Any]]:
    """drama 口型音轨出口：从有序 ``utterances`` 取 dialogue-kind 条目，转成 video YAML 的
    ``{speaker, line}`` 列表（保留时序）。

    voiceover-kind 不进视频提示词（无 speaker，留给字幕 / TTS）。对脏数据稳健：非 list 整体、
    非 dict/object 元素、非 dialogue 条目一律跳过；dialogue 须 speaker 与 line 同时非空才进口型音轨，
    缺 speaker 的脏 dialogue（契约要求 dialogue 必带非空 speaker）不重新喂给 lip-sync / video prompt。

    兼容两种条目形态：原始 JSON ``dict`` 与已实例化的 Pydantic ``Utterance`` 模型对象（取同名属性）。
    speaker / text 用 ``isinstance(_, str)`` 显式取值，非字符串（如数字）按空处理、不 ``str()`` 强转，
    避免脏类型被静默字符串化进 YAML。

    条目若带 ``at``（秒）或 ``shot``（分镜段号）锚点则原样保留，供 ``anchor="shot"`` 的方言把台词
    内联回原句位置；不带锚点的老数据行为不变。
    """
    dialogue: list[dict[str, Any]] = []
    if not isinstance(utterances, list):
        return dialogue
    for entry in utterances:
        at_val: object = None
        shot_val: object = None
        if isinstance(entry, dict):
            kind = entry.get("kind")
            speaker_val = entry.get("speaker")
            text_val = entry.get("text")
            at_val = entry.get("at")
            shot_val = entry.get("shot")
        elif hasattr(entry, "kind"):
            kind = getattr(entry, "kind", None)
            speaker_val = getattr(entry, "speaker", None)
            text_val = getattr(entry, "text", None)
            at_val = getattr(entry, "at", None)
            shot_val = getattr(entry, "shot", None)
        else:
            continue

        if kind != "dialogue":
            continue

        speaker = speaker_val.strip() if isinstance(speaker_val, str) else ""
        line = text_val.strip() if isinstance(text_val, str) else ""
        if not (speaker and line):
            continue
        item: dict[str, Any] = {"speaker": speaker, "line": line}
        at = _as_seconds(at_val)
        if at is not None:
            item["at"] = at
        shot = _as_shot(shot_val)
        if shot is not None:
            item["shot"] = shot
        dialogue.append(item)
    return dialogue


def is_structured_image_prompt(image_prompt) -> bool:
    """
    检查 image_prompt 是否为结构化格式

    Args:
        image_prompt: image_prompt 字段值

    Returns:
        True 如果是结构化格式（dict），False 如果是旧的字符串格式
    """
    return isinstance(image_prompt, dict) and "scene" in image_prompt


def is_structured_video_prompt(video_prompt) -> bool:
    """
    检查 video_prompt 是否为结构化格式

    Args:
        video_prompt: video_prompt 字段值

    Returns:
        True 如果是结构化格式（dict），False 如果是旧的字符串格式
    """
    return isinstance(video_prompt, dict) and "action" in video_prompt


def validate_shot_type(shot_type: str) -> bool:
    """验证镜头类型是否为预设选项"""
    return shot_type in SHOT_TYPES


def validate_camera_motion(camera_motion: str) -> bool:
    """验证摄像机运动是否为预设选项"""
    return camera_motion in CAMERA_MOTIONS
