"""一次已提交的 RunningHub 执行：轮询到终态、判成败、取件落盘、取消与超时。

RunningHub 的 ``outputs`` 一次调用同时给状态与产物；轮询层用 ``poll_with_retry`` 共用 ComfyUI
通道的失败预算与退避。产物是公网 URL，下载不带 APIKEY——对象存储/COS 不认识它，也不该收到它。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from lib.backends.artifact_download_guard import ARTIFACT_MAX_BYTES_BY_MEDIA_TYPE, artifact_http_client
from lib.backends.backend_runtime import (
    faststart_video_artifact,
    poll_with_retry,
    should_retry_poll,
    stream_to_file,
    with_artifact_retry,
)
from lib.backends.container_sniff import CONTAINER_HEAD_BYTES
from lib.custom_provider.comfyui.artifacts import ARTIFACT_SUFFIXES_BY_MEDIA_TYPE, container_matches

from .client import RunningHubArtifact, RunningHubClient, RunningHubTaskState, artifact_suffix
from .failures import (
    JOB_LOST,
    OUTPUT_CONTAINER_MISMATCH,
    OUTPUT_MISSING,
    OUTPUT_TYPE_MISMATCH,
    TASK_FAILED,
    RunningHubError,
)

logger = logging.getLogger(__name__)

#: 叫停远端用的超时。比生成路径短：任务已取消后不该再拖住 worker 关停。
_STOP_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class PickedArtifact:
    """这次执行取走的产物，以及候选总数。"""

    artifact: RunningHubArtifact
    count: int

    @property
    def filename(self) -> str:
        return Path(self.artifact.url.split("?", 1)[0]).name

    @property
    def url(self) -> str:
        return self.artifact.url


class RunningHubExecution:
    """围绕一个 ``taskId`` 的轮询、取件与取消。"""

    def __init__(
        self,
        *,
        client: RunningHubClient,
        media_type: str,
        definition: Mapping[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._media_type = media_type
        self._definition = definition or {}

    async def fetch_artifact(
        self,
        http: httpx.AsyncClient,
        task_id: str,
        *,
        output_path: Path,
        poll_timeout_seconds: float,
        record: Any | None = None,
        lost_error: Any | None = None,
    ) -> PickedArtifact:
        """轮询到终态、挑出产物、下载；本地不要这次执行了就顺手停掉远端。"""
        try:
            state = await self._poll_to_terminal(
                http,
                task_id,
                poll_timeout_seconds=poll_timeout_seconds,
                record=record,
                lost_error=lost_error,
            )
            picked = self._pick(state)
            if record is not None:
                await record("result", {"artifact": _artifact_dict(picked.artifact), "count": picked.count})
            await self._download(http, picked, output_path, poll_timeout_seconds=poll_timeout_seconds)
            await self._verify_container(output_path, picked)
            return picked
        except (asyncio.CancelledError, TimeoutError):
            await self.stop_remote(task_id)
            raise

    def view_url(self, artifact: RunningHubArtifact) -> str:
        return artifact.url

    # ------------------------------------------------------------------ 轮询与取件

    async def _poll_to_terminal(
        self,
        http: httpx.AsyncClient,
        task_id: str,
        *,
        poll_timeout_seconds: float,
        record: Any | None,
        lost_error: Any | None,
    ) -> RunningHubTaskState:
        async def poll_once() -> RunningHubTaskState:
            state = await self._client.query_outputs(http, task_id)
            if record is not None:
                await record(
                    "poll",
                    {
                        "code": state.code,
                        "msg": state.msg,
                        "artifacts": [_artifact_dict(item) for item in state.artifacts],
                    },
                )
            if state.is_missing:
                if lost_error is not None:
                    raise lost_error(task_id)
                raise RunningHubError(JOB_LOST, task_id=task_id)
            if state.is_failed:
                raise _task_failure(state)
            if not state.is_success and not state.is_running:
                raise RunningHubError(TASK_FAILED, detail=state.msg or f"code={state.code}")
            return state

        return await poll_with_retry(
            poll_fn=poll_once,
            is_done=lambda state: state.is_success,
            is_failed=lambda _state: None,
            max_wait=poll_timeout_seconds,
            retry_if=should_retry_poll,
            label="runninghub",
        )

    def _pick(self, state: RunningHubTaskState) -> PickedArtifact:
        if not state.artifacts:
            raise RunningHubError(OUTPUT_MISSING, media_type=self._media_type)
        allowed = ARTIFACT_SUFFIXES_BY_MEDIA_TYPE.get(self._media_type, frozenset())
        matching = [item for item in state.artifacts if _suffix_of(item) in allowed]
        if not matching:
            first = state.artifacts[0]
            raise RunningHubError(
                OUTPUT_TYPE_MISMATCH,
                filename=Path(first.url.split("?", 1)[0]).name,
                media_type=self._media_type,
            )
        if len(state.artifacts) > 1:
            logger.warning(
                "RunningHub 产物共 %d 个，取: %s",
                len(state.artifacts),
                matching[0].url,
            )
        return PickedArtifact(artifact=matching[0], count=len(state.artifacts))

    async def _download(
        self,
        http: httpx.AsyncClient,
        picked: PickedArtifact,
        output_path: Path,
        *,
        poll_timeout_seconds: float,
    ) -> None:
        async def once() -> None:
            await stream_to_file(
                http,
                picked.artifact.url,
                output_path,
                max_bytes=ARTIFACT_MAX_BYTES_BY_MEDIA_TYPE[self._media_type],
            )

        await with_artifact_retry(
            once,
            label="runninghub artifact download",
            max_wait=poll_timeout_seconds,
        )
        if self._media_type == "video":
            await faststart_video_artifact(output_path)

    async def _verify_container(self, output_path: Path, picked: PickedArtifact) -> None:
        head = await asyncio.to_thread(_read_head, output_path)
        if container_matches(head, self._media_type):
            return
        await asyncio.to_thread(output_path.unlink, True)
        raise RunningHubError(
            OUTPUT_CONTAINER_MISMATCH,
            filename=picked.filename,
            media_type=self._media_type,
        )

    # ------------------------------------------------------------------ 叫停远端

    async def stop_remote(self, task_id: str) -> None:
        """best-effort 取消；失败只记日志。"""
        try:
            async with artifact_http_client(timeout=_STOP_TIMEOUT_SECONDS) as http:
                await self._client.cancel(http, task_id)
        except Exception:
            logger.warning("RunningHub 远端叫停失败 task_id=%s", task_id, exc_info=True)


def _task_failure(state: RunningHubTaskState) -> RunningHubError:
    reason = state.failed_reason or {}
    node = str(reason.get("node_name") or reason.get("node_id") or reason.get("node") or "").strip()
    detail = str(reason.get("exception_message") or reason.get("message") or state.msg or "").strip()
    if node:
        detail = f"{detail}（节点 {node}）" if detail else f"节点 {node}"
    return RunningHubError(TASK_FAILED, detail=detail or "task failed")


def _artifact_dict(artifact: RunningHubArtifact) -> dict[str, Any]:
    return {
        "url": artifact.url,
        "file_type": artifact.file_type,
        "node_id": artifact.node_id,
        "consume_coins": artifact.consume_coins,
        "task_cost_time": artifact.task_cost_time,
    }


def _suffix_of(artifact: RunningHubArtifact) -> str:
    suffix = artifact_suffix(artifact.url)
    if suffix:
        return suffix
    file_type = artifact.file_type.strip().lower()
    return f".{file_type}" if file_type else ""


def _read_head(path: Path) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(CONTAINER_HEAD_BYTES)
