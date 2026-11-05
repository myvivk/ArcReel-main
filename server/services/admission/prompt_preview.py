"""条目最终提示词的预览渲染。

回答「这个条目现在生成，送进图像 / 视频模型的提示词逐字是什么」。渲染函数与执行路径同一个
出口（``lib.prompts.prompt_builders.render_storyboard_image_prompt`` /
``lib.prompts.prompt_utils.render_storyboard_video_prompt``）。分镜图侧与执行器取同一份生成输入
（``lib.artifacts.generation_input``）：生成会被拒时该侧不可用并带缺口码，否则按与执行器相同的
图像后端参考图上限裁剪后渲染，「图N」编号与实发一致；裁剪发生时带上与执行期任务结果同形的
warning，用户在生成前就能看到有参考图不会随请求发出。视频侧按执行期同一口径备齐创作类型与
声音绑定。

只读：不向供应商发请求、不产生费用、不写产物清单。参考生视频路径的 unit 正文本身即提示词
主体，其预览走 ``lib.script.reference_video.script_preview``，不经本模块。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from lib.artifacts.artifact_input_claims import (
    resolve_usable_episode_script_input,
    resolve_usable_storyboard_video_slots,
)
from lib.artifacts.generation_input import (
    InputRefused,
    StoryboardImageInput,
    project_input_observation,
    storyboard_image_input,
)
from lib.config.resolver import ConfigResolver
from lib.db import async_session_factory
from lib.generation.video_request_facts import (
    ExecutionVideoIdentity,
    VideoRequestFacts,
    evaluate_video_request_facts,
)
from lib.project.project_manager import ProjectManager, get_project_manager
from lib.prompts.model_specs import VideoPromptSpec, resolve_video_prompt_spec
from lib.prompts.prompt_utils import VideoReferenceLabel
from lib.script.script_models import resolve_content_mode
from lib.script.storyboard_sequence import (
    StoryboardImageBindingRequired,
    apply_storyboard_frame_mode,
    find_storyboard_item,
    get_storyboard_items,
    require_confirmed_storyboard_grid,
    resolve_storyboard_video_asset_images,
    split_storyboard_video_asset_images,
    storyboard_motion_prompt_item_problem,
)
from lib.script.storyboard_video_request import (
    resolve_storyboard_audio_inputs,
    resolve_storyboard_video_prompt,
    storyboard_asset_links,
    storyboard_video_asset_trim_warning,
)
from lib.speech.voice_settings import VoiceRenderSettings
from server.services.admission.reference_admission import input_refusal_error
from server.services.admission.video_batch_admission import storyboard_video_prompt, storyboard_video_request_facts
from server.services.tasks.generation_context import (
    ImageLaneRequest,
    VideoLaneRequest,
    resolve_generation_context,
)

logger = logging.getLogger(__name__)

#: 条目的该提示词为待生成（``None`` 或字段不在）时的不可用原因：机械转换后尚未补写。
UNAVAILABLE_PENDING = "prompt_preview_pending"
#: 提示词字段在场但为空（空串 / 空对象）时的不可用原因。
UNAVAILABLE_MISSING = "prompt_preview_missing"
#: 提示词字段在场但形状不合规、渲染函数拒绝时的不可用原因。
UNAVAILABLE_INVALID = "prompt_preview_invalid"


class ScriptItemNotFound(LookupError):
    """剧本里没有该条目 id。"""


@dataclass(frozen=True)
class RenderedPrompt:
    """一侧提示词的渲染结果：``text`` 与 ``unavailable`` 恰有一个非 ``None``。

    ``warnings`` 是渲染这份文本时产生的非阻断提示（如参考图按后端上限裁剪），与任务
    ``result.warnings`` 的 ``{key, params}`` 条目同形；只在 ``text`` 渲染成功时出现。
    """

    text: str | None = None
    unavailable: str | None = None
    #: 不可用原因的文案参数：生成输入被拒时带全部缺口（与执行期错误详情同形）。
    unavailable_params: dict[str, object] = field(default_factory=dict)
    is_text_form: bool = False
    warnings: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ItemPromptPreview:
    item_id: str
    content_mode: str
    storyboard_image: RenderedPrompt
    video: RenderedPrompt


def _unavailable_before_rendering(prompt: object) -> str | None:
    """渲染之前就能判定的不可用原因：待生成或为空；``None`` 表示该提示词值得渲染。"""
    if prompt is None:
        return UNAVAILABLE_PENDING
    if (isinstance(prompt, str) and not prompt.strip()) or prompt == {}:
        return UNAVAILABLE_MISSING
    return None


def _render(prompt: object, render: Any) -> RenderedPrompt:
    is_text_form = isinstance(prompt, str)
    if (reason := _unavailable_before_rendering(prompt)) is not None:
        return RenderedPrompt(unavailable=reason, is_text_form=is_text_form)
    try:
        return RenderedPrompt(text=render(prompt), is_text_form=is_text_form)
    except (ValueError, TypeError, KeyError):
        return RenderedPrompt(unavailable=UNAVAILABLE_INVALID, is_text_form=is_text_form)


async def reference_image_limit(
    project_name: str,
    project: dict[str, Any],
    project_path: Path,
) -> tuple[int, str]:
    """image lane 的 backend 参考图上限与模型名，与执行期同一裁剪口径；``0`` 表示不裁剪。

    解析 image lane 会构造 backend（需凭证）。预览是只读视图，凭证缺失 / 供应商未配置时
    不该连提示词都看不到：解析失败按不裁剪处理并记一条 info——这种项目本来也跑不了生成，
    预览与执行的逐字一致仍然成立。预览按项目当前配置解析、没有任务 payload：若入队的
    payload 钉了别的供应商且其上限不同，编号会与那次执行不一致，这是预览不知 payload 的
    既有限制。
    """
    try:
        ctx = await resolve_generation_context(
            project_name,
            None,
            project=project,
            project_path=project_path,
            image=ImageLaneRequest(generation_type="i2i"),
        )
    except Exception as exc:
        logger.info("预览无法解析 image lane，参考图不按后端上限裁剪：%s", exc)
        return 0, ""
    return ctx.image.max_reference_images, ctx.image.backend_model


@dataclass(frozen=True)
class CandidateVideoContext:
    """候选模型下的视频预览上下文：方言规范 + 该模型的能力事实（可能解析不出）。"""

    spec: VideoPromptSpec | None = None
    facts: VideoRequestFacts | None = None


async def resolve_candidate_video_context(
    project_name: str,
    project: dict[str, Any],
    project_path: Path,
    video_backend: str | None,
) -> CandidateVideoContext | None:
    """解析这次预览要按哪个视频模型出文本；未给候选时按项目/全局的生效模型。

    规范走与执行期同一入口（``resolve_generation_context``）：由执行端点的定义 / 工作流结构
    检出，不靠模型名猜；事实按参考生视频（``r2v``）桶求值——这条路就是参考图模式，按 i2v
    求值会得到「模型不支持 i2v」这种与本次请求无关的失败，参考图也会被裁掉。

    只读：构造 backend 不提交任务、不扣费；解析失败按「不知道」处理，退回按模型名匹配 /
    项目配置，不让一次预览把项目打挂。
    """

    backend = (video_backend or "").strip()
    payload = {"video_provider_r2v": backend} if backend else None
    provider = model = ""
    declared: str | None = None
    try:
        ctx = await resolve_generation_context(
            project_name,
            payload,
            project=project,
            project_path=project_path,
            video=VideoLaneRequest(generation_type="r2v"),
        )
        provider = ctx.video.provider_model.provider_id
        model = ctx.video.backend_model
        declared = ctx.video.prompt_spec
    except Exception as exc:
        logger.info("预览无法解析视频 lane（候选=%s），规范按模型名回落：%s", backend or "项目配置", exc)
        if not backend:
            return None
        provider, _sep, model = backend.partition("/")

    if not provider or not model:
        return None
    spec: VideoPromptSpec | None = (
        resolve_video_prompt_spec(declared=declared) if declared else resolve_video_prompt_spec(provider, model)
    )

    facts: VideoRequestFacts | None = None
    try:
        evaluated = await evaluate_video_request_facts(
            project,
            route="storyboard",
            generation_type="r2v",
            identity=ExecutionVideoIdentity(provider, model),
            resolver=ConfigResolver(async_session_factory),
        )
    except Exception as exc:
        logger.info("预览无法按 %s/%s 求值视频请求事实：%s", provider, model, exc)
    else:
        if isinstance(evaluated, VideoRequestFacts):
            facts = evaluated
    return CandidateVideoContext(spec=spec, facts=facts)


async def preview_item_prompts(
    project_name: str,
    script_file: str,
    item_id: str,
    *,
    video_backend: str | None = None,
    draft: bool = False,
    projects: ProjectManager | None = None,
) -> ItemPromptPreview:
    """渲染一个分镜条目的分镜图与视频最终提示词文本。

    ``video_backend`` 是生成弹窗里选中的候选模型（``provider/model``）。给了就按它解析视频
    lane 的提示词规范——模型决定用哪套方言，面板上看到的就是这次真正会发出去的文本；不给则
    沿用项目当前配置。``draft`` 允许参考素材 K 帧尚未确认时渲染草稿（不含 K 帧声明），
    供写作阶段核对目标模型方言。``projects`` 供工具运行时把已解析的 ProjectManager 注入进来。
    """

    def _load() -> tuple[dict[str, Any], Path, dict[str, Any], dict[str, Any], str]:
        manager = projects if projects is not None else get_project_manager()
        project = manager.load_project(project_name)
        project_path = manager.get_project_path(project_name)
        script = manager.load_script(project_name, script_file)
        items, id_field, *_ = get_storyboard_items(script)
        resolved = find_storyboard_item(items, id_field, item_id)
        if resolved is None:
            raise ScriptItemNotFound(item_id)
        return project, project_path, script, resolved[0], resolve_content_mode(script, project)

    project, project_path, script, item, content_mode = await asyncio.to_thread(_load)
    # 弹窗选了候选模型就先按它求值：方言规范与能力事实都按这次请求的模型走。
    candidate = await resolve_candidate_video_context(project_name, project, project_path, video_backend)
    # 声音绑定按项目当前的视频能力档解析（非 drama 或无声一律 None），与执行期同一判据。
    video_facts = (
        candidate.facts
        if candidate is not None and candidate.facts is not None
        else await storyboard_video_request_facts(project)
    )
    silent = isinstance(video_facts, VideoRequestFacts) and (
        not video_facts.requested_generate_audio or video_facts.voice_consistency == "none"
    )
    voice_characters = (project.get("characters") or {}) if content_mode == "drama" and not silent else None
    candidate_spec = candidate.spec if candidate is not None else None

    def _assemble() -> StoryboardImageInput | InputRefused:
        # 与执行期读同一份生成输入（含上一分镜图的可用性判定），「图N」编号才能逐字一致；
        # 剧本未登记或内容已变更时以 ValueError 拒绝，按提示词不可渲染处理，产物清单本身的
        # 故障（ArtifactManifestError）沿用 fail loud。
        script_input = resolve_usable_episode_script_input(
            project_path=project_path,
            project=project,
            script=script,
            script_filename=script_file,
        )
        return storyboard_image_input(
            project,
            script,
            episode=script_input.episode,
            resource_id=item_id,
            observation=project_input_observation(project_path),
        )

    image: RenderedPrompt | None = None
    generation_input: StoryboardImageInput | None = None
    image_prompt = item.get("image_prompt")
    is_text_form = isinstance(image_prompt, str)
    # 空提示词不可渲染；待生成提示词交给生成输入，与引用缺口一次报全。
    if image_prompt is not None and (reason := _unavailable_before_rendering(image_prompt)) is not None:
        image = RenderedPrompt(unavailable=reason, is_text_form=is_text_form)
    else:
        try:
            assembled = await asyncio.to_thread(_assemble)
        except (ValueError, TypeError, KeyError):
            image = RenderedPrompt(unavailable=UNAVAILABLE_INVALID, is_text_form=is_text_form)
        else:
            if isinstance(assembled, InputRefused):
                refusal = input_refusal_error(assembled)
                image = RenderedPrompt(
                    unavailable=refusal.key,
                    unavailable_params=dict(refusal.params),
                    is_text_form=is_text_form,
                )
            else:
                generation_input = assembled
    limit = (
        await reference_image_limit(project_name, project, project_path)
        if generation_input is not None and generation_input.references
        else (0, "")
    )

    def _render_both() -> ItemPromptPreview:
        # 规范只解析一次：渲染进 `_render` 的正文与 render_video 里用的 spec 必须是同一个，
        # 否则 H3 这类方言会被当成通用口径渲染。
        prompt_spec = (
            candidate_spec
            if candidate_spec is not None
            else resolve_video_prompt_spec(video_facts.provider_id, video_facts.model_id)
            if isinstance(video_facts, VideoRequestFacts)
            else None
        )
        storyboard_image = image
        if storyboard_image is None:
            assert generation_input is not None
            max_reference_images, model = limit
            try:
                rendered = generation_input.render(max_reference_images=max_reference_images, model=model)
            except (ValueError, TypeError, KeyError):
                storyboard_image = RenderedPrompt(unavailable=UNAVAILABLE_INVALID, is_text_form=is_text_form)
            else:
                storyboard_image = RenderedPrompt(
                    text=rendered.prompt,
                    is_text_form=is_text_form,
                    warnings=rendered.warnings,
                )

        video_warnings: list[dict[str, Any]] = []

        def render_video(_prompt: object) -> str:
            has_grid = True
            try:
                require_confirmed_storyboard_grid(item, item_id)
            except StoryboardImageBindingRequired:
                if not draft:
                    raise
                # 草稿只给写作阶段核对目标模型方言：没有 K 帧，不声明参考图编号，
                # 也不把「这次真正发什么」说满；正式预览仍要求 confirmed 宫格。
                has_grid = False
            if problem := storyboard_motion_prompt_item_problem(project, item):
                raise ValueError(f"动态分镜提示词不符合视频生成要求: {problem}")
            asset_links = storyboard_asset_links(project, item, project_name)
            slots = None
            references: list[VideoReferenceLabel] = []
            audio_speakers: tuple[str, ...] = ()
            if has_grid:
                script_input = resolve_usable_episode_script_input(
                    project_path=project_path,
                    project=project,
                    script=script,
                    script_filename=script_file,
                )
                items, id_field, *_ = get_storyboard_items(script)
                slots = resolve_usable_storyboard_video_slots(
                    project_path=project_path,
                    project=project,
                    episode=script_input.episode,
                    items=items,
                    id_field=id_field,
                    resource_id=item_id,
                )
                # 生成固定走参考图模式：预览与实发同一份参考图序列（资产图在前、K 帧在后）。
                slots = apply_storyboard_frame_mode(slots, "reference")
                if isinstance(video_facts, VideoRequestFacts):
                    assets = resolve_storyboard_video_asset_images(
                        project_path=project_path, project=project, item=item
                    )
                    limit = video_facts.max_reference_images or 0
                    selected_assets, dropped_assets = split_storyboard_video_asset_images(
                        assets,
                        max_reference_images=limit,
                        frame_count=len(slots.frames),
                    )
                    if (
                        trim_warning := storyboard_video_asset_trim_warning(
                            dropped_assets,
                            limit=limit,
                            frame_count=len(slots.frames),
                        )
                    ) is not None:
                        video_warnings.append(trim_warning)
                    references.extend(VideoReferenceLabel(name, kind) for kind, name, _path in selected_assets)
                references.extend(
                    VideoReferenceLabel(f"K{frame.keyframe_index}", "keyframe")
                    for _role, frame in slots.frames
                    if frame.keyframe_index is not None
                )
                if isinstance(video_facts, VideoRequestFacts):
                    audio_speakers = resolve_storyboard_audio_inputs(
                        project=project,
                        project_path=project_path,
                        item=item,
                        slots=slots,
                        voice=VoiceRenderSettings.from_request_facts(video_facts),
                    ).speakers
            return storyboard_video_prompt(
                item,
                content_mode=content_mode,
                voice_characters=voice_characters,
                slots=slots,
                asset_links=asset_links,
                audio_speakers=audio_speakers,
                spec=prompt_spec,
                references=references or None,
                language=(project.get("source_language") if isinstance(project.get("source_language"), str) else None),
            )

        # 缺正文（待生成 / 空）交给 `_render` 按 pending/missing 分类；其余按目标模型投影。
        resolved_video_prompt: object = item.get("image_prompt")
        with contextlib.suppress(ValueError):
            resolved_video_prompt = resolve_storyboard_video_prompt(item, prompt_spec)
        video = _render(resolved_video_prompt, render_video)
        if video.text is not None and video_warnings:
            video = replace(video, warnings=tuple(video_warnings))
        return ItemPromptPreview(
            item_id=item_id,
            content_mode=content_mode,
            storyboard_image=storyboard_image,
            video=video,
        )

    return await asyncio.to_thread(_render_both)


__all__ = [
    "UNAVAILABLE_INVALID",
    "UNAVAILABLE_MISSING",
    "UNAVAILABLE_PENDING",
    "ItemPromptPreview",
    "RenderedPrompt",
    "ScriptItemNotFound",
    "preview_item_prompts",
]
