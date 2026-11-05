"""RunningHub workflow 的视频 / 图像调用通道。

与 ComfyUI 通道共用定义构造（``build_workflow``）、绑定能力推导与产物容器校验；差别只在运输层：
上传走 ``/openapi/v2/media/upload/binary``，提交走 ``/task/openapi/create``，轮询走
``/task/openapi/outputs``，产物是公网 URL。完整 workflow 与 ``nodeInfoList`` 同时发送：
前者保证节点裁剪 / 旁路生效，后者兜住 RunningHub 会强制覆盖的种子。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from arcreel_market_core.comfyui.bindings import targets_of
from arcreel_market_core.video_backend_contract import (
    ProviderResponseStage,
    ResumeExpiredError,
    VideoCapabilities,
    VideoGenerationRequest,
    VideoGenerationResult,
)
from lib.backends.artifact_download_guard import artifact_http_client
from lib.backends.backend_runtime import (
    ProviderJobIdPersistenceMixin,
    notify_provider_response,
    notify_provider_response_to,
)
from lib.backends.image_backends.base import (
    ImageCapability,
    ImageGenerationRequest,
    ImageGenerationResult,
)
from lib.custom_provider.comfyui.capabilities import takes_reference_images
from lib.custom_provider.comfyui.comfyui_backend import binding_video_capabilities
from lib.custom_provider.comfyui.comfyui_client import upload_filename
from lib.custom_provider.comfyui.comfyui_execution import HTTP_TIMEOUT_SECONDS
from lib.custom_provider.comfyui.comfyui_image_backend import (
    IMAGE_POLL_TIMEOUT_SECONDS,
    binding_image_capabilities,
)
from lib.custom_provider.comfyui.request_builder import (
    BuiltWorkflow,
    MediaInputs,
    build_workflow,
    reference_audio_targets,
)

from .client import RunningHubClient
from .execution import PickedArtifact, RunningHubExecution
from .failures import WORKFLOW_NOT_FOUND, RunningHubError
from .node_info import build_node_info_list

logger = logging.getLogger(__name__)

MEDIA_TYPE_VIDEO = "video"
MEDIA_TYPE_IMAGE = "image"


def _workflow_id_of(definition: Mapping[str, Any]) -> str:
    workflow_id = str(definition.get("workflow_id") or "").strip()
    if not workflow_id:
        raise RunningHubError(WORKFLOW_NOT_FOUND, detail="workflow_id missing from endpoint definition")
    return workflow_id


def _instance_type_of(definition: Mapping[str, Any]) -> str | None:
    """RunningHub 显存实例：default 24G / plus 48G / ultra 84G；非法值退回平台默认。"""

    value = str(definition.get("instance_type") or "").strip().lower()
    return value if value in {"default", "plus", "ultra"} else None


def _targets(raw: object) -> list[Mapping[str, Any]]:
    return [target for target in raw if isinstance(target, Mapping)] if isinstance(raw, list) else []


class RunningHubVideoBackend(ProviderJobIdPersistenceMixin):
    """把一份 workflow 端点定义跑成一条分镜视频。"""

    def __init__(
        self,
        *,
        provider_id: str,
        model: str,
        base_url: str,
        api_key: str,
        definition: Mapping[str, Any],
        job_label: str | None = None,
    ) -> None:
        self._provider = provider_id
        self._model = model
        self._definition = definition
        self._job_label = job_label
        self._base_url = base_url
        self._api_key = api_key
        self._client = RunningHubClient(base_url=base_url, api_key=api_key)
        self._execution = RunningHubExecution(
            client=self._client,
            media_type=MEDIA_TYPE_VIDEO,
            definition=definition,
        )

    @property
    def name(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._model

    @property
    def video_capabilities(self) -> VideoCapabilities:
        return binding_video_capabilities(self._definition)

    async def generate(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        job_label = request.task_id or self._job_label or uuid4().hex
        async with artifact_http_client(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True) as http:
            media = await self._upload_media(http, request, job_label=job_label)
            built = build_workflow(
                self._definition,
                prompt=request.prompt,
                aspect_ratio=request.aspect_ratio,
                duration_seconds=request.duration_seconds,
                resolution=request.resolution,
                media=media,
                seed=request.seed,
            )
            task_id = await self._client.create_task(
                http,
                workflow_id=_workflow_id_of(self._definition),
                node_info_list=build_node_info_list(self._definition, built),
                workflow=built.workflow,
                instance_type=_instance_type_of(self._definition),
                record=lambda stage, body: notify_provider_response(request, stage, body),
            )
            await self._persist_provider_job_id(request, task_id, provider=self._provider, endpoint=self._base_url)
            picked = await self._execution.fetch_artifact(
                http,
                task_id,
                output_path=request.output_path,
                poll_timeout_seconds=request.poll_timeout_seconds,
                record=lambda stage, body: notify_provider_response(request, stage, body),
            )
            return self._result(request, task_id, picked, built=built)

    async def resume_video(self, job_id: str, request: VideoGenerationRequest) -> VideoGenerationResult:
        submitted = request.submitted_base_url
        if submitted and submitted != self._base_url:
            return await self._bound_to(submitted).resume_video(job_id, request)
        async with artifact_http_client(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True) as http:
            picked = await self._execution.fetch_artifact(
                http,
                job_id,
                output_path=request.output_path,
                poll_timeout_seconds=request.poll_timeout_seconds,
                record=lambda stage, body: notify_provider_response(request, stage, body),
                lost_error=lambda task_id: ResumeExpiredError(job_id=task_id, provider=self._provider),
            )
            return self._result(request, job_id, picked, built=None)

    def _bound_to(self, base_url: str) -> RunningHubVideoBackend:
        return RunningHubVideoBackend(
            provider_id=self._provider,
            model=self._model,
            base_url=base_url,
            api_key=self._api_key,
            definition=self._definition,
            job_label=self._job_label,
        )

    def _result(
        self,
        request: VideoGenerationRequest,
        task_id: str,
        picked: PickedArtifact,
        *,
        built: BuiltWorkflow | None,
    ) -> VideoGenerationResult:
        warnings: tuple[Mapping[str, Any], ...] = ()
        if picked.count > 1:
            warnings = (
                {
                    "key": "runninghub_multiple_outputs",
                    "params": {"count": picked.count, "filename": picked.filename},
                },
            )
        provenance: dict[str, Any] = {}
        if built is not None:
            provenance["workflow_sha256"] = built.workflow_sha256
        if picked.artifact.consume_coins:
            provenance["runninghub_consume_coins"] = picked.artifact.consume_coins
        if picked.artifact.task_cost_time:
            provenance["runninghub_task_cost_time"] = picked.artifact.task_cost_time
        return VideoGenerationResult(
            video_path=request.output_path,
            provider=self._provider,
            model=self._model,
            duration_seconds=request.duration_seconds,
            video_uri=self._execution.view_url(picked.artifact),
            task_id=task_id,
            seed=built.seed if built is not None else None,
            generate_audio=request.generate_audio,
            provenance=provenance or None,
            warnings=warnings,
        )

    async def _upload_media(
        self,
        http: httpx.AsyncClient,
        request: VideoGenerationRequest,
        *,
        job_label: str,
    ) -> MediaInputs:
        bindings: Mapping[str, Any] = self._definition.get("bindings") or {}
        start = await self._upload_one(http, request.start_image, bindings, "start_image", job_label=job_label)
        end = await self._upload_one(http, request.end_image, bindings, "end_image", job_label=job_label)
        slots = len(_targets(bindings.get("reference_images")))
        references: list[str] = []
        for index, path in enumerate((request.reference_images or [])[:slots]):
            references.append(
                await self._client.upload_file(
                    http,
                    path,
                    filename=upload_filename(job_label, "reference_images", path, index),
                )
            )
        audio_slots = len(reference_audio_targets(self._definition.get("workflow") or {}))
        audios: list[str] = []
        for index, path in enumerate((request.reference_audio_files or [])[:audio_slots]):
            audios.append(
                await self._client.upload_file(
                    http,
                    path,
                    filename=upload_filename(job_label, "reference_audios", path, index),
                )
            )
        return MediaInputs(
            start_image=start,
            end_image=end,
            reference_images=tuple(references),
            reference_audios=tuple(audios),
        )

    async def _upload_one(
        self,
        http: httpx.AsyncClient,
        path: Path | None,
        bindings: Mapping[str, Any],
        key: str,
        *,
        job_label: str,
    ) -> str | None:
        if path is None or not _targets(bindings.get(key)):
            return None
        return await self._client.upload_file(http, path, filename=upload_filename(job_label, key, path))


class RunningHubImageBackend:
    """把一份 workflow 端点定义跑成一张图。"""

    def __init__(
        self,
        *,
        provider_id: str,
        model: str,
        base_url: str,
        api_key: str,
        definition: Mapping[str, Any],
        job_label: str | None = None,
    ) -> None:
        self._provider = provider_id
        self._model = model
        self._definition = definition
        self._job_label = job_label
        self._client = RunningHubClient(base_url=base_url, api_key=api_key)
        self._execution = RunningHubExecution(
            client=self._client,
            media_type=MEDIA_TYPE_IMAGE,
            definition=definition,
        )

    @property
    def name(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> set[ImageCapability]:
        return set(binding_image_capabilities(self._definition))

    @property
    def max_reference_images(self) -> int:
        return len(targets_of((self._definition.get("bindings") or {}).get("reference_images")))

    async def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        job_label = self._job_label or uuid4().hex
        async with artifact_http_client(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True) as http:
            media = await self._upload_media(http, request, job_label=job_label)
            built = build_workflow(
                self._definition,
                prompt=request.prompt,
                aspect_ratio=request.aspect_ratio,
                resolution=request.image_size,
                media=media,
                seed=request.seed,
            )

            async def record(stage: ProviderResponseStage, body: object) -> None:
                await notify_provider_response_to(request.on_provider_response, stage, body)

            task_id = await self._client.create_task(
                http,
                workflow_id=_workflow_id_of(self._definition),
                node_info_list=build_node_info_list(self._definition, built),
                workflow=built.workflow,
                instance_type=_instance_type_of(self._definition),
                record=record,
            )
            picked = await self._execution.fetch_artifact(
                http,
                task_id,
                output_path=request.output_path,
                poll_timeout_seconds=IMAGE_POLL_TIMEOUT_SECONDS,
                record=record,
            )
            return ImageGenerationResult(
                image_path=request.output_path,
                provider=self._provider,
                model=self._model,
                image_uri=self._execution.view_url(picked.artifact),
                seed=built.seed,
            )

    async def _upload_media(
        self,
        http: httpx.AsyncClient,
        request: ImageGenerationRequest,
        *,
        job_label: str,
    ) -> MediaInputs:
        if not takes_reference_images(self._definition):
            return MediaInputs()
        slots = self.max_reference_images
        references: list[str] = []
        for index, reference in enumerate(request.reference_images[:slots]):
            path = Path(reference.path)
            references.append(
                await self._client.upload_file(
                    http,
                    path,
                    filename=upload_filename(job_label, "reference_images", path, index),
                )
            )
        return MediaInputs(reference_images=tuple(references))
