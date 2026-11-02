"""视频提示词的模型规范（spec）。

只定义「长什么样」——正文单位、运镜写法、声音段、台词写法、排除项、编号方式；
不定义「写什么」——内容仍来自 ``video_prompt`` 的 ``action / camera_motion /
ambiance_audio / dialogue`` 与参考图槽位。

按模型匹配的映射留在 :mod:`lib.prompts.model_specs.registry`（取值），
各方言的措辞留在模版片段（措辞）——与 ``docs/adr/0077`` 的判据一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: 正文单位：按秒段 / 中文镜头 / 英文镜头 / 整段散文。
BodyUnit = Literal["seconds", "shots_cn", "shots_en", "prose"]
#: 时间写法：内联（3-6秒：）/ 切换标记（At 00:03.000, cut to）/ 不写。
TimeMode = Literal["inline", "marker", "none"]
#: 运镜落点：独立键 / 方括号指令 / 嵌进句子 / 自然语句。
CameraMode = Literal["key", "bracket", "inline", "natural"]
#: 环境声落点。
SoundMode = Literal["key", "section", "inline"]
#: 配乐与排除项写法。
MusicMode = Literal["avoid", "section", "sentence", "none"]
#: 排除项写法。
NegativeMode = Literal["avoid_line", "sentence", "none"]
#: 台词落点。
DialogueMode = Literal["block", "inline", "quote", "h3"]
#: 台词在正文里的位置：``none`` 整段附在正文后；``shot`` 按 utterance 的时间锚点内联进对应分镜段。
DialogueAnchor = Literal["none", "shot"]
#: 参考图编号写法：中文「图N」/ MiniMax H3 本地权重的 ``<Picture N>``。
ReferenceStyle = Literal["figure", "pictures"]
#: 首尾帧与参考图能否同请求。
FrameWithReference = Literal["allow", "reject", "shared_slot", "unknown"]
#: 参考图编号方式：正文内联「名称图N」/ 由供应商 role 承载。
NumberingMode = Literal["inline", "roles"]
#: 编号声明落点。
DeclareMode = Literal["line", "prose", "roles"]


@dataclass(frozen=True, slots=True)
class BodySpec:
    unit: BodyUnit = "seconds"
    #: 每段前缀，``{n}`` 为段号（``[Shot {n}]`` / ``镜头{n}：``）。
    open: str = ""
    time: TimeMode = "inline"
    #: 切换标记只从第二段起写（H3 首段不带时间）。
    first_no_time: bool = False


@dataclass(frozen=True, slots=True)
class CameraSpec:
    mode: CameraMode = "key"
    label: str = "Camera_Motion"


@dataclass(frozen=True, slots=True)
class SoundSpec:
    mode: SoundMode = "key"
    label: str = "Ambiance_Audio"


@dataclass(frozen=True, slots=True)
class MusicSpec:
    mode: MusicMode = "avoid"
    text: str = "BGM、文字字幕、水印、Logo"
    label: str = "non_diegetic_music"
    empty_value: str = "N/A"


@dataclass(frozen=True, slots=True)
class NegativeSpec:
    mode: NegativeMode = "avoid_line"


@dataclass(frozen=True, slots=True)
class DialogueSpec:
    mode: DialogueMode = "block"
    label: str = "Dialogue"
    open_quote: str = "「"
    close_quote: str = "」"
    #: 是否把台词按 utterance 的 ``at`` / ``shot`` 锚点内联进对应分镜段。
    #: 默认 ``none``（整段附在正文后）——只有不支持后置台词的模型才打开。
    anchor: DialogueAnchor = "none"


@dataclass(frozen=True, slots=True)
class ReferenceSpec:
    frame_plus_reference: FrameWithReference = "unknown"
    numbering: NumberingMode = "inline"
    declare: DeclareMode = "line"
    #: 编号记号：``figure`` 写「图N」（中文模型），``pictures`` 写 ``<Picture N>``（H3 本地权重）。
    style: ReferenceStyle = "figure"


@dataclass(frozen=True, slots=True)
class VideoPromptSpec:
    """一个模型的提示词规范。字段默认值即 ``generic``（与现状逐字一致）。"""

    key: str
    label: str
    body: BodySpec = field(default_factory=BodySpec)
    camera: CameraSpec = field(default_factory=CameraSpec)
    sound: SoundSpec = field(default_factory=SoundSpec)
    music: MusicSpec = field(default_factory=MusicSpec)
    negative: NegativeSpec = field(default_factory=NegativeSpec)
    dialogue: DialogueSpec = field(default_factory=DialogueSpec)
    reference: ReferenceSpec = field(default_factory=ReferenceSpec)
    #: 注入写作侧 prompt 的方言要点（措辞），空串表示没有额外要点。
    writing_guide: str = ""
    #: 无台词时追加的一段声轨声明（空串表示不追加）。只在该分镜没有任何台词时写入。
    silent_clause: str = ""
    #: 是否把资产描述块（``Asset references``）拼进正文。本地权重端不认这段，置 False。
    include_asset_links: bool = True

    @property
    def is_generic(self) -> bool:
        return self.key == "generic"

    @property
    def negative_style(self) -> str:
        """``storyboard/video`` 模版的排除项变体：``avoid`` / ``positive`` / ``none``。"""

        if self.negative.mode == "avoid_line":
            return "avoid"
        if self.music.mode == "sentence" or self.negative.mode == "sentence":
            return "none"
        return "none"


#: 运镜枚举 → 供应商方括号指令（MiniMax Hailuo 官方 15 条指令，中文标签）。
CAMERA_BRACKETS: dict[str, str] = {
    "Static": "[固定]",
    "Pan Left": "[左摇]",
    "Pan Right": "[右摇]",
    "Tilt Up": "[上摇]",
    "Tilt Down": "[下摇]",
    "Zoom In": "[变焦推近]",
    "Zoom Out": "[变焦拉远]",
    "Push In": "[推进]",
    "Pull Out": "[拉远]",
    "Truck Left": "[左移]",
    "Truck Right": "[右移]",
    "Pedestal Up": "[上升]",
    "Pedestal Down": "[下降]",
    "Orbit": "[环绕]",
    "Tracking Shot": "[跟随]",
    "Shake": "[晃动]",
}

#: 运镜枚举 → 中文自然句（``相机 + 动作``），用于 inline / natural 方言。
CAMERA_PHRASES_ZH: dict[str, str] = {
    "Static": "固定机位",
    "Pan Left": "镜头向左平移",
    "Pan Right": "镜头向右平移",
    "Tilt Up": "镜头向上摇",
    "Tilt Down": "镜头向下摇",
    "Zoom In": "镜头推近",
    "Zoom Out": "镜头拉远",
    "Push In": "镜头缓缓推近",
    "Pull Out": "镜头缓缓拉远",
    "Truck Left": "镜头向左横移",
    "Truck Right": "镜头向右横移",
    "Pedestal Up": "机位向上抬升",
    "Pedestal Down": "机位向下降",
    "Orbit": "镜头环绕主体",
    "Tracking Shot": "镜头跟随主体",
    "Shake": "镜头轻微晃动",
}

#: 运镜枚举 → 英文自然句。
CAMERA_PHRASES_EN: dict[str, str] = {
    "Static": "the camera stays static",
    "Pan Left": "the camera pans left",
    "Pan Right": "the camera pans right",
    "Tilt Up": "the camera tilts up",
    "Tilt Down": "the camera tilts down",
    "Zoom In": "the camera zooms in",
    "Zoom Out": "the camera zooms out",
    "Push In": "the camera slowly pushes in",
    "Pull Out": "the camera slowly pulls out",
    "Truck Left": "the camera trucks left",
    "Truck Right": "the camera trucks right",
    "Pedestal Up": "the camera pedestals up",
    "Pedestal Down": "the camera pedestals down",
    "Orbit": "the camera orbits the subject",
    "Tracking Shot": "the camera tracks the subject",
    "Shake": "the camera shakes slightly",
}


def camera_bracket(motion: str) -> str:
    """运镜枚举 → 方括号指令；未知值原样加方括号，不静默丢运镜。"""

    value = motion.strip()
    if not value:
        return ""
    return CAMERA_BRACKETS.get(value, f"[{value}]")


def camera_phrase(motion: str, language: str = "中文") -> str:
    """运镜枚举 → 自然句；未知值返回空串（不臆造运镜）。"""

    value = motion.strip()
    if not value:
        return ""
    table = CAMERA_PHRASES_EN if language.lower().startswith(("en", "english")) else CAMERA_PHRASES_ZH
    return table.get(value, "")


__all__ = [
    "CAMERA_BRACKETS",
    "CAMERA_PHRASES_EN",
    "CAMERA_PHRASES_ZH",
    "BodySpec",
    "CameraSpec",
    "DialogueAnchor",
    "DialogueMode",
    "DialogueSpec",
    "MusicSpec",
    "NegativeSpec",
    "ReferenceSpec",
    "SoundSpec",
    "VideoPromptSpec",
    "camera_bracket",
    "camera_phrase",
]
