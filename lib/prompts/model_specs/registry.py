"""模型 → 提示词规范的匹配表。

匹配顺序：model 精确 → model 前缀 → endpoint key → provider 家族 → ``generic``。
前缀匹配让同族变体自动继承（如 MiniMax H3 的 Highspeed / Lite / Max 落在同一份
``minimax-h3`` 上）；自定义供应商挂 ``minimax-h3`` 端点时也命中同一条。
"""

from __future__ import annotations

from dataclasses import dataclass

from .spec import (
    BodySpec,
    CameraSpec,
    DialogueSpec,
    MusicSpec,
    NegativeSpec,
    ReferenceSpec,
    SoundSpec,
    VideoPromptSpec,
)

GENERIC = VideoPromptSpec(key="generic", label="通用")

MINIMAX_H3 = VideoPromptSpec(
    key="minimax-h3",
    label="MiniMax H3",
    body=BodySpec(unit="shots_en", open="[Shot {n}]", time="marker", first_no_time=True),
    camera=CameraSpec(mode="inline"),
    sound=SoundSpec(mode="section", label="overall_soundscape"),
    music=MusicSpec(mode="section", label="non_diegetic_music", empty_value="N/A"),
    negative=NegativeSpec(mode="none"),
    dialogue=DialogueSpec(mode="inline"),
    reference=ReferenceSpec(frame_plus_reference="reject", numbering="inline", declare="line"),
    writing_guide=(
        "多镜头用 [Shot N] 分段，切换时刻写 At 00:03.000, cut to；运镜嵌进段落句子，"
        "不单列 Camera_Motion 键；不要配乐写 non_diegetic_music: N/A；首尾帧与参考图互斥，"
        "本模式不带资产图。"
    ),
)

MINIMAX_H3_LOCAL = VideoPromptSpec(
    key="minimax-h3-local",
    label="MiniMax H3（ComfyUI 本地权重）",
    body=BodySpec(unit="shots_en", open="[Shot {n}]", time="marker", first_no_time=True),
    camera=CameraSpec(mode="inline"),
    sound=SoundSpec(mode="section", label="overall_soundscape"),
    music=MusicSpec(mode="section", label="non_diegetic_music", empty_value="N/A"),
    negative=NegativeSpec(mode="none"),
    dialogue=DialogueSpec(mode="h3", anchor="shot"),
    reference=ReferenceSpec(
        frame_plus_reference="reject",
        numbering="inline",
        declare="line",
        style="pictures",
    ),
    writing_guide=(
        "按 MiniMax H3 本地权重的口径写：正文分 [Shot N] 段，切换写 At 00:03.000, cut to；"
        "参考图一律用 <Picture N> 指认（编号＝实际连线顺序），不要写「图N」；"
        "台词写在原句位置并用 <d>[Chinese] 原文</d> 包住，说话人带 (S1)(S2) 稳定编号；"
        "不需要配乐写 non_diegetic_music: N/A；不写 Avoid 行。"
    ),
    silent_clause=(
        "No dialogue in this video: no character speaks, and every character keeps their lips "
        "completely closed from the first frame to the last. Do not add speech, dialogue, "
        "narration, voice-over, or any vocalization; keep only the ambient and physical sounds "
        "described above."
    ),
    include_asset_links=False,
)

MINIMAX_HAILUO = VideoPromptSpec(
    key="minimax-hailuo",
    label="MiniMax Hailuo 2.3",
    body=BodySpec(unit="seconds", time="inline"),
    camera=CameraSpec(mode="bracket"),
    sound=SoundSpec(mode="key", label="环境声"),
    music=MusicSpec(mode="sentence", text="画面只保留主体与场景本身，不放字幕、水印与 Logo。"),
    negative=NegativeSpec(mode="none"),
    dialogue=DialogueSpec(mode="quote"),
    reference=ReferenceSpec(frame_plus_reference="allow", numbering="inline", declare="line"),
    writing_guide=(
        "运镜用官方方括号指令（[推进] / [拉远] / [固定] / [左摇] 等 15 条），"
        "同一组方括号内最多 3 个；单次运镜控制在 5–6 秒；prompt 上限 2000 字符，"
        "只写首帧之后的变化，不复述画面。"
    ),
)

ARK_SEEDANCE = VideoPromptSpec(
    key="ark-seedance",
    label="Seedance",
    body=BodySpec(unit="shots_cn", open="镜头{n}：", time="inline"),
    camera=CameraSpec(mode="inline"),
    sound=SoundSpec(mode="key", label="音效"),
    music=MusicSpec(mode="sentence", text="配乐：无"),
    negative=NegativeSpec(mode="sentence"),
    dialogue=DialogueSpec(mode="quote"),
    reference=ReferenceSpec(frame_plus_reference="unknown", numbering="inline", declare="line"),
    writing_guide=(
        "用「镜头1/2/3」分镜，不写秒级时间码；一个镜头只指定一种运镜；"
        "优先低缓连续的小动作；约束词直接写在正文（保持无字幕、不要 Logo 与水印）。"
    ),
)

KLING = VideoPromptSpec(
    key="kling",
    label="可灵",
    body=BodySpec(unit="shots_cn", open="镜头{n}：", time="inline"),
    camera=CameraSpec(mode="inline"),
    sound=SoundSpec(mode="key", label="环境音"),
    music=MusicSpec(mode="sentence", text="画面只保留主体与场景本身。"),
    negative=NegativeSpec(mode="sentence"),
    dialogue=DialogueSpec(mode="quote"),
    reference=ReferenceSpec(frame_plus_reference="unknown", numbering="inline", declare="line"),
    writing_guide=(
        "运镜写成自然语句，一次一种；负向信息优先写在正文的约束句里，"
        "不要堆多个同时运动指令。"
    ),
)

GEMINI_VEO = VideoPromptSpec(
    key="gemini-veo",
    label="Veo",
    body=BodySpec(unit="prose"),
    camera=CameraSpec(mode="inline"),
    sound=SoundSpec(mode="key", label="Audio"),
    music=MusicSpec(mode="sentence", text="No on-screen text, watermark, or logo."),
    negative=NegativeSpec(mode="sentence"),
    dialogue=DialogueSpec(mode="inline"),
    reference=ReferenceSpec(frame_plus_reference="unknown", numbering="inline", declare="line"),
    writing_guide=(
        "整段散文，只写运动与运镜，不复述首帧画面；人物用泛称；"
        "排除项写实体名词（text / watermark / logo），不写 no / don't。"
    ),
)

OPENAI_SORA = VideoPromptSpec(
    key="openai-sora",
    label="Sora",
    body=BodySpec(unit="prose"),
    camera=CameraSpec(mode="inline"),
    sound=SoundSpec(mode="key", label="Audio"),
    music=MusicSpec(mode="sentence", text="No on-screen text, watermark, or logo."),
    negative=NegativeSpec(mode="sentence"),
    dialogue=DialogueSpec(mode="inline"),
    reference=ReferenceSpec(frame_plus_reference="shared_slot", numbering="inline", declare="line"),
    writing_guide=(
        "短句散文更好控：一条只写一个可完成的动作 + 运镜；不靠否定句，改写成正向描述；"
        "时长是请求参数，不用文字写「加长」。"
    ),
)

VIDU = VideoPromptSpec(
    key="vidu",
    label="Vidu",
    body=BodySpec(unit="prose"),
    camera=CameraSpec(mode="inline"),
    sound=SoundSpec(mode="key", label="音效"),
    music=MusicSpec(mode="sentence", text="画面只保留主体与场景本身，不加字幕与水印。"),
    negative=NegativeSpec(mode="sentence"),
    dialogue=DialogueSpec(mode="quote"),
    reference=ReferenceSpec(frame_plus_reference="unknown", numbering="inline", declare="line"),
    writing_guide=(
        "运镜用方向性语言 + 强度限定词（缓慢、轻微），同一句里只放一个运动指令；"
        "无 negative 通道，排除项改写成正向描述。"
    ),
)


@dataclass(frozen=True, slots=True)
class SpecMatch:
    spec: VideoPromptSpec
    model_exact: tuple[str, ...] = ()
    model_prefix: tuple[str, ...] = ()
    endpoint_keys: tuple[str, ...] = ()
    providers: tuple[str, ...] = ()


MATCHES: tuple[SpecMatch, ...] = (
    SpecMatch(
        spec=MINIMAX_H3,
        model_exact=("minimax-h3", "minimax h3"),
        model_prefix=("minimax-h3", "minimax h3"),
        endpoint_keys=("minimax-h3",),
    ),
    SpecMatch(
        spec=MINIMAX_HAILUO,
        model_exact=("minimax-hailuo-2.3", "minimax-hailuo-2.3-fast", "s2v-01"),
        model_prefix=("minimax-hailuo", "hailuo", "s2v-01"),
        endpoint_keys=("minimax-hailuo-v1", "minimax-hailuo-v1-fast", "minimax-s2v-01"),
    ),
    SpecMatch(
        spec=ARK_SEEDANCE,
        model_prefix=("seedance", "doubao-seedance"),
        providers=("ark",),
    ),
    SpecMatch(
        spec=KLING,
        model_prefix=("kling",),
        providers=("kling",),
    ),
    SpecMatch(
        spec=GEMINI_VEO,
        model_prefix=("veo",),
        providers=("gemini",),
    ),
    SpecMatch(
        spec=OPENAI_SORA,
        model_prefix=("sora",),
        providers=("openai",),
    ),
    SpecMatch(
        spec=VIDU,
        model_prefix=("vidu",),
        providers=("vidu",),
    ),
)


def _norm(value: str | None) -> str:
    return (value or "").strip().casefold()


_SPECS_BY_KEY: dict[str, VideoPromptSpec] = {
    spec.key: spec
    for spec in (
        GENERIC,
        MINIMAX_H3,
        MINIMAX_H3_LOCAL,
        MINIMAX_HAILUO,
        ARK_SEEDANCE,
        KLING,
        GEMINI_VEO,
        OPENAI_SORA,
        VIDU,
    )
}


def spec_by_key(key: str | None) -> VideoPromptSpec | None:
    """按规范键取 spec；未知名返回 None（调用方继续按模型名匹配，不因脏数据失败）。"""

    return _SPECS_BY_KEY.get((key or "").strip().casefold())


def parse_backend_ref(value: object) -> tuple[str, str]:
    """``"provider/model"`` → ``(provider, model)``；空值或只有 provider 时返回 ``("", "")``。"""

    if not isinstance(value, str) or not value.strip():
        return "", ""
    provider, _sep, model = value.strip().partition("/")
    provider, model = provider.strip(), model.strip()
    if not provider or not model:
        return "", ""
    return provider, model


def resolve_video_prompt_spec(
    provider_id: str | None = None,
    model_id: str | None = None,
    *,
    endpoint_key: str | None = None,
    declared: str | None = None,
) -> VideoPromptSpec:
    """按供应商 / 模型 / 端点取提示词规范；未命中一律 ``generic``。

    ``declared`` 是执行端点自己声明的规范键（例如 ComfyUI / RunningHub 工作流里检出
    MiniMax H3 节点后写下的 ``minimax-h3-local``）。它排在最前：这类端点的模型名是用户
    随手起的别名（「参考图生视频」），靠名字匹配一定落空，而落空就会把对的提示词渲染坏。
    未知键不报错，继续走名字匹配，避免旧数据把生成打挂。
    """

    declared_spec = spec_by_key(declared)
    if declared_spec is not None:
        return declared_spec

    provider = _norm(provider_id)
    model = _norm(model_id)
    endpoint = _norm(endpoint_key)

    for match in MATCHES:
        if model and model in (m.casefold() for m in match.model_exact):
            return match.spec
    for match in MATCHES:
        if model and any(model.startswith(prefix.casefold()) for prefix in match.model_prefix):
            return match.spec
    for match in MATCHES:
        if endpoint and endpoint in (k.casefold() for k in match.endpoint_keys):
            return match.spec
    for match in MATCHES:
        if provider and provider in (p.casefold() for p in match.providers):
            return match.spec
    return GENERIC


__all__ = [
    "GENERIC",
    "MATCHES",
    "MINIMAX_H3_LOCAL",
    "SpecMatch",
    "parse_backend_ref",
    "resolve_video_prompt_spec",
    "spec_by_key",
]
