"""
Task execution service for queued generation jobs.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arcreel_market_core.video_backend_contract import VideoCapabilityError
from lib.artifacts.artifact_activation import (
    ArtifactCurrencyResolver,
    ArtifactInputClaim,
    active_artifact_currency_resolver,
    assert_current_artifact_input_claims_usable,
    bind_artifact_input_claims_to_content_digests,
    resolve_usable_episode_script_input,
)
from lib.artifacts.artifact_input_claims import resolve_usable_storyboard_video_slots
from lib.artifacts.artifact_manifest import (
    ArtifactBasisDescriptor,
    compose_video_artifact_basis,
)
from lib.artifacts.generation_input import (
    AssetSheetInput,
    FrozenGenerationInput,
    InputRefused,
    StoryboardImageInput,
    asset_sheet_input,
    project_input_observation,
    storyboard_image_input,
)
from lib.artifacts.version_manager import PaidVersionCommit
from lib.artifacts.video_artifact_facts import VideoArtifactCurrencyFacts
from lib.artifacts.video_visual_provenance import build_storyboard_video_visual_basis
from lib.artifacts.visual_artifact_provenance import (
    build_storyboard_video_artifact_visual_basis,
)
from lib.config.resolver import video_bucket_for_generation_mode
from lib.config.service import DEFAULT_VIDEO_POLL_TIMEOUT_SECONDS
from lib.db.base import DEFAULT_USER_ID
from lib.generation.execution_checkpoint import (
    ProviderMediaInput,
    StagedProviderMedia,
    StoryboardSubmissionCheckpoint,
    checkpoint_version_metadata,
    cleanup_staged_provider_media,
    stage_provider_media_for_task,
)
from lib.generation.generation_queue import (
    DispatchProviderChanged,
    get_generation_queue,
    without_video_execution_identity,
)
from lib.generation.render_lane import is_render_task_type
from lib.generation.video_request_facts import (
    DEFAULT_PLANNED_DURATION_SECONDS,
    VideoRequestFactsError,
    audio_switch_conflict,
    require_video_request_facts,
)
from lib.infra.async_thread import run_sync_transaction
from lib.infra.content_digest import sha256_file
from lib.infra.path_safety import safe_join
from lib.infra.thumbnail import extract_video_last_frame, extract_video_thumbnail
from lib.project.asset_derivatives import DERIVATIVE_ASSET_TYPE, DERIVATIVE_TASK_TYPE
from lib.project.asset_types import (
    ASSET_SPECS,
    resolve_asset_key,
    validate_asset_name,
)
from lib.project.project_change_hints import build_change_label, emit_project_change_batch, project_change_source
from lib.project.project_manager import (
    EpisodeScriptReboundError,
    ProjectManager,
    get_project_manager,
    resolve_episode_script_binding,
)
from lib.project.resource_paths import CHARACTER_DERIVATIVE_RESOURCE_TYPE, resource_relative_path
from lib.prompts.prompt_utils import render_storyboard_video_prompt
from lib.script.factory_episode import require_factory_script
from lib.script.grid.keyframe_parser import (
    keyframe_reference_problems,
    parse_grid_prompt_cells,
    parse_scene_shots,
    scene_revision_of,
    shot_scene_text,
)
from lib.script.grid.layout import grid_aspect_ratio_for
from lib.script.grid.shot_grid_manager import ShotGridManager
from lib.script.grid.shot_prompt_builder import build_shot_grid_prompt, shot_grid_cells
from lib.script.script_editor import resolve_items
from lib.script.script_models import resolve_content_mode
from lib.script.script_skeleton import SKELETON_ENTITY_TYPES, SKELETON_ITEM_LABEL_KEYS, resolve_script_kind
from lib.script.storyboard_sequence import (
    find_storyboard_item,
    get_storyboard_items,
)
from lib.script.storyboard_video_request import (
    resolve_storyboard_audio_inputs,
    storyboard_asset_links,
    storyboard_frame_evidence,
)
from lib.speech.audio_utils import (
    AUDIO_REFERENCE_MAX_BYTES,
    AUDIO_REFERENCE_MAX_SECONDS,
    AUDIO_REFERENCE_MIN_SECONDS,
    probe_audio_duration_seconds,
    probe_existing_audio_duration_seconds,
)
from lib.speech.narration_config import (
    NarrationConfigError,
    project_tts_settings,
    require_project_tts_generation,
)
from lib.speech.narration_delivery import (
    build_narration_audio_basis,
    canonical_narration_text,
    register_narration_audio_transactionally,
)
from lib.speech.speech_artifact_provenance import build_video_duration_basis
from lib.speech.speech_composition import SpeechAdmissionError, admit_script_unit
from lib.speech.voice_settings import VoiceRenderSettings
from server.services.admission.reference_admission import input_refusal_error
from server.services.currency.video_artifact_currency import (
    VideoArtifactCommitter,
    complete_video_artifact_commit,
    freeze_video_speech_facts,
)
from server.services.tasks.derivative_sheet_tasks import execute_character_derivative_task
from server.services.tasks.formal_image_commit import (
    FormalImageCommitOutcome,
    FormalImagePlan,
    StagedImageCommit,
    get_aspect_ratio,
    run_asset_sheet_image_task,
    run_formal_image_task,
    shot_grid_formal_image_callback,
    storyboard_formal_image_callback,
)
from server.services.tasks.generation_context import (
    AudioLaneRequest,
    ImageLaneRequest,
    VideoLaneRequest,
    resolve_generation_context,
)
from server.services.tasks.image_edit_tasks import execute_image_edit_task
from server.services.tasks.render_tasks import execute_render_task

logger = logging.getLogger(__name__)


def assert_duration_supported(duration: int | float | str, supported_durations: list[int]) -> None:
    """执行层能力守卫：duration 必须落在已解析 model 的 supported_durations 内。

    这是 `duration ↔ supported_durations` 唯一的权威校验家——provider 在执行时才解析
    （见 ADR-0001），故能力校验只能坐在 provider 解析之后。``supported_durations`` 为空只在
    时长由端点固定时出现（``docs/adr/0082``），此时放行：能力解析不出已在取事实时阻断。

    duration 可能来自外部配置（payload / project.json），故安全解析字符串 / 浮点：
    可解析为整数秒（如 ``"6"`` / ``6.0``）的归一化后比较；非整数秒（如 ``4.5``）一律
    视为非法而**拒绝**，不做截断式归一化（截断会把本应拒绝的非法值静默修正）。

    校验失败抛 :class:`VideoCapabilityError`（带稳定 code），与 ImageCapabilityError 对称——
    Worker 按 code + params 落 task.error_message，文案由读侧 Translator 渲染。
    """
    if not supported_durations:
        return
    try:
        numeric = float(duration)
    except (TypeError, ValueError) as exc:
        raise VideoCapabilityError("video_duration_invalid", duration=duration) from exc
    if not numeric.is_integer():
        raise VideoCapabilityError("video_duration_invalid", duration=duration)
    seconds = int(numeric)
    if seconds not in supported_durations:
        raise VideoCapabilityError(
            "video_duration_not_supported",
            duration=seconds,
            supported=", ".join(str(d) for d in supported_durations),
        )


def _episode_from_script(script: dict[str, Any] | None) -> int | None:
    if not isinstance(script, dict):
        return None
    episode = script.get("episode")
    if isinstance(episode, int):
        return episode
    return None


def compute_affected_fingerprints(project_name: str, task_type: str, resource_id: str) -> dict[str, int]:
    """计算受影响文件的 mtime 指纹"""
    try:
        project_path = get_project_manager().get_project_path(project_name)
    except Exception:
        return {}

    paths: list[tuple[str, Path]] = []

    if task_type == "storyboard":
        paths.append(
            (
                f"storyboards/scene_{resource_id}.png",
                project_path / "storyboards" / f"scene_{resource_id}.png",
            )
        )
    elif task_type == "video":
        paths.append(
            (
                f"videos/scene_{resource_id}.mp4",
                project_path / "videos" / f"scene_{resource_id}.mp4",
            )
        )
        paths.append(
            (
                f"thumbnails/scene_{resource_id}.jpg",
                project_path / "thumbnails" / f"scene_{resource_id}.jpg",
            )
        )
    elif task_type == "character":
        paths.append(
            (
                f"characters/{resource_id}.png",
                project_path / "characters" / f"{resource_id}.png",
            )
        )
    elif task_type == "scene":
        paths.append(
            (
                f"scenes/{resource_id}.png",
                project_path / "scenes" / f"{resource_id}.png",
            )
        )
    elif task_type == "prop":
        paths.append(
            (
                f"props/{resource_id}.png",
                project_path / "props" / f"{resource_id}.png",
            )
        )
    elif task_type == "tts":
        audio_rel = resource_relative_path("audio", resource_id)
        paths.append((audio_rel, project_path / audio_rel))
    elif task_type == DERIVATIVE_TASK_TYPE:
        # 衍生的 resource_id 本身是 `本体名/衍生名`，两段原样成为路径层级。
        sheet_rel = resource_relative_path(CHARACTER_DERIVATIVE_RESOURCE_TYPE, resource_id)
        paths.append((sheet_rel, project_path / sheet_rel))

    result: dict[str, int] = {}
    for rel, abs_path in paths:
        if abs_path.exists():
            result[rel] = abs_path.stat().st_mtime_ns

    return result


# (entity_type, action, label_key, include_script_episode)
# label_key 是事件载荷携带的稳定标识，界面按用户语言查表成文；文案本身见 lib/i18n 的
# ``event_label_*``。三类项目级资产（character / scene / prop）的 spec 由
# lib.project.asset_types.ASSET_SPECS 派生。
# storyboard / video / reference_video 不在此表——三者按剧本骨架种类（segments/scenes/shots/
# video_units）动态派生 entity_type 与条目名词，见 _SKELETON_DRIVEN_TASK_ACTIONS，避免恒发
# ``segment``/「分镜」而与分镜级事件（project_state_projection.py）名词不一致。
_TASK_CHANGE_SPECS: dict[str, tuple] = {
    "voice_sample": ("character", "voice_sample_ready", "voice_sample", False),
    **{atype: (atype, "updated", f"asset_image_{atype}", False) for atype in ASSET_SPECS},
    # 衍生资产图落在本体的 entity 下（entity_id 是 `本体名/衍生名`），但条目名词另立一条：
    # 复用 asset_image_character 会把复合 id 当成角色名读出来。
    DERIVATIVE_TASK_TYPE: (DERIVATIVE_ASSET_TYPE, "updated", "asset_image_character_derivative", False),
}

# 骨架驱动的任务类型 → 完成事件 action。entity_type/条目名词按项目剧本当前骨架种类
# （resolve_script_kind，与分镜级事件同一判定）动态解析，不按 task_type 恒定硬编码。
_SKELETON_DRIVEN_TASK_ACTIONS: dict[str, str] = {
    "storyboard": "storyboard_ready",
    "video": "video_ready",
    "tts": "tts_ready",
}

# 任务类型自带条目标签的例外：tts 的产物是旁白配音，与骨架条目名词不同名。reference_video 显式
# 指向视频单元，与参考生视频项目的骨架名词同口径；storyboard/video 未列出，回退到按骨架种类派生
# 的 label_key（分镜），与同项目分镜级事件同口径。
_SKELETON_TASK_LABEL_KEYS: dict[str, str] = {
    "tts": "narration_audio",
}


def _load_event_script(project_name: str, script_file: str | None) -> dict[str, Any] | None:
    """加载完成事件所属剧本一次，供骨架种类与 episode 共用；缺失/损坏时返回 None。

    调用方对 None 各自兜底（骨架种类回退 ``"segments"``、episode 回退 ``None``），
    不让剧本加载失败导致通知发送中断。
    """
    if not script_file:
        return None
    try:
        return get_project_manager().load_script(project_name, script_file)
    except Exception:
        return None


def emit_generation_success_batch(
    *,
    task_type: str,
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
) -> dict[str, int]:
    """发送生成/上传完成的项目变更事件，返回受影响文件的指纹（调用方可直接复用，免二次计算）。

    事件 source 由 project_change_source contextvar 决定（worker / webui 调用方各自包裹）。
    """
    if task_type == "image_edit":
        # 编辑完成事件与「同一资源的生成完成事件」同形状：按 payload.resource_type 派发到
        # 既有 spec 表（storyboard 走骨架驱动、四类资产走 ASSET_SPECS 派生表），entity/action/
        # 指纹与生成路径一致，前端既有的 SSE fingerprint 刷新零改动即可覆盖编辑完成。
        task_type = str(payload.get("resource_type") or "")

    script_file = str(payload.get("script_file") or "") or None
    # 单次加载剧本，骨架种类与 episode 共用，避免同一 script_file 双解析。
    script = _load_event_script(project_name, script_file)

    action = _SKELETON_DRIVEN_TASK_ACTIONS.get(task_type)
    if action is not None:
        kind = resolve_script_kind(script) if isinstance(script, dict) else "scenes"
        entity_type = SKELETON_ENTITY_TYPES.get(kind, "segment")
        label_key = _SKELETON_TASK_LABEL_KEYS.get(task_type) or SKELETON_ITEM_LABEL_KEYS.get(kind, "skeleton_segments")
        include_script_episode = True
    else:
        spec = _TASK_CHANGE_SPECS.get(task_type)
        if spec is None:
            return {}
        entity_type, action, label_key, include_script_episode = spec

    asset_fingerprints = compute_affected_fingerprints(project_name, task_type, resource_id)

    change: dict[str, Any] = {
        "entity_type": entity_type,
        "action": action,
        "entity_id": resource_id,
        **build_change_label(label_key, id=resource_id),
        "focus": None,
        "important": True,
        "asset_fingerprints": asset_fingerprints,
    }
    if include_script_episode:
        change["script_file"] = script_file
        change["episode"] = _episode_from_script(script)

    try:
        emit_project_change_batch(project_name, [change])
    except Exception:
        logger.exception(
            "发送生成完成项目事件失败 project=%s task_type=%s resource_id=%s",
            project_name,
            task_type,
            resource_id,
        )
    return asset_fingerprints


@dataclass(frozen=True, slots=True)
class _StoryboardImageInputs:
    """分镜图任务在解析 image lane 之前就能备齐的输入：项目快照、剧本 claim 与成立的生成输入。"""

    project: dict[str, Any]
    project_path: Path
    currency_resolver: ArtifactCurrencyResolver
    script_claim: ArtifactInputClaim
    generation_input: StoryboardImageInput


async def execute_storyboard_task(
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
    task_id: str | None = None,
) -> dict[str, Any]:
    script_file = payload.get("script_file")
    if not script_file:
        raise ValueError("script_file is required for storyboard task")

    if payload.get("prompt") is None:
        raise ValueError("prompt is required for storyboard task")

    def _load() -> _StoryboardImageInputs:
        _project = get_project_manager().load_project(project_name)
        _project_path = get_project_manager().get_project_path(project_name)
        _script = get_project_manager().load_script(project_name, script_file)
        _script_input = resolve_usable_episode_script_input(
            project_path=_project_path,
            project=_project,
            script=_script,
            script_filename=str(script_file),
        )
        require_factory_script(_project_path, _project, _script, _script_input.episode)
        _currency_resolver = active_artifact_currency_resolver(_project_path, _project)
        _generation_input = storyboard_image_input(
            _project,
            _script,
            episode=_script_input.episode,
            resource_id=resource_id,
            observation=project_input_observation(_project_path),
        )
        # 生成输入不成立时在解析供应商通道与付费之前失败，一次报出全部缺口。
        if isinstance(_generation_input, InputRefused):
            raise input_refusal_error(_generation_input)
        return _StoryboardImageInputs(
            project=_project,
            project_path=_project_path,
            currency_resolver=_currency_resolver,
            script_claim=_script_input.claim,
            generation_input=_generation_input,
        )

    inputs = await asyncio.to_thread(_load)
    project, project_path = inputs.project, inputs.project_path
    # 参考图先于提示词定型：backend 的参考图上限决定实际发出几张，编号只能按裁剪后的序列渲染，
    # 故 image lane 在此解析一次并沿用到提交，避免重解析落到上限不同的 backend。
    context = await resolve_generation_context(
        project_name,
        payload,
        project=project,
        project_path=project_path,
        user_id=user_id,
        image=ImageLaneRequest(generation_type="i2i" if inputs.generation_input.references else "t2i"),
    )

    def _bind_claims(
        claims: Sequence[ArtifactInputClaim], content_digests: Mapping[str, str]
    ) -> tuple[ArtifactInputClaim, ...]:
        return bind_artifact_input_claims_to_content_digests(
            resolver=inputs.currency_resolver,
            claims=claims,
            content_digests=content_digests,
        )

    def _freeze() -> FrozenGenerationInput:
        return inputs.generation_input.freeze(
            max_reference_images=context.image.max_reference_images,
            model=context.image.backend_model,
            bind_claims=_bind_claims,
        )

    artifact_path = f"storyboards/scene_{resource_id}.png"
    with await asyncio.to_thread(_freeze) as frozen:
        # 剧本 claim 排在最前，其后是参考图按冻结字节绑定的 claims。
        formal_claims = (inputs.script_claim, *frozen.claims)

        async def _assert_claims_usable() -> None:
            await asyncio.to_thread(assert_current_artifact_input_claims_usable, project_path, formal_claims)

        async def _pre_submit(_generator: Any) -> None:
            await _assert_claims_usable()

        def _build_commit(generator: Any, outcome_box: list[FormalImageCommitOutcome]) -> StagedImageCommit:
            return storyboard_formal_image_callback(
                project_name=project_name,
                script_file=str(script_file),
                resource_id=resource_id,
                artifact_path=artifact_path,
                prompt=frozen.prompt,
                versions=generator.versions,
                task_id=task_id,
                basis=frozen.basis,
                outcome_box=outcome_box,
                project_manager=get_project_manager(),
            )

        return await run_formal_image_task(
            project_name=project_name,
            payload=payload,
            project=project,
            user_id=user_id,
            task_id=task_id,
            frozen_references=frozen.references,
            context=context,
            plan=FormalImagePlan(
                resource_type="storyboards",
                resource_id=resource_id,
                artifact_path=artifact_path,
                prompt=frozen.prompt,
                aspect_ratio=inputs.generation_input.canvas_ratio,
                build_commit_callback=_build_commit,
                pre_submit=_pre_submit,
                before_submit=_assert_claims_usable,
                warnings=frozen.warnings,
            ),
        )


async def execute_tts_task(
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
    task_id: str | None = None,
) -> dict[str, Any]:
    """为一个 narrator-owned script unit 合成独立旁白配音。"""
    script_file = payload.get("script_file")

    def _prepare() -> tuple[dict, Path, str, Any, int, tuple[ArtifactInputClaim, ...]]:
        pm = get_project_manager()
        current_project = pm.load_project(project_name)
        project_path = pm.get_project_path(project_name)
        if script_file:
            script = pm.load_script(project_name, script_file)
            script_input = resolve_usable_episode_script_input(
                project_path=project_path,
                project=current_project,
                script=script,
                script_filename=str(script_file),
            )
            episode = script_input.episode
            require_factory_script(project_path, current_project, script, episode)
            items, id_field, kind = resolve_items(script)
            item = next(
                (
                    candidate
                    for candidate in items
                    if isinstance(candidate, dict) and str(candidate.get(id_field)) == str(resource_id)
                ),
                None,
            )
            if item is None:
                raise ValueError(f"segment not found: {resource_id}")
            admission = admit_script_unit(kind, item)
            text = canonical_narration_text(admission.preparation)
            if not admission.allowed:
                if not text:
                    raise ValueError(f"segment {resource_id} 无可合成的旁白文本")
                raise SpeechAdmissionError(admission)
            if not text:
                raise ValueError(f"segment {resource_id} 无可合成的旁白文本")
            return (
                current_project,
                project_path,
                text,
                admission.preparation,
                episode,
                (script_input.claim,),
            )

        raise ValueError("tts task 需要工厂派生剧本 payload.script_file")

    (
        project,
        project_path,
        text,
        preparation,
        episode,
        formal_input_claims,
    ) = await asyncio.to_thread(_prepare)

    # 旁白配音只按项目的 TTS 快照合成：项目改为后期配音或快照不完整时拒绝执行。
    settings = require_project_tts_generation(project)
    voice = settings.voice
    speed = settings.speed
    # 快照带完整的 provider/model，audio lane 按项目层解析即落到它；不传 payload，
    # 历史任务携带的供应商不得越过项目快照。
    ctx = await resolve_generation_context(
        project_name,
        None,
        project=project,
        user_id=user_id,
        audio=AudioLaneRequest(),
    )
    # 自定义后端可回退到默认型号；旁白快照不允许用另一个型号合成并登记为所选型号。
    if ctx.audio.backend_model != settings.model_id:
        raise NarrationConfigError("narration_tts_model_invalid")
    generator = ctx.generator
    basis = build_narration_audio_basis(preparation, settings)

    audio_rel = resource_relative_path("audio", resource_id)
    duration_seconds: float | None = None
    # 选片依据的解析失败在回调里发生、在外层消费，用单元素信箱传递而非 nonlocal 哨兵。
    tts_selection_errors: list[BaseException] = []
    selected_current = True

    class _TtsSelectionResolutionFailed(RuntimeError):
        pass

    async def _measure_staged(staged_path: Path) -> None:
        nonlocal duration_seconds
        try:
            measured_duration = await probe_existing_audio_duration_seconds(staged_path)
        except (Exception, asyncio.CancelledError) as exc:
            tts_selection_errors.append(exc)
            return
        if measured_duration is None or not math.isfinite(measured_duration) or measured_duration <= 0:
            tts_selection_errors.append(RuntimeError("generated narration audio duration is unavailable"))
            return
        duration_seconds = float(measured_duration)

    def _commit_staged(staged_path: Path, output_path: Path) -> int | PaidVersionCommit:
        nonlocal selected_current
        committed_preparation = preparation
        committed_episode = episode
        committed_basis = basis
        pm = get_project_manager()
        committed_outcome: list[PaidVersionCommit] = []
        should_select = False
        guarded_project: list[dict[str, Any]] = []

        version_metadata = {
            "tts_provider_id": settings.provider_id,
            "tts_model_id": settings.model_id,
            "tts_voice": settings.voice,
            "tts_speed": settings.speed,
            "tts_basis_digest": committed_basis.digest,
            "artifact_episode": committed_episode,
            "artifact_audio_basis": ArtifactBasisDescriptor.from_basis(committed_basis).to_dict(),
            "tts_actual_duration_seconds": duration_seconds,
            "execution_script_file": str(script_file),
        }

        def _archive_paid_history() -> PaidVersionCommit:
            return generator.versions.commit_staged_paid_version(
                resource_type="audio",
                resource_id=resource_id,
                prompt=text,
                staged_file=staged_path,
                current_file=output_path,
                select_current=False,
                **version_metadata,
            )

        if tts_selection_errors:
            committed_outcome.append(_archive_paid_history())
            selected_current = False
            return committed_outcome[-1]

        def _register_basis() -> None:
            register_narration_audio_transactionally(
                project_path=project_path,
                episode=committed_episode,
                preparation=committed_preparation,
                settings=settings,
            )

        def _activate(_script_path: Path) -> None:
            committed_outcome.append(
                generator.versions.commit_staged_paid_version(
                    resource_type="audio",
                    resource_id=resource_id,
                    prompt=text,
                    staged_file=staged_path,
                    current_file=output_path,
                    select_current=lambda: should_select,
                    on_select=_register_basis,
                    **version_metadata,
                )
            )

        def _same_script(_project: dict) -> str:
            guarded_project.append(_project)
            current_binding = resolve_episode_script_binding(_project, committed_episode, str(script_file))
            if current_binding is None:
                raise EpisodeScriptReboundError(f"episode {committed_episode} script binding changed before TTS commit")
            return current_binding

        try:
            with pm.locked_episode_script(
                project_name,
                _same_script,
                validate=False,
                on_commit=_activate,
            ) as current_script:
                if not guarded_project:
                    raise RuntimeError("TTS commit guard did not expose the current project")
                current_commit_settings = project_tts_settings(guarded_project[-1])
                if current_commit_settings is None:
                    tts_selection_errors.append(ValueError("project TTS snapshot is no longer complete"))
                    raise _TtsSelectionResolutionFailed
                items, id_field, current_kind = resolve_items(current_script)
                item = next(
                    (
                        candidate
                        for candidate in items
                        if isinstance(candidate, dict) and str(candidate.get(id_field)) == str(resource_id)
                    ),
                    None,
                )
                current_basis = None
                if not tts_selection_errors and item is not None:
                    current_admission = admit_script_unit(current_kind, item)
                    try:
                        current_basis = build_narration_audio_basis(
                            current_admission.preparation,
                            current_commit_settings,
                        )
                    except ValueError:
                        current_basis = None
                should_select = current_basis is not None and current_basis.digest == committed_basis.digest
                if should_select:
                    assert item is not None
                    assets = item.get("generated_assets")
                    if not isinstance(assets, dict):
                        assets = ProjectManager.create_generated_assets("drama")
                        item["generated_assets"] = assets
                    assets["narration_audio"] = audio_rel
                    pm.update_scene_status(item)
        except (EpisodeScriptReboundError, _TtsSelectionResolutionFailed):
            committed_outcome.append(_archive_paid_history())
        except BaseException as failure:
            if staged_path.is_file():
                try:
                    _archive_paid_history()
                except BaseException as archive_failure:
                    failure.add_note(f"paid TTS history archival also failed: {archive_failure}")
            raise

        if not committed_outcome:
            raise RuntimeError("paid TTS commit produced no version record")
        selected_current = committed_outcome[-1].selected
        return committed_outcome[-1]

    async def _before_submit() -> None:
        await asyncio.to_thread(
            assert_current_artifact_input_claims_usable,
            project_path,
            formal_input_claims,
        )

    _output_path, version = await generator.generate_audio_async(
        text=text,
        resource_id=resource_id,
        voice=voice,
        speed=speed,
        task_id=task_id,
        before_submit=_before_submit,
        before_commit=_measure_staged,
        commit_staged=_commit_staged,
        tts_provider_id=settings.provider_id,
        tts_model_id=settings.model_id,
        tts_voice=settings.voice,
        tts_speed=settings.speed,
        tts_basis_digest=basis.digest,
    )

    if tts_selection_errors:
        raise tts_selection_errors[-1]

    version_record: dict[str, Any] | None = None
    try:
        records = generator.versions.get_versions("audio", resource_id)["versions"]
        version_record = next(
            (record for record in reversed(records) if record.get("version") == version),
            None,
        )
        created_at = next(
            (record.get("created_at") for record in reversed(records) if record.get("version") == version),
            records[-1].get("created_at") if records else None,
        )
    except Exception:
        logger.warning("读取 TTS 版本入库时间失败 resource_id=%s", resource_id, exc_info=True)
        created_at = None

    return {
        "version": version,
        "file_path": (
            audio_rel if selected_current else version_record.get("file") if isinstance(version_record, dict) else None
        ),
        "created_at": created_at,
        "resource_type": "audio",
        "resource_id": resource_id,
        "duration_seconds": duration_seconds,
        "tts_basis_digest": basis.digest,
        "selected_current": selected_current,
    }


# character_name 经 validate_asset_name 校验合法字符，但不限长度；task_id 固定是 uuid4().hex
# （32 字节 ASCII）。若不裁剪，超长角色名 + 前缀/分隔符/task_id 拼出的 resource_id，
# 再叠加 VersionManager.add_version 的 "_v{n}_{timestamp}{ext}" 版本文件名后缀，
# 可能在 255 字节 NAME_MAX 的文件系统上让落盘/建版本失败。留出足够余量后裁剪角色名部分——
# resource_id 本身不需要人工从文件名反解角色名（仅内部拼接，无解析方），裁剪不影响正确性，
# 唯一性完全靠 task_id 保证。
_SAMPLE_ID_NAME_MAX_BYTES = 80


def _truncate_name_bytes(name: str, max_bytes: int) -> str:
    """按 UTF-8 字节数裁剪，裁剪点落在多字节字符中间时丢弃残缺字符而非产生非法编码。"""
    encoded = name.encode("utf-8")
    if len(encoded) <= max_bytes:
        return name
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def voice_sample_resource_id(character_name: str, task_id: str) -> str:
    """角色 TTS 试听样本在 ``audio/`` 下的资源 id（区别于旁白 segment id 命名空间）。

    生成产物只是待确认的预览件，落盘位置与旁白共用 ``audio/`` 目录但用固定前缀隔离，
    不会与旁白/解说的 segment id 冲突；只有 confirm 步骤才把音频提升为角色 reference_audio。

    带 ``task_id`` 而非只用角色名：同一角色前一次成功样本尚未确认时发起重新生成会产生
    新任务，若资源 id 只按角色名固定，新任务落盘会原地覆盖前一个已成功任务引用的文件——
    旧任务的 ``result.file_path`` 字段不变，但物理内容已变成新任务的（甚至是校验失败前
    写入的）字节；若前一个任务的 task_id 仍被别处持有（如另一浏览器标签页）并调用 confirm，
    会把错误内容误落为角色参考音频。每次生成用任务专属文件名，杜绝跨任务覆盖。
    """
    safe_name = _truncate_name_bytes(character_name, _SAMPLE_ID_NAME_MAX_BYTES)
    return f"voice_sample__{safe_name}__{task_id}"


async def execute_character_voice_sample_task(
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
    task_id: str | None = None,
) -> dict[str, Any]:
    """为角色参考音频候选生成一段 TTS 试听样本（预览用，不写入角色资产）。

    ``resource_id`` 是角色名；文本与音色显式来自 payload（``prompt`` = 待合成文本，
    ``voice`` = 用户选定的音色 id），不回落任何全局旁白配置。生成产物须满足与
    参考音频上传同口径的校验（格式经落盘扩展名固定为 wav、时长 2-10 秒、≤15MB）；
    校验失败直接抛错让任务落 failed，不静默放行不合规样本。
    """
    character_name = validate_asset_name(resource_id)
    text = payload.get("prompt")
    voice = payload.get("voice")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("voice sample 任务需要非空 payload.prompt（待合成文本）")
    if not isinstance(voice, str) or not voice.strip():
        raise ValueError("voice sample 任务需要 payload.voice（音色 id）")
    if not task_id:
        # 恒由 worker 经 execute_generation_task 传入队列任务自身 id；缺失说明调用方绕过了
        # 常规队列执行路径（如误从别处直接调用），而 sample_id 的跨任务隔离依赖它，fail-fast。
        raise ValueError("voice sample 任务需要 task_id")

    project = await asyncio.to_thread(get_project_manager().load_project, project_name)
    if resolve_asset_key(project.get("characters"), character_name) is None:
        # 与 execute_character_task 等其它执行器同口径：入队后、worker 取到任务前角色可能
        # 已被删除，执行前重新核实存在，避免花钱合成一段没有归属的孤儿预览。
        raise ValueError(f"character not found: {character_name}")
    ctx = await resolve_generation_context(
        project_name,
        payload,
        project=project,
        user_id=user_id,
        audio=AudioLaneRequest(),
    )
    generator = ctx.generator

    sample_id = voice_sample_resource_id(character_name, task_id)
    _, version = await generator.generate_audio_async(
        text=text.strip(),
        resource_id=sample_id,
        voice=voice.strip(),
        speed=None,
        task_id=task_id,
    )

    audio_rel = resource_relative_path("audio", sample_id)
    audio_abs = get_project_manager().get_project_path(project_name) / audio_rel

    def _read_bytes() -> bytes:
        return audio_abs.read_bytes()

    content = await asyncio.to_thread(_read_bytes)
    if len(content) > AUDIO_REFERENCE_MAX_BYTES:
        raise ValueError(f"生成的语音样本超过 {AUDIO_REFERENCE_MAX_BYTES // (1024 * 1024)}MB 限制")

    duration = await probe_audio_duration_seconds(content, audio_abs.suffix)
    if duration is not None and not (AUDIO_REFERENCE_MIN_SECONDS <= duration <= AUDIO_REFERENCE_MAX_SECONDS):
        raise ValueError(
            f"生成的语音样本时长 {duration:.1f}s 超出 "
            f"{AUDIO_REFERENCE_MIN_SECONDS:.0f}-{AUDIO_REFERENCE_MAX_SECONDS:.0f} 秒范围"
        )

    return {
        "version": version,
        "file_path": audio_rel,
        "resource_type": "audio",
        "resource_id": sample_id,
        "character_name": character_name,
        "voice": voice.strip(),
        "duration_seconds": duration,
    }


async def execute_video_task(
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    script_file: str | None = None,
    user_id: str = DEFAULT_USER_ID,
    task_id: str | None = None,
    claimed_provider_id: str | None = None,
    poll_timeout_seconds: int = DEFAULT_VIDEO_POLL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    payload_script_file = payload.get("script_file")
    if script_file is None and isinstance(payload_script_file, str):
        script_file = payload_script_file
    if not isinstance(script_file, str) or not script_file:
        raise ValueError("script_file is required for video task")

    def _load():
        _pm = get_project_manager()
        _project = _pm.load_project(project_name)
        _project_path = _pm.get_project_path(project_name)
        _script = _pm.load_script(project_name, script_file)
        _script_input = resolve_usable_episode_script_input(
            project_path=_project_path,
            project=_project,
            script=_script,
            script_filename=script_file,
        )
        require_factory_script(_project_path, _project, _script, _script_input.episode)
        _items, _id_field, _, _, _ = get_storyboard_items(_script)
        _resolved = find_storyboard_item(_items, _id_field, resource_id)
        _item = _resolved[0] if _resolved else {}
        return (
            _project,
            _project_path,
            _item,
            resolve_content_mode(_script, _project),
            resolve_script_kind(_script),
            _script,
            _script_input,
            _items,
            _id_field,
        )

    (
        project,
        project_path,
        item,
        content_mode,
        script_kind,
        _script,
        script_input,
        storyboard_items,
        storyboard_id_field,
    ) = await asyncio.to_thread(_load)
    # Queue execution re-materializes mutable visual intent from the current script unit. Direct/internal callers
    # without a task row retain the request-prompt fallback for compatibility with synchronous service tests.
    current_prompt = item.get("video_prompt") if isinstance(item, dict) else None
    prompt = current_prompt if task_id is not None else payload.get("prompt", current_prompt)
    if prompt is None:
        raise ValueError("current script unit is missing video_prompt")
    requested_visual_prompt = copy.deepcopy(prompt)
    # lane 归桶按项目生成模式求值，与提交入口（``generate_video``）同源：入口挡掉参考生视频后
    # 到达这里的项目恒为 i2v，但桶不在两处各硬编码一次，避免生成模式口径分叉。
    execution_payload = without_video_execution_identity(payload) if task_id is not None else payload
    ctx = await resolve_generation_context(
        project_name,
        execution_payload,
        project=project,
        user_id=user_id,
        video=VideoLaneRequest(
            generation_type=video_bucket_for_generation_mode(project.get("generation_mode")),
            route="storyboard",
        ),
    )
    generator = ctx.generator
    registry_provider_id = ctx.video.provider_model.provider_id
    if claimed_provider_id is not None and registry_provider_id != claimed_provider_id:
        raise DispatchProviderChanged(
            claimed_provider_id=claimed_provider_id,
            actual_provider_id=registry_provider_id,
        )
    if ctx.video.request_facts is None:
        raise RuntimeError("storyboard video lane is missing its request facts")
    request_facts = require_video_request_facts(ctx.video.request_facts)
    if (conflict := audio_switch_conflict(request_facts)) is not None:
        raise VideoRequestFactsError(conflict)
    requested_generate_audio = request_facts.requested_generate_audio
    model_name = ctx.video.backend_model
    supported_durations = list(request_facts.allowed_durations)
    resolution = request_facts.resolution

    artifact_episode = script_input.episode
    formal_input_claims: list[ArtifactInputClaim] = [script_input.claim]
    currency_resolver = active_artifact_currency_resolver(project_path, project)
    slots = await asyncio.to_thread(
        resolve_usable_storyboard_video_slots,
        project_path=project_path,
        project=project,
        episode=artifact_episode,
        resource_id=resource_id,
        items=storyboard_items,
        id_field=storyboard_id_field,
        resolver=currency_resolver,
        claims=formal_input_claims,
    )
    frozen_frames = await asyncio.to_thread(
        storyboard_frame_evidence,
        slots,
        project_path=project_path,
        items=storyboard_items,
        id_field=storyboard_id_field,
    )
    provider_start_image = slots.start.path
    if slots.start.kind == "video_last_frame":
        tail_path = safe_join(project_path, f".cache/video-continuity/{sha256_file(slots.start.path)}.png")
        if await extract_video_last_frame(slots.start.path, tail_path) is None:
            raise ValueError("previous video ending frame could not be extracted")
        provider_start_image = tail_path
    provider_end_image = slots.end.path if slots.end is not None else None
    reference_images = [frame.path for frame in slots.references]
    audio_inputs = await asyncio.to_thread(
        resolve_storyboard_audio_inputs,
        project=project,
        project_path=project_path,
        item=item,
        slots=slots,
        voice=VoiceRenderSettings.from_request_facts(request_facts),
    )
    reference_audio_files = list(audio_inputs.paths)
    asset_links = storyboard_asset_links(project, item, project_name)
    prompt_text = render_storyboard_video_prompt(
        prompt,
        item,
        content_mode=content_mode,
        voice_characters=(None if ctx.video.is_silent else (project.get("characters") or {}))
        if content_mode == "drama"
        else None,
        slots=slots,
        asset_links=asset_links,
        audio_speakers=audio_inputs.speakers,
    )
    if problems := keyframe_reference_problems(prompt_text, slots.cell_count):
        raise ValueError(f"video_prompt references unavailable keyframes: {', '.join(problems)}")
    aspect_ratio = get_aspect_ratio(project, "videos")
    seed = payload.get("seed")

    def _visual_basis_digest_for(storyboard_image: Path, end_frame_image: Path | None) -> str:
        return build_storyboard_video_visual_basis(
            prompt=requested_visual_prompt,
            storyboard_image=storyboard_image,
            end_frame_image=end_frame_image,
            aspect_ratio=aspect_ratio,
            provider_id=registry_provider_id,
            model_id=model_name,
            resolution=resolution,
            seed=seed,
            requested_generate_audio=requested_generate_audio,
            content_mode=content_mode,
            utterances=item.get("utterances") if content_mode == "drama" else None,
            has_utterances=content_mode == "drama" and "utterances" in item,
            voice_characters=(None if ctx.video.is_silent else project.get("characters"))
            if content_mode == "drama"
            else None,
            rendered_prompt=prompt_text,
            reference_images=reference_images,
            reference_audio_files=reference_audio_files,
            reference_audio_speakers=audio_inputs.speakers,
            reference_audio_targets=audio_inputs.targets,
            frame_evidence=frozen_frames,
        ).digest

    def _current_visual_basis_digest() -> str:
        return _visual_basis_digest_for(provider_start_image, provider_end_image)

    visual_basis_digest = await asyncio.to_thread(_current_visual_basis_digest)

    service_tier = payload.get("video_provider_settings", {}).get("service_tier", "default")

    # provider / model / 能力 / 分辨率均取自单次解析的 video lane：能力按 backend 实际身份
    # （registry provider_id + backend.model）查询，与实际要调用的 model 对齐——历史任务 payload
    # 携带 provider 覆盖、或自定义供应商目标 model 被禁用回退时，二者一致避免 duration 守卫误判
    # （用「项目默认 model 的能力」误判「实际调用的 model」）。解析/构造失败已在
    # resolve_generation_context 内原样上抛整次任务失败。
    # duration 解析收口于执行层：payload > project.default_duration > 请求事实的首个档位。
    # 用 ``is not None`` 而非 ``or`` 取 payload 值，避免显式 falsy 值被当作未设置。
    duration_seconds = (
        item.get("duration_seconds")
        if task_id is not None and isinstance(item, dict)
        else payload.get("duration_seconds")
    )
    if duration_seconds is None:
        duration_seconds = project.get("default_duration")
    if not duration_seconds:
        if request_facts.allowed_durations:
            duration_seconds = request_facts.allowed_durations[0]
        else:
            # 档位为空只在时长由端点固定时成立（其余情形请求事实已失败）：没有档位可借，取共享的
            # 规划篇幅默认值。
            duration_seconds = DEFAULT_PLANNED_DURATION_SECONDS

    if not isinstance(duration_seconds, (int, str)) or isinstance(duration_seconds, bool):
        raise ValueError("video request duration must be an integer or integer string")
    # 能力守卫：provider 解析之后的唯一权威家（见 ADR-0001）。安全解析交给守卫，
    # 此处不预先 int() 截断，避免把非整数秒静默修正成「碰巧合法」的值。
    assert_duration_supported(duration_seconds, supported_durations)
    duration_seconds = int(float(duration_seconds))

    async def _admit_before_submit() -> Mapping[str, object] | None:
        await asyncio.to_thread(assert_current_artifact_input_claims_usable, project_path, formal_input_claims)
        current_frames = await asyncio.to_thread(
            storyboard_frame_evidence,
            slots,
            project_path=project_path,
            items=storyboard_items,
            id_field=storyboard_id_field,
        )
        if current_frames != frozen_frames:
            raise ValueError("video frame inputs changed before submission")
        current_project = await asyncio.to_thread(get_project_manager().load_project, project_name)
        current_audio = await asyncio.to_thread(
            resolve_storyboard_audio_inputs,
            project=current_project,
            project_path=project_path,
            item=item,
            slots=slots,
            voice=VoiceRenderSettings.from_request_facts(request_facts),
        )
        current_rendered = render_storyboard_video_prompt(
            requested_visual_prompt,
            item,
            content_mode=content_mode,
            voice_characters=(None if ctx.video.is_silent else current_project.get("characters") or {})
            if content_mode == "drama"
            else None,
            slots=slots,
            asset_links=storyboard_asset_links(current_project, item, project_name),
            audio_speakers=current_audio.speakers,
        )
        if current_rendered != prompt_text or current_audio != audio_inputs:
            raise ValueError("video asset or voice inputs changed before submission")
        if any(sha256_file(path) != digest for path, digest in frozen_audio):
            raise ValueError("video reference audio changed before submission")
        return None

    frozen_audio = [(path, sha256_file(path)) for path in reference_audio_files]
    checkpoint_hook: Callable[[], Awaitable[Mapping[str, object] | None]] | None = _admit_before_submit
    staged_media: tuple[StagedProviderMedia, ...] = ()
    if task_id is not None:
        artifact_speech_preparation = admit_script_unit(script_kind, item).preparation
        artifact_speech = freeze_video_speech_facts(
            artifact_speech_preparation,
            characters=project.get("characters"),
            include_voice_styles=not ctx.video.is_silent,
            reference_audio_paths=dict(zip(audio_inputs.speakers, audio_inputs.paths, strict=True)),
        )
        artifact_duration_basis = build_video_duration_basis(duration_seconds)
        artifact_duration_tiers = tuple(
            sorted(
                {
                    duration_seconds,
                    *request_facts.allowed_durations,
                }
            )
        )
        media_inputs = [
            ProviderMediaInput(
                path=provider_start_image,
                role="start_image",
                logical_type="video" if slots.start.kind == "video_last_frame" else "storyboard",
                logical_name=slots.start.resource_id,
                kind=slots.start.kind
                if slots.start.keyframe_index is None
                else f"grid_cell_{slots.start.keyframe_index}",
            )
        ]
        media_inputs.extend(
            ProviderMediaInput(
                path=image,
                role="reference_image",
                logical_type="storyboard_grid",
                logical_name=f"{resource_id}:K{frame.keyframe_index}",
                kind=f"grid_cell_{frame.keyframe_index}",
            )
            for frame, image in zip(slots.references, reference_images, strict=True)
        )
        if provider_end_image is not None:
            media_inputs.append(
                ProviderMediaInput(
                    path=provider_end_image,
                    role="end_image",
                    logical_type="storyboard",
                    logical_name=resource_id,
                    kind=f"grid_cell_{slots.end.keyframe_index}" if slots.end is not None else "last_frame",
                )
            )
        media_inputs.extend(
            ProviderMediaInput(
                path=path,
                role="reference_audio",
                logical_type="character",
                logical_name=speaker,
                kind="voice",
                target_index=audio_inputs.targets[index] if audio_inputs.targets is not None else None,
            )
            for index, (speaker, path) in enumerate(zip(audio_inputs.speakers, reference_audio_files, strict=True))
        )
        staged_media = await stage_provider_media_for_task(project_path, task_id, tuple(media_inputs))
        try:
            formal_input_claims = list(
                bind_artifact_input_claims_to_content_digests(
                    resolver=currency_resolver,
                    claims=formal_input_claims,
                    content_digests={
                        **{media.source_locator: media.sha256 for media in staged_media},
                        **{
                            frame.path.relative_to(project_path).as_posix(): str(evidence["sha256"])
                            for (_, frame), evidence in zip(slots.frames, frozen_frames, strict=True)
                        },
                    },
                )
            )
            provider_start_image = safe_join(
                project_path,
                next(media.staged_locator for media in staged_media if media.role == "start_image"),
                require_file=True,
            )
            staged_end = next((media for media in staged_media if media.role == "end_image"), None)
            provider_end_image = (
                safe_join(project_path, staged_end.staged_locator, require_file=True)
                if staged_end is not None
                else None
            )
            reference_images = [
                safe_join(project_path, media.staged_locator, require_file=True)
                for media in staged_media
                if media.role == "reference_image"
            ]
            reference_audio_files = [
                safe_join(project_path, media.staged_locator, require_file=True)
                for media in staged_media
                if media.role == "reference_audio"
            ]
            visual_basis_digest = await asyncio.to_thread(
                _visual_basis_digest_for, provider_start_image, provider_end_image
            )
            artifact_visual_basis = await asyncio.to_thread(
                lambda: build_storyboard_video_artifact_visual_basis(
                    resource_id=resource_id,
                    visual_prompt=requested_visual_prompt,
                    storyboard_image=provider_start_image,
                    end_frame_image=provider_end_image,
                    aspect_ratio=aspect_ratio,
                    frame_evidence=frozen_frames,
                    asset_links=asset_links,
                )
            )
            artifact_video_basis = compose_video_artifact_basis(
                visual=artifact_visual_basis,
                speech=artifact_speech.basis,
                duration=artifact_duration_basis,
            )

            async def _checkpoint_before_submit() -> Mapping[str, object]:
                await _admit_before_submit()
                artifact_currency = VideoArtifactCurrencyFacts(
                    episode=artifact_episode,
                    request_duration_seconds=duration_seconds,
                    visual_basis=artifact_visual_basis,
                    speech_basis=artifact_speech.basis,
                    duration_basis=artifact_duration_basis,
                    video_basis=artifact_video_basis,
                    voice_style_speakers=artifact_speech.voice_style_speakers,
                    duration_tiers=artifact_duration_tiers,
                    reference_image_limit=None,
                    parent_version=generator.versions.get_current_version("videos", resource_id),
                )
                checkpoint = StoryboardSubmissionCheckpoint.create(
                    task_id=task_id,
                    project_name=project_name,
                    script_file=script_file,
                    unit_id=resource_id,
                    generation_type="i2v",
                    provider_id=ctx.video.provider_model.provider_id,
                    provider_model_id=ctx.video.provider_model.model_id,
                    backend_model_id=ctx.video.backend_model,
                    endpoint_guard=ctx.video.endpoint,
                    prompt=prompt_text,
                    duration_seconds=duration_seconds,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    generate_audio=requested_generate_audio,
                    service_tier=service_tier,
                    seed=seed,
                    visual_basis_digest=visual_basis_digest,
                    artifact_currency=artifact_currency,
                    media=staged_media,
                    reference_audio_targets=audio_inputs.targets,
                )
                await get_generation_queue().persist_execution_checkpoint(
                    task_id,
                    checkpoint.to_json(),
                    checkpoint.provider_id,
                )
                return checkpoint_version_metadata(checkpoint)

            checkpoint_hook = _checkpoint_before_submit
        except BaseException:
            await asyncio.to_thread(cleanup_staged_provider_media, project_path, task_id)
            raise

    artifact_committer = (
        VideoArtifactCommitter(
            project_manager=get_project_manager(),
            project_name=project_name,
            project_path=project_path,
            versions=generator.versions,
            resource_type="videos",
            resource_id=resource_id,
            prompt=prompt_text,
        )
        if task_id is not None
        else None
    )
    #: backend 执行期产生的非阻断提示（如 ComfyUI 一次产出多个文件），随结果落进任务 result。
    warnings: list[dict[str, Any]] = []
    try:
        await _admit_before_submit()
        _output_path, version, _, video_uri = await generator.generate_video_async(
            prompt=prompt_text,
            resource_type="videos",
            resource_id=resource_id,
            start_image=provider_start_image,
            end_image=provider_end_image,
            reference_images=reference_images,
            reference_audio_files=reference_audio_files,
            reference_audio_targets=list(audio_inputs.targets) if audio_inputs.targets is not None else None,
            aspect_ratio=aspect_ratio,
            duration_seconds=duration_seconds,
            resolution=resolution,
            task_id=task_id,
            before_submit=checkpoint_hook,
            formal_output=task_id is not None,
            before_formal_commit=artifact_committer.prepare_selection if artifact_committer is not None else None,
            commit_formal_output=artifact_committer,
            seed=seed,
            service_tier=service_tier,
            visual_basis_digest=visual_basis_digest,
            generate_audio=requested_generate_audio,
            poll_timeout_seconds=poll_timeout_seconds,
            warnings=warnings,
        )

        async def _finalize() -> dict[str, Any]:
            return await finalize_video_task(
                project_name=project_name,
                script_file=script_file,
                project_path=project_path,
                resource_id=resource_id,
                version=version,
                video_uri=video_uri,
                generator=generator,
                warnings=warnings,
            )

        return await complete_video_artifact_commit(
            committer=artifact_committer,
            versions=generator.versions,
            resource_type="videos",
            resource_id=resource_id,
            version=version,
            video_uri=video_uri,
            finalize=_finalize,
            warnings=warnings,
        )
    finally:
        if artifact_committer is not None:
            await artifact_committer.release_admission_guard()
        if task_id is not None:
            await asyncio.to_thread(cleanup_staged_provider_media, project_path, task_id)


async def finalize_video_task(
    *,
    project_name: str,
    script_file: str,
    project_path: Path,
    resource_id: str,
    version: int,
    video_uri: str | None,
    generator: Any,
    warnings: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Normal + resume 共用的 finalize 逻辑：写 scene asset + 抽缩略图 + 返回 result dict。

    ``warnings`` 为空时结果不带该键：读侧按「有没有这个键」判断这一版带不带提示，恒写一个空
    列表会让每一版都多出一个永远为空的形状。
    """

    def _update_video_metadata():
        get_project_manager().update_scene_asset(
            project_name=project_name,
            script_filename=script_file,
            scene_id=resource_id,
            asset_type="video_clip",
            asset_path=f"videos/scene_{resource_id}.mp4",
        )
        if video_uri:
            get_project_manager().update_scene_asset(
                project_name=project_name,
                script_filename=script_file,
                scene_id=resource_id,
                asset_type="video_uri",
                asset_path=video_uri,
            )

    await run_sync_transaction(_update_video_metadata)

    video_file = project_path / f"videos/scene_{resource_id}.mp4"
    thumbnail_file = project_path / f"thumbnails/scene_{resource_id}.jpg"
    if await extract_video_thumbnail(video_file, thumbnail_file):
        await run_sync_transaction(
            get_project_manager().update_scene_asset,
            project_name=project_name,
            script_filename=script_file,
            scene_id=resource_id,
            asset_type="video_thumbnail",
            asset_path=f"thumbnails/scene_{resource_id}.jpg",
        )
    else:
        thumbnail_file.unlink(missing_ok=True)

    created_at = await asyncio.to_thread(
        lambda: generator.versions.get_versions("videos", resource_id)["versions"][-1]["created_at"]
    )

    result: dict[str, Any] = {
        "version": version,
        "file_path": f"videos/scene_{resource_id}.mp4",
        "created_at": created_at,
        "resource_type": "videos",
        "resource_id": resource_id,
        "video_uri": video_uri,
    }
    if warnings:
        result["warnings"] = list(warnings)
    return result


async def execute_asset_sheet_task(
    kind: str,
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
    task_id: str | None = None,
) -> dict[str, Any]:
    """资产图（角色、场景、道具、商品）：描述取自存储的条目，参考图与依据取自生成输入。"""

    def _load() -> tuple[dict[str, Any], Path, AssetSheetInput]:
        _project = get_project_manager().load_project(project_name)
        _project_path = get_project_manager().get_project_path(project_name)
        _generation_input = asset_sheet_input(
            _project,
            asset_type=kind,
            name=resource_id,
            observation=project_input_observation(_project_path),
        )
        # 生成输入不成立时在解析供应商通道与付费之前失败，一次报出全部缺口。
        if isinstance(_generation_input, InputRefused):
            raise input_refusal_error(_generation_input)
        return _project, _project_path, _generation_input

    project, project_path, generation_input = await asyncio.to_thread(_load)
    context = await resolve_generation_context(
        project_name,
        payload,
        project=project,
        project_path=project_path,
        user_id=user_id,
        image=ImageLaneRequest(generation_type="i2i" if generation_input.references else "t2i"),
    )

    def _freeze() -> FrozenGenerationInput:
        return generation_input.freeze(
            max_reference_images=context.image.max_reference_images,
            model=context.image.backend_model,
            bind_claims=_originals_have_no_claims,
        )

    with await asyncio.to_thread(_freeze) as frozen:
        return await run_asset_sheet_image_task(
            asset_type=kind,
            project_name=project_name,
            resource_id=resource_id,
            payload=payload,
            user_id=user_id,
            task_id=task_id,
            project=project,
            frozen=frozen,
            context=context,
            project_manager=get_project_manager(),
        )


def _originals_have_no_claims(
    claims: Sequence[ArtifactInputClaim], _content_digests: Mapping[str, str]
) -> tuple[ArtifactInputClaim, ...]:
    """资产图的参考图只有作者上传的原图，原图不是登记产物，没有 claim 要复核。"""

    if claims:
        raise ValueError("asset sheet references carry no artifact input claims")
    return ()


async def execute_character_task(
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
    task_id: str | None = None,
) -> dict[str, Any]:
    return await execute_asset_sheet_task(
        "character", project_name, resource_id, payload, user_id=user_id, task_id=task_id
    )


async def execute_scene_task(
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
    task_id: str | None = None,
) -> dict[str, Any]:
    return await execute_asset_sheet_task("scene", project_name, resource_id, payload, user_id=user_id, task_id=task_id)


async def execute_prop_task(
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
    task_id: str | None = None,
) -> dict[str, Any]:
    return await execute_asset_sheet_task("prop", project_name, resource_id, payload, user_id=user_id, task_id=task_id)


def _shot_grid_scene_opening(image_prompt: object) -> str:
    """分镜提示词 scene 的导语段，作为跨格基调进「风格与锚点」。

    纯文本形态的 scene 没有镜头序列结构，取不到导语就留空——逐格文本同样来自 scene，
    缺导语不影响出图。
    """

    scene = image_prompt.get("scene") if isinstance(image_prompt, Mapping) else image_prompt
    if not isinstance(scene, str) or not scene.strip():
        return ""
    try:
        return parse_scene_shots(scene).opening
    except ValueError:
        return ""


async def execute_shot_grid_task(
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Execute a per-shot reference-material (keyframe grid) generation task.

    逐格镜头文本以 ``ShotGrid.prompt`` 为优先来源（单分镜弹窗填写，ADR 0099 D-2）；``prompt``
    为空时回退到 ``image_prompt.scene`` 现解析（整集批量走这条，不预存 prompt）。出图前把解析
    结果回填 ``cells`` 只作本次留证。已确认的宫格允许按同一规格重做——规格由 PUT / generate
    的 409 锁死（R7）。
    """

    project_path = get_project_manager().get_project_path(project_name)
    manager = ShotGridManager(project_path)
    grid = manager.get(resource_id)
    if grid is None:
        raise ValueError(f"shot grid not found: {resource_id}")

    grid.status = "generating"
    grid.confirmed_at = None
    grid.error_message = None
    grid.updated_at = datetime.now(UTC).isoformat()
    manager.save(grid)

    outcome_box: list[FormalImageCommitOutcome] = []
    try:
        project = await asyncio.to_thread(get_project_manager().load_project, project_name)
        script = await asyncio.to_thread(get_project_manager().load_script, project_name, grid.script_file)
        await asyncio.to_thread(require_factory_script, project_path, project, script, grid.episode)
        # 逐格文本：ShotGrid.prompt 优先（单分镜弹窗填写），否则现解析 scene（整集批量）。
        frames = parse_grid_prompt_cells(grid.prompt) if grid.prompt else []
        if grid.prompt.strip() and len(frames) != grid.rows * grid.cols:
            raise ValueError(f"shot grid prompt requires exactly {grid.rows * grid.cols} cells")
        if not frames:
            items, id_field, _, _, _ = get_storyboard_items(script)
            shot = next((item for item in items if str(item.get(id_field, "")) == grid.shot_id), None)
            if shot is None:
                raise ValueError(f"shot not found in script: {grid.shot_id}")
            try:
                frames = list(parse_scene_shots(shot_scene_text(shot)).shots)
            except ValueError as exc:
                raise ValueError(f"shot grid scene has no shot sequence: {grid.shot_id}") from exc
        # 留证按本次实际使用的文本回填：换过整图之后，旧的切格结果全部作废。
        grid.cells = shot_grid_cells(frames, grid.rows, grid.cols)
        items, id_field, _, _, _ = get_storyboard_items(script)
        shot = next((item for item in items if str(item.get(id_field, "")) == grid.shot_id), None)
        if shot is None:
            raise ValueError(f"shot not found in script: {grid.shot_id}")
        grid.scene_revision = scene_revision_of(shot_scene_text(shot))
        grid.updated_at = datetime.now(UTC).isoformat()
        manager.save(grid)

        generation_input = storyboard_image_input(
            project,
            script,
            episode=grid.episode,
            resource_id=grid.shot_id,
            observation=project_input_observation(project_path),
        )
        if isinstance(generation_input, InputRefused):
            raise input_refusal_error(generation_input)
        # 冻结生图时的画布比例（16:9 / 9:16），供确认切分按该比例 center-crop 对齐单格。
        grid.video_aspect_ratio = generation_input.canvas_ratio
        manager.save(grid)
        context = await resolve_generation_context(
            project_name,
            payload,
            project=project,
            project_path=project_path,
            user_id=user_id,
            image=ImageLaneRequest(generation_type="i2i" if generation_input.references else "t2i"),
        )
        generator = context.generator
        resolver = active_artifact_currency_resolver(project_path, project)
        with generation_input.freeze(
            max_reference_images=context.image.max_reference_images,
            model=context.image.backend_model,
            bind_claims=lambda claims, digests: bind_artifact_input_claims_to_content_digests(
                resolver=resolver,
                claims=claims,
                content_digests=digests,
            ),
        ) as frozen:
            prompt = build_shot_grid_prompt(
                rows=grid.rows,
                cols=grid.cols,
                canvas_ratio=generation_input.canvas_ratio,
                cells=frames,
                style=generation_input.semantics.style,
                style_description=generation_input.semantics.style_description,
                scene_opening=_shot_grid_scene_opening(generation_input.semantics.image_prompt),
                references=frozen.references.visual_references,
            )
            await asyncio.to_thread(assert_current_artifact_input_claims_usable, project_path, frozen.claims)
            await generator.generate_image_async(
                prompt=prompt,
                resource_type="shot_grids",
                resource_id=grid.id,
                reference_images=frozen.references.reference_images,
                # 整图比例按规格与画布比例算出，不再是裸画布比例；单格比例只随提示词下发。
                aspect_ratio=grid_aspect_ratio_for(grid.rows, grid.cols, generation_input.canvas_ratio),
                image_size=context.image.resolution,
                formal_output=True,
                task_id=task_id,
                commit_formal_output=shot_grid_formal_image_callback(
                    project_path=project_path,
                    grid_manager=manager,
                    grid=grid,
                    resource_id=grid.id,
                    prompt=prompt,
                    versions=generator.versions,
                    task_id=task_id,
                    basis=frozen.basis,
                    outcome_box=outcome_box,
                ),
            )
        return {
            "file_path": f"shot_grids/{grid.id}.png",
            "resource_type": "shot_grids",
            "resource_id": grid.id,
            "status": "completed",
        }
    except (Exception, asyncio.CancelledError) as exc:
        failed = manager.get(resource_id)
        if failed is not None and failed.status != "completed":
            failed.status = "failed"
            failed.error_message = str(exc) or "Reference material task cancelled"
            failed.updated_at = datetime.now(UTC).isoformat()
            manager.save(failed)
        raise


_TASK_EXECUTORS = {
    "storyboard": execute_storyboard_task,
    "video": execute_video_task,
    "tts": execute_tts_task,
    "voice_sample": execute_character_voice_sample_task,
    "character": execute_character_task,
    "scene": execute_scene_task,
    "prop": execute_prop_task,
    "shot_grid": execute_shot_grid_task,
    "image_edit": execute_image_edit_task,
    DERIVATIVE_TASK_TYPE: execute_character_derivative_task,
}


async def execute_generation_task(task: dict[str, Any], *, claimed_provider_id: str | None = None) -> dict[str, Any]:
    task_type = task.get("task_type")
    project_name = task.get("project_name")
    resource_id = str(task.get("resource_id"))
    payload = task.get("payload") or {}
    user_id = task.get("user_id", DEFAULT_USER_ID)
    queue_task_id = task.get("task_id")
    # worker 派发时把当下的全局设置写进任务字典；非 worker 调用方（测试 / 直生）落回缺省。
    video_poll_timeout_seconds = int(task.get("video_poll_timeout_seconds", DEFAULT_VIDEO_POLL_TIMEOUT_SECONDS))

    if not project_name:
        raise ValueError("task.project_name is required")
    if not task_type:
        raise ValueError("task.task_type is required")

    executor = _TASK_EXECUTORS.get(task_type)
    if task_type.startswith("text_"):
        from server.tool_runtime import execute_queued_text_task

        return await execute_queued_text_task(task)
    if is_render_task_type(task_type):
        return await execute_render_task(task)
    if executor is None:
        raise ValueError(f"unsupported task_type: {task_type}")

    with project_change_source("worker"):
        # 能力类异常（Image/VideoCapabilityError、ReferencePayloadFloorError）原样上抛：
        # worker 的 encode_task_failure_message 按 code + params 落库，渲染留到读侧
        # Translator，同一失败任务按 Accept-Language 显示 zh/en/vi。
        if task_type == "video":
            result = await executor(
                project_name,
                resource_id,
                payload,
                script_file=task.get("script_file"),
                user_id=user_id,
                task_id=queue_task_id,
                claimed_provider_id=claimed_provider_id,
                poll_timeout_seconds=video_poll_timeout_seconds,
            )
        else:
            result = await executor(project_name, resource_id, payload, user_id=user_id, task_id=queue_task_id)
        # 成功事件发出失败时不回滚产物：已写入的产物保持有效，异常照常上抛。
        emit_generation_success_batch(
            task_type=task_type,
            project_name=project_name,
            resource_id=resource_id,
            payload=payload,
        )
        return result
