"""RunningHub OpenAPI 的 HTTP 面：上传素材、提交 workflow、查结果、取消、账户与工作流读取。

RunningHub 的 workflow OpenAPI 与本地 ComfyUI 不同：提交要 ``workflowId`` + ``nodeInfoList``，
轮询走 ``/task/openapi/outputs``，凭据是 ``Authorization: Bearer`` + body 里的 ``apiKey``。
本模块只产协议形状与结构化状态，业务终态由 execution / backend 判。

出站一律走调用方给的 ``artifact_http_client``（目的地校验）；本模块不另开客户端。
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from arcreel_market_core.video_backend_contract import ProviderResponseStage
from lib.backends.backend_runtime import (
    request_with_scoped_credentials,
    should_retry_submit,
    submit_post,
)
from lib.backends.http_status_errors import redacted_status_error
from lib.infra.logging_utils import format_kwargs_for_log
from lib.infra.retry import retry_async

from .failures import (
    AUTH_FAILED,
    INSUFFICIENT_COINS,
    TASK_FAILED,
    UPLOAD_FAILED,
    WORKFLOW_NOT_FOUND,
    RunningHubError,
)

logger = logging.getLogger(__name__)

#: 留痕回调：与 ComfyUI 通道同形（阶段 + 原文），由 backend 接到任务的诊断列上。
RecordResponse = Callable[[ProviderResponseStage, object], Awaitable[None]]

#: 生成路径上每个 HTTP 请求的超时。
HTTP_TIMEOUT_SECONDS = 60

#: RunningHub 的 HTTP 200 里承载业务码的字段。0 成功；804 运行中、813 排队中、805 失败、
#: 807 任务不存在。这些是官方文档里的固定值，不在本地另立枚举。
_CODE_SUCCESS = 0
_CODE_RUNNING = 804
_CODE_QUEUED = 813
_CODE_FAILED = 805
_CODE_NOT_FOUND = 807

#: 上传接口。v2 走 Bearer，旧 ``/task/openapi/upload`` 需要表单 apiKey，已不采用。
_UPLOAD_PATH = "/openapi/v2/media/upload/binary"
_CREATE_PATH = "/task/openapi/create"
_OUTPUTS_PATH = "/task/openapi/outputs"
_CANCEL_PATH = "/task/openapi/cancel"
_ACCOUNT_PATH = "/uc/openapi/accountStatus"
_WORKFLOW_JSON_PATH = "/api/openapi/getJsonApiFormat"


def normalize_base_url(base_url: str) -> str:
    """补协议 + 去尾斜杠；空地址抛错。"""
    stripped = base_url.strip().rstrip("/")
    if not stripped:
        raise ValueError("RunningHub 调用端点需要服务地址")
    if "://" not in stripped:
        stripped = f"https://{stripped}"
    return stripped


@dataclass(frozen=True)
class RunningHubArtifact:
    """``outputs`` 里的一条产物。"""

    url: str
    file_type: str
    node_id: str | None = None
    consume_coins: str | None = None
    task_cost_time: str | None = None


@dataclass(frozen=True)
class RunningHubTaskState:
    """一次 ``outputs`` 查询的结构化结果。"""

    code: int
    msg: str
    artifacts: tuple[RunningHubArtifact, ...] = ()
    failed_reason: Mapping[str, Any] | None = None
    usage: Mapping[str, Any] | None = None

    @property
    def is_success(self) -> bool:
        return self.code == _CODE_SUCCESS

    @property
    def is_running(self) -> bool:
        return self.code in {_CODE_RUNNING, _CODE_QUEUED}

    @property
    def is_failed(self) -> bool:
        return self.code == _CODE_FAILED

    @property
    def is_missing(self) -> bool:
        return self.code == _CODE_NOT_FOUND


@dataclass(frozen=True)
class RunningHubAccount:
    """账户探针结果。"""

    remain_coins: str | None
    running_tasks: str | None
    api_type: str | None


def _body_or_text(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return response.text


def _as_mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


class RunningHubClient:
    """接一个 RunningHub 账号（base URL + APIKEY）。"""

    def __init__(self, *, base_url: str, api_key: str) -> None:
        self._base_url = normalize_base_url(base_url)
        self._api_key = api_key

    @property
    def base_url(self) -> str:
        return self._base_url

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    async def _post(
        self,
        http: httpx.AsyncClient,
        path: str,
        body: Mapping[str, Any],
    ) -> httpx.Response:
        return await request_with_scoped_credentials(
            http,
            "POST",
            self._url(path),
            headers=self._headers(),
            json=body,
            auth_query=None,
        )

    async def account_status(self, http: httpx.AsyncClient) -> RunningHubAccount:
        """免费账户探针；失败按 auth / general 分类。"""
        try:
            response = await self._post(http, _ACCOUNT_PATH, {"apikey": self._api_key})
            response.raise_for_status()
            payload = _body_or_text(response)
        except httpx.HTTPStatusError as exc:
            raise self._status_error(exc) from None
        except (httpx.HTTPError, ValueError) as exc:
            raise RunningHubError(TASK_FAILED, detail=str(exc)) from exc
        body = _as_mapping(payload) or {}
        code = _positive_int(body.get("code"))
        if code != _CODE_SUCCESS:
            raise self._code_error(code, str(body.get("msg") or ""))
        data = _as_mapping(body.get("data")) or {}
        return RunningHubAccount(
            remain_coins=_str_or_none(data.get("remainCoins")),
            running_tasks=_str_or_none(data.get("currentTaskCounts")),
            api_type=_str_or_none(data.get("apiType")),
        )

    async def upload_file(
        self,
        http: httpx.AsyncClient,
        path: Path,
        *,
        filename: str,
    ) -> str:
        """上传素材，返回 RunningHub 的 ``fileName``（节点里填的值，不是下载链接）。"""
        try:
            payload = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            raise RunningHubError(UPLOAD_FAILED, detail=str(exc)) from exc
        try:
            response = await request_with_scoped_credentials(
                http,
                "POST",
                self._url(_UPLOAD_PATH),
                headers=self._headers(),
                json=None,
                auth_query=None,
                files={"file": (filename, payload, _mime_of(path))},
            )
            response.raise_for_status()
            body = _body_or_text(response)
        except httpx.HTTPStatusError as exc:
            raise self._status_error(exc) from None
        except (httpx.HTTPError, ValueError) as exc:
            raise RunningHubError(UPLOAD_FAILED, detail=str(exc)) from exc
        mapping = _as_mapping(body) or {}
        code = _positive_int(mapping.get("code"))
        if code != _CODE_SUCCESS:
            raise RunningHubError(
                UPLOAD_FAILED,
                detail=str(mapping.get("message") or mapping.get("msg") or f"code={code}"),
            )
        data = _as_mapping(mapping.get("data")) or {}
        file_name = _str_or_none(data.get("fileName"))
        if not file_name:
            raise RunningHubError(UPLOAD_FAILED, detail="upload response did not return fileName")
        logger.info(
            "RunningHub 素材已上传: %s",
            format_kwargs_for_log({"file": filename, "stored": file_name}),
        )
        return file_name

    async def create_task(
        self,
        http: httpx.AsyncClient,
        *,
        workflow_id: str,
        node_info_list: Sequence[Mapping[str, Any]],
        workflow: Mapping[str, Any] | None = None,
        record: RecordResponse | None = None,
    ) -> str:
        """提交一笔 workflow 任务，返回 ``taskId``。"""
        body: dict[str, Any] = {
            "apiKey": self._api_key,
            "workflowId": workflow_id,
            # RunningHub 默认走公共队列；企业级独占 Key 只有显式打开个人队列才不排公共队。
            # 共享/普通 Key 即使传 true 也只是回落到公共队列，不会影响提交。
            "usePersonalQueue": True,
        }
        if node_info_list:
            body["nodeInfoList"] = list(node_info_list)
        if workflow is not None:
            # 官方定义该字段是 JSON 字符串，不是对象。
            body["workflow"] = json.dumps(workflow, ensure_ascii=False, separators=(",", ":"))

        async def post() -> httpx.Response:
            return await self._post(http, _CREATE_PATH, body)

        try:
            response = await retry_async(
                lambda: submit_post(post, provider="runninghub"),
                retry_if=should_retry_submit,
            )
        except httpx.HTTPStatusError as exc:
            raise self._status_error(exc) from None
        payload = _body_or_text(response)
        if record is not None:
            await record("submit", payload)
        mapping = _as_mapping(payload)
        if mapping is None:
            raise RunningHubError(TASK_FAILED, detail="create response was not JSON")
        code = _positive_int(mapping.get("code"))
        if code != _CODE_SUCCESS:
            raise self._code_error(code, str(mapping.get("msg") or ""))
        data = _as_mapping(mapping.get("data")) or {}
        self._raise_node_errors(data.get("promptTips"))
        task_id = _str_or_none(data.get("taskId"))
        if not task_id:
            raise RunningHubError(TASK_FAILED, detail="create response did not contain taskId")
        logger.info(
            "RunningHub 已提交: %s",
            format_kwargs_for_log({"task_id": task_id, "workflow_id": workflow_id}),
        )
        return task_id

    async def query_outputs(self, http: httpx.AsyncClient, task_id: str) -> RunningHubTaskState:
        """查询一次任务结果；不在本层重试。"""
        try:
            response = await self._post(http, _OUTPUTS_PATH, {"apiKey": self._api_key, "taskId": task_id})
            response.raise_for_status()
            payload = _body_or_text(response)
        except httpx.HTTPStatusError as exc:
            raise self._status_error(exc) from None
        except (httpx.HTTPError, ValueError) as exc:
            raise RunningHubError(TASK_FAILED, detail=str(exc)) from exc
        mapping = _as_mapping(payload)
        if mapping is None:
            raise RunningHubError(TASK_FAILED, detail="outputs response was not JSON")
        code = _positive_int(mapping.get("code"))
        msg = str(mapping.get("msg") or "")
        if code is None:
            raise RunningHubError(TASK_FAILED, detail="outputs response did not contain code")
        if code == _CODE_FAILED:
            data = _as_mapping(mapping.get("data")) or {}
            raw_reason = data.get("failedReason")
            failed_reason = _as_mapping(raw_reason)
            if failed_reason is None and isinstance(raw_reason, str) and raw_reason.strip():
                # 失败原因在 code=805 时既可能是对象（含 node_name/exception_message），
                # 也可能是一句纯文本（「重试 3 次后失败，连接超时…」）。只认对象会把真正
                # 的原因丢掉，只把 msg 的「APIKEY_TASK_STATUS_ERROR」抛给用户。
                failed_reason = {"message": raw_reason.strip()}
            return RunningHubTaskState(code=code, msg=msg, failed_reason=failed_reason, usage=data)
        if code != _CODE_SUCCESS:
            return RunningHubTaskState(code=code, msg=msg)
        raw_data = mapping.get("data")
        artifacts = _artifacts_of(raw_data)
        usage = _as_mapping(raw_data[0]) if isinstance(raw_data, list) and raw_data else None
        return RunningHubTaskState(code=code, msg=msg, artifacts=artifacts, usage=usage)

    async def cancel(self, http: httpx.AsyncClient, task_id: str) -> None:
        """best-effort 取消；失败只记日志，本地状态机不动。"""
        try:
            response = await self._post(http, _CANCEL_PATH, {"apiKey": self._api_key, "taskId": task_id})
            response.raise_for_status()
            body = _body_or_text(response)
            mapping = _as_mapping(body) or {}
            code = _positive_int(mapping.get("code"))
            if code not in {_CODE_SUCCESS, _CODE_NOT_FOUND, None}:
                logger.info("RunningHub 取消返回 code=%s msg=%s", code, mapping.get("msg"))
        except Exception:
            logger.warning("RunningHub 远端取消失败 task_id=%s", task_id, exc_info=True)

    async def fetch_workflow_json(self, http: httpx.AsyncClient, workflow_id: str) -> Mapping[str, Any]:
        """按 workflowId 取回 API 格式 workflow。"""
        try:
            response = await self._post(
                http,
                _WORKFLOW_JSON_PATH,
                {"apiKey": self._api_key, "workflowId": workflow_id},
            )
            response.raise_for_status()
            payload = _body_or_text(response)
        except httpx.HTTPStatusError as exc:
            raise self._status_error(exc) from None
        except (httpx.HTTPError, ValueError) as exc:
            raise RunningHubError(TASK_FAILED, detail=str(exc)) from exc
        mapping = _as_mapping(payload) or {}
        code = _positive_int(mapping.get("code"))
        if code != _CODE_SUCCESS:
            raise self._code_error(code, str(mapping.get("msg") or ""))
        data = _as_mapping(mapping.get("data")) or {}
        prompt = data.get("prompt")
        if isinstance(prompt, str):
            try:
                prompt = json.loads(prompt)
            except ValueError as exc:
                raise RunningHubError(TASK_FAILED, detail="workflow JSON is not valid JSON") from exc
        if not isinstance(prompt, Mapping) or not prompt:
            raise RunningHubError(WORKFLOW_NOT_FOUND, detail=f"workflow {workflow_id} returned no prompt")
        return prompt

    # ------------------------------------------------------------------ 错误分流

    def _status_error(self, exc: httpx.HTTPStatusError) -> RunningHubError:
        status = exc.response.status_code
        if status in {401, 403}:
            return RunningHubError(AUTH_FAILED)
        return RunningHubError(TASK_FAILED, detail=str(redacted_status_error(exc)))

    def _code_error(self, code: int | None, message: str) -> RunningHubError:
        lowered = message.lower()
        if code in {401, 403} or "apikey" in lowered or "api key" in lowered:
            return RunningHubError(AUTH_FAILED)
        if "workflow" in lowered and any(token in lowered for token in ("not found", "不存在", "无权", "not exist")):
            return RunningHubError(WORKFLOW_NOT_FOUND, detail=message or "workflow not found")
        if "coin" in lowered or "余额" in message or "insufficient" in lowered:
            return RunningHubError(INSUFFICIENT_COINS, detail=message)
        return RunningHubError(TASK_FAILED, detail=message or f"code={code}")

    @staticmethod
    def _raise_node_errors(prompt_tips: object) -> None:
        tips: Mapping[str, Any] | None = None
        if isinstance(prompt_tips, str) and prompt_tips.strip():
            try:
                parsed = json.loads(prompt_tips)
            except ValueError:
                parsed = None
            tips = parsed if isinstance(parsed, Mapping) else None
        elif isinstance(prompt_tips, Mapping):
            tips = prompt_tips
        if tips is None:
            return
        node_errors = tips.get("node_errors")
        if isinstance(node_errors, Mapping) and node_errors:
            first = next(iter(node_errors.items()))
            raise RunningHubError(
                "runninghub_node_errors",
                nodes=len(node_errors),
                summary=_node_error_summary(first),
            )


def _artifacts_of(raw: object) -> tuple[RunningHubArtifact, ...]:
    if not isinstance(raw, list):
        return ()
    found: list[RunningHubArtifact] = []
    for item in raw:
        mapping = _as_mapping(item)
        if mapping is None:
            continue
        url = _str_or_none(mapping.get("fileUrl"))
        file_type = _str_or_none(mapping.get("fileType"))
        if not url or not file_type:
            continue
        found.append(
            RunningHubArtifact(
                url=url,
                file_type=file_type,
                node_id=_str_or_none(mapping.get("nodeId")),
                consume_coins=_str_or_none(mapping.get("consumeCoins")),
                task_cost_time=_str_or_none(mapping.get("taskCostTime")),
            )
        )
    return tuple(found)


def _node_error_summary(first: tuple[object, object]) -> str:
    node_id, detail = first
    if not isinstance(detail, Mapping):
        return str(node_id)
    errors = detail.get("errors")
    message = ""
    if isinstance(errors, list) and errors and isinstance(errors[0], Mapping):
        message = str(errors[0].get("message") or "").strip()
    class_type = str(detail.get("class_type") or "").strip()
    label = class_type or str(node_id)
    return f"{label}: {message}" if message else label


def _mime_of(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _str_or_none(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def artifact_suffix(url: str) -> str:
    """产物 URL 的扩展名（小写，含点）；读不出来给空串。"""
    return Path(urlparse(url).path).suffix.lower()
