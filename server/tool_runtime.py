"""Host-independent ArcReel tool handlers and their typed call contract."""

from __future__ import annotations

import asyncio
import copy
import functools
import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from lib.artifacts.artifact_activation import ArtifactCurrencyResolver, active_artifact_currency_resolver
from lib.backends.providers import CallPurpose
from lib.backends.text_backends.base import (
    TextGenerationRequest as TextBackendRequest,
)
from lib.backends.text_backends.base import (
    TextOutputTruncatedError,
    TextTaskType,
)
from lib.backends.text_generator import TextGenerator
from lib.config.resolver import ConfigResolver, caps_generation_mode, video_bucket_for_generation_mode
from lib.db import async_session_factory
from lib.episode.episode_ids import describe_episode_for_agent
from lib.episode.episode_paths import (
    DRAMA_SCRIPT_PLAN_QUARANTINE_FILENAME,
    NARRATION_SCRIPT_PLAN_QUARANTINE_FILENAME,
    REFERENCE_VIDEO_PROMPT_AUTHORING_QUARANTINE_FILENAME,
    REFERENCE_VIDEO_SCRIPT_PLAN_FILENAME,
    REFERENCE_VIDEO_SCRIPT_PLAN_LEGACY_FILENAME,
    REFERENCE_VIDEO_SCRIPT_PLAN_QUARANTINE_FILENAME,
    SCRIPT_PLAN_FILENAMES,
    SCRIPT_PLAN_LEGACY_FILENAMES,
)
from lib.episode.episode_target_duration import (
    EPISODE_TARGET_DURATION_FIELD,
    MAX_EPISODE_TARGET_DURATION,
    MIN_EPISODE_TARGET_DURATION,
    is_valid_episode_target_duration,
)
from lib.generation.generation_batch import (
    GenerationBatchReadModel,
    GenerationBatchRequestedItem,
    GenerationBatchRequestSnapshot,
    build_generation_batch_admission,
)
from lib.generation.generation_queue import (
    ActiveTaskRequestConflict,
    GenerationBatchNotFound,
    GenerationQueue,
    cleanup_fresh_generation_batch,
    get_generation_queue,
    text_task_request_facts,
)
from lib.generation.generation_queue_client import (
    BatchTaskResult,
    TaskSpec,
    WorkerOfflineError,
    enqueue_task_only,
    submit_generation_batch,
    wait_for_task,
)
from lib.generation.generation_result import (
    GenerationAction,
    GenerationBatchResult,
    GenerationProblem,
    GenerationSelectionMode,
    GenerationTargetState,
    encode_generation_problem,
    enqueue_problem,
    migration_problem,
    problem_from_task_failure,
)
from lib.generation.video_request_facts import VideoRequestFactsError, VideoRequestFactsFailure
from lib.infra.async_thread import run_sync_transaction as _run_sync_transaction
from lib.infra.content_digest import prefixed, prefixed_canonical_json_digest
from lib.infra.data_root_layout import DataRootLayout
from lib.infra.path_safety import safe_join
from lib.infra.schema_guards import is_str
from lib.project.asset_merge import (
    MERGEABLE_ASSET_TYPES,
    AssetMergeEpisodeImpact,
    AssetMergeNotFoundError,
    AssetMergeRejectedError,
    AssetMergeReport,
)
from lib.project.asset_types import ASSET_SPECS
from lib.project.project_manager import ProjectManager
from lib.project.project_migration_failure import (
    MigrationFailureRecord,
    ProjectMigrationError,
    load_migration_failure,
)
from lib.project.project_migration_guard import project_migration_failure
from lib.project.project_migrations import migrate_project_with_verdict
from lib.script.script_batch_edit import (
    ScriptBatchEditCommand,
    ScriptBatchEditLocation,
    ScriptBatchEditor,
    ScriptBatchEditProblem,
    ScriptBatchEditResult,
    script_revision,
)
from lib.script.script_editor import (
    ScriptEditError,
    insert_segment,
    move_segment,
    patch_field,
    remove_segment,
    resolve_items,
    split_segment,
)
from lib.speech.character_voice import VALID_CHARACTER_VOICE_BINDINGS
from lib.speech.narration_config import (
    POST_PRODUCTION,
    NarrationConfigError,
    NarrationDelivery,
    validate_project_narration_config,
)
from lib.speech.speech_composition import SpeechProblemCode
from lib.workflow.workflow_plan import (
    WorkflowPlan,
    WorkflowPlanRequest,
)
from lib.workflow.workflow_state import WorkflowRequestError
from server.request_types import (
    EPISODE_ID_DESCRIPTION,
    PositiveEpisode,
)
from server.services.admission.prompt_preview import ItemPromptPreview, ScriptItemNotFound, preview_item_prompts
from server.services.admission.video_batch_admission import generation_action_for
from server.services.project.episode_id_records import recorded_episode_ids_on
from server.services.project.narration_settings import NarrationSettingsInput, new_project_narration_fields
from server.services.project.workflow_planner import WorkflowPlanner
from server.services.tasks.video_caps import (
    capability_request_facts,
    duration_constraints_payload,
)
from server.text_generation import (
    MAX_INSTRUCTIONS_LEN,
    OperationNotAdmittedError,
    PromptOverwriteRequiredError,
    TextGenerationError,
    TextGenerationRequest,
    TextGenerationResult,
    VideoRequestFactsUnavailableError,
    prompt_authoring_preflight,
)
from server.text_generation import (
    generate_episode_script as generate_episode_script_handler,
)


@dataclass(frozen=True, slots=True)
class ToolRequest[RequestT]:
    value: RequestT


@dataclass(frozen=True, slots=True)
class ProjectScope:
    project_name: str
    data_root: Path


type BatchWaiter = Callable[..., Awaitable[tuple[list[BatchTaskResult], list[BatchTaskResult]]]]


@dataclass(frozen=True, slots=True)
class CallerContext:
    """调用方身份与宿主。

    ``source`` 决定长任务阻塞还是即返：``embedded`` 由 ``batch_waiter`` 入队并等到批次终态，
    ``mcp`` 与 Web 的 ``webui`` 提交后立即返回批次句柄、不需要等待器。
    """

    user_id: str
    source: Literal["embedded", "mcp", "webui"]
    batch_waiter: BatchWaiter | None = None

    def waiting_with(self, **options: Any) -> CallerContext:
        """给等待器绑定额外选项（如 ``on_enqueued`` / ``stop_on_failure``）；没有等待器时原样返回。"""
        if self.batch_waiter is None:
            return self
        return replace(self, batch_waiter=functools.partial(self.batch_waiter, **options))


@dataclass(frozen=True, slots=True)
class Services:
    projects: ProjectManager
    workflow_planner: WorkflowPlanner
    capabilities: ConfigResolver
    queue: GenerationQueue = field(default_factory=get_generation_queue)

    @classmethod
    def defaults(cls, projects: ProjectManager) -> Services:
        """生产缺省协作者：该项目管理器的工作流规划器、读数据库的配置解析器与全局生成队列。"""
        return cls(
            projects=projects,
            workflow_planner=WorkflowPlanner(projects),
            capabilities=ConfigResolver(async_session_factory),
        )


@dataclass(frozen=True, slots=True)
class ToolProblem:
    code: str
    detail: str
    action: str | None = None
    params: dict[str, Any] | None = None

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        del mode
        payload: dict[str, Any] = {"code": self.code, "detail": self.detail}
        if self.action is not None:
            payload["action"] = self.action
        if self.params is not None:
            payload["params"] = self.params
        return payload


@dataclass(frozen=True, slots=True)
class ToolOutcome[ResultT]:
    value: ResultT | None = None
    problem: ToolProblem | None = None


class GenerationBatchToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_id: str = Field(min_length=1, description="生成批次 id，取自生成工具返回的 generation_batch.batch_id")


@dataclass(frozen=True, slots=True)
class MediaGenerationSubmission:
    batch: GenerationBatchReadModel
    successes: list[BatchTaskResult] | None = None
    failures: list[BatchTaskResult] | None = None


async def submit_media_generation(
    *,
    scope: ProjectScope,
    caller: CallerContext,
    services: Services,
    operation: str,
    preflight: GenerationBatchResult,
    pending_ids: list[str],
    specs: list[TaskSpec],
    states: dict[str, GenerationTargetState] | None = None,
    admission: dict[str, dict[str, Any]] | None = None,
    dependencies: Mapping[str, str] | None = None,
) -> MediaGenerationSubmission:
    """提交一批媒体生成：远程调用方即返批次句柄，内嵌调用方经 ``caller.batch_waiter`` 等到批次终态。

    ``dependencies`` 记下批内先后：某单元要等同批另一单元的任务成功后才执行（其 ``TaskSpec``
    经 ``dependency_resource_id`` 指向那个单元），前置失败时它不提交给供应商。
    """
    requested, blocked = build_generation_batch_admission(
        preflight=preflight,
        pending_ids=pending_ids,
        states=states,
        admission=admission,
        dependencies=dependencies,
    )
    if caller.source != "embedded":
        batch, _enqueued, _enqueue_failures = await submit_generation_batch(
            project_name=scope.project_name,
            operation=operation,
            requested=requested,
            blocked=blocked,
            specs=specs,
            source=caller.source,
            user_id=caller.user_id,
            queue=services.queue,
        )
        return MediaGenerationSubmission(batch=batch)
    batch_id = await services.queue.create_generation_batch(
        project_name=scope.project_name,
        operation=operation,
        requested=requested,
        blocked=blocked,
        source=caller.source,
        user_id=caller.user_id,
    )
    try:
        waiter = caller.batch_waiter
        if waiter is None:
            raise ValueError("embedded media generation requires a batch waiter")
        if specs:
            successes, failures = await waiter(
                project_name=scope.project_name,
                specs=specs,
                batch_id=batch_id,
                queue=services.queue,
                user_id=caller.user_id,
            )
        else:
            successes, failures = [], []
        settled = await services.queue.get_generation_batch(
            project_name=scope.project_name,
            batch_id=batch_id,
            user_id=caller.user_id,
        )
        return MediaGenerationSubmission(batch=settled, successes=successes, failures=failures)
    except BaseException as failure:
        await cleanup_fresh_generation_batch(
            services.queue,
            project_name=scope.project_name,
            batch_id=batch_id,
            user_id=caller.user_id,
            failure=failure,
        )
        raise


async def get_generation_batch(
    request: ToolRequest[GenerationBatchToolRequest],
    scope: ProjectScope,
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[Any]:
    try:
        resolver: ArtifactCurrencyResolver | None = None
        try:
            project = await asyncio.to_thread(services.projects.load_project, scope.project_name)
            resolver = active_artifact_currency_resolver(
                services.projects.get_project_path(scope.project_name),
                project,
            )
        except ProjectMigrationError:
            pass
        result = await services.queue.get_generation_batch(
            project_name=scope.project_name,
            batch_id=request.value.batch_id,
            user_id=_caller.user_id,
            resolver=resolver,
        )
    except GenerationBatchNotFound as exc:
        return ToolOutcome(problem=ToolProblem("generation_batch_not_found", str(exc)))
    except Exception as exc:
        return ToolOutcome(problem=ToolProblem("internal_error", f"get_generation_batch 失败: {exc}"))
    return ToolOutcome(value=result)


async def cancel_generation_batch(
    request: ToolRequest[GenerationBatchToolRequest],
    scope: ProjectScope,
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[Any]:
    try:
        result = await services.queue.cancel_generation_batch(
            project_name=scope.project_name,
            batch_id=request.value.batch_id,
            user_id=_caller.user_id,
        )
    except GenerationBatchNotFound as exc:
        return ToolOutcome(problem=ToolProblem("generation_batch_not_found", str(exc)))
    except Exception as exc:
        return ToolOutcome(problem=ToolProblem("internal_error", f"cancel_generation_batch 失败: {exc}"))
    return ToolOutcome(value=result)


class PatchUpdateOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    op: Literal["update"]
    id: str = Field(min_length=1, description="要修改的条目 id（segment_id / scene_id / shot_id / unit_id）")
    fields: dict[str, Any] = Field(
        min_length=1, description="字段路径到新值的映射；嵌套字段用点号路径，如 image_prompt.scene"
    )


class PatchInsertOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    op: Literal["insert"]
    after_id: str | None = Field(
        default=None,
        min_length=1,
        description="新条目插在这个 id 之后；省略或为 null 时插到最前（空脚本的第一条即如此）",
    )
    item: dict[str, Any] = Field(description="新条目的完整内容；id 由系统分配，取本集现有最大序号的下一个号")


class PatchRemoveOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    op: Literal["remove"]
    id: str = Field(min_length=1, description="要删除的条目 id")


class PatchMoveOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    op: Literal["move"]
    id: str = Field(min_length=1, description="要移动的条目 id；条目内容与已生成的媒体随条目一起移动")
    after_id: str | None = Field(
        default=None,
        min_length=1,
        description="移到这个 id 之后；省略或为 null 时移到最前",
    )


class PatchSplitOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    op: Literal["split"]
    id: str = Field(min_length=1, description="要拆分的条目 id；第一段沿用该 id，其余段分配新 id")
    parts: list[dict[str, Any]] = Field(min_length=2, description="拆分后的各段完整内容，按顺序排列，至少两段")


PatchEpisodeScriptOperation = Annotated[
    PatchUpdateOperation | PatchInsertOperation | PatchMoveOperation | PatchRemoveOperation | PatchSplitOperation,
    Field(discriminator="op"),
]


class PatchEpisodeScriptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    script: str = Field(
        min_length=1, description="剧本纯文件名（不含目录），如 episode_1.json；单集单文件，多集编辑每集一次调用"
    )
    base_revision: str | SkipJsonSchema[None] = Field(
        default=None,
        description=(
            "基线 revision：不知道就省略（不给这个字段），服务端按当前 revision 执行；"
            "千万不要填占位文字或凭空编造，格式不符会被整批拒绝。"
            "要校验并发就传 get_episode_script 返回的原样 revision（sha256-v1: 前缀 + 64 位十六进制），"
            "与当前 revision 不一致时整批以 revision_conflict 拒绝，须重新读取后重试"
        ),
    )
    operations: list[PatchEpisodeScriptOperation] = Field(
        min_length=1,
        description=(
            "按顺序执行的编辑操作，整批原子提交：update / insert / move / remove / split。"
            'update 的结构固定为 {"op": "update", "id": "<条目id>", "fields": {"<字段路径>": "<新值>"}}：'
            "条目 id 写进 id（值如 E66S1，键名就叫 id，不是 unit_id），要改的字段全部写进 fields 映射；"
            "把字段平铺在 operation 上会被整批拒绝"
        ),
    )

    @field_validator("script")
    @classmethod
    def _validate_script(cls, value: str) -> str:
        if "/" in value or "\\" in value or value in (".", ".."):
            raise ValueError(f"script 必须是纯文件名，禁止路径分隔符: {value!r}")
        return value

    @field_validator("base_revision")
    @classmethod
    def _validate_base_revision(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if re.fullmatch(r"sha256-v1:[0-9a-f]{64}", value) is None:
            raise ValueError(
                "base_revision 不是合法 revision：须原样传 get_episode_script 返回的 revision"
                "（sha256-v1: 前缀 + 64 位十六进制），不要自行编造或拼接；"
                "请先调用 get_episode_script 读取本集当前 revision，再原样传入"
            )
        return value


_SPEECH_PROBLEM_CODES = frozenset(code.value for code in SpeechProblemCode)
_REGENERATION_FIELDS = frozenset({"image_prompt", "video_prompt"})


class ScriptSpeechProblem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    unit_id: str | None
    locations: tuple[ScriptBatchEditLocation, ...] = ()
    reason: str
    action: str


class ScriptSpeechAdmission(BaseModel):
    """剧本编辑因发声组合被拒时，被点名单元的发声准入；与 ``SpeechAdmission.to_dict()`` 同形。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: Literal[False] = False
    unit_id: str | None
    mode: None = None
    problems: tuple[ScriptSpeechProblem, ...]


class ScriptPatchResult(ScriptBatchEditResult):
    """``patch_episode_script`` 的结果：剧本批量编辑结果，加上给 Agent 的后续动作提示。

    - ``regeneration_required_ids``：提交成功且改了 image_prompt / video_prompt 的条目，须紧接着重新生成对应图 / 视频；
    - ``speech_admission``：首个问题属于发声组合时，被点名单元的全部发声问题。
    未绑定 mention 的提示沿用 ``warnings``。
    """

    regeneration_required_ids: tuple[str, ...] = ()
    speech_admission: ScriptSpeechAdmission | None = None


def _regeneration_required_ids(operations: list[PatchEpisodeScriptOperation]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            operation.id
            for operation in operations
            if isinstance(operation, PatchUpdateOperation)
            and any(field.split(".", 1)[0] in _REGENERATION_FIELDS for field in operation.fields)
        )
    )


def _speech_admission(result: ScriptBatchEditResult) -> ScriptSpeechAdmission | None:
    if not result.problems or result.problems[0].code not in _SPEECH_PROBLEM_CODES:
        return None
    unit_id = result.problems[0].unit_id
    return ScriptSpeechAdmission(
        unit_id=unit_id,
        problems=tuple(
            ScriptSpeechProblem(
                code=problem.code,
                unit_id=problem.unit_id,
                locations=problem.locations,
                reason=problem.reason,
                action=problem.next_action,
            )
            for problem in result.problems
            if problem.unit_id == unit_id
        ),
    )


def _script_patch_result(request: PatchEpisodeScriptRequest, result: ScriptBatchEditResult) -> ScriptPatchResult:
    return ScriptPatchResult(
        **dict(result),
        regeneration_required_ids=_regeneration_required_ids(request.operations) if result.success else (),
        speech_admission=_speech_admission(result),
    )


def _prompt_overwrite_problem(exc: PromptOverwriteRequiredError) -> ToolProblem:
    return ToolProblem("prompt_overwrite_required", str(exc), params={"prompt_overwrite": exc.overwrite})


def truncation_problem(exc: TextOutputTruncatedError) -> ToolProblem:
    """文本模型输出被截断：各文本任务同一个问题码，出路是登记最大输出长度（自定义模型）或换一个文本模型。"""
    return ToolProblem(
        "text_output_truncated",
        str(exc),
        action=GenerationAction.CONFIGURE_PROVIDER,
        params={"provider_id": exc.provider_id or exc.provider, "model": exc.model, "custom_model": exc.custom_model},
    )


def video_facts_problem(failure: VideoRequestFactsFailure, detail: str) -> ToolProblem:
    """视频请求事实解析不出：问题码与参数原样透出，修复指引与批量准入同一映射（多为配置供应商）。"""
    return ToolProblem(
        failure.code,
        detail,
        action=generation_action_for(failure.action),
        params=failure.parameters(),
    )


def _not_admitted_problem(exc: OperationNotAdmittedError) -> ToolProblem:
    """准入不成立的拒绝：``params.reason`` 与制作状态 ``operations`` 里同一操作的理由码一致。"""
    return ToolProblem(
        "operation_not_admitted",
        str(exc),
        action=GenerationAction.FIX_INPUT,
        params={"operation": exc.operation, "reason": exc.reason.value if exc.reason is not None else None},
    )


async def _run_text_generation(
    operation: str,
    call: Awaitable[TextGenerationResult],
) -> ToolOutcome[TextGenerationResult]:
    try:
        return ToolOutcome(value=await call)
    except PromptOverwriteRequiredError as exc:
        return ToolOutcome(problem=_prompt_overwrite_problem(exc))
    except OperationNotAdmittedError as exc:
        return ToolOutcome(problem=_not_admitted_problem(exc))
    except TextOutputTruncatedError as exc:
        return ToolOutcome(problem=truncation_problem(exc))
    except VideoRequestFactsUnavailableError as exc:
        return ToolOutcome(problem=video_facts_problem(exc.failure, str(exc)))
    except TextGenerationError as exc:
        return ToolOutcome(problem=ToolProblem("generation_refused", str(exc)))
    except Exception as exc:
        return ToolOutcome(problem=ToolProblem("internal_error", f"{operation} 失败: {exc}"))


_TEXT_EPISODE_SCRIPT = "text_episode_script"
#: 嵌入式宿主在本进程内提交、并在本进程内等待的文本任务：worker 沿用提交方的数据根与服务。
#: 只在内存里，不进任务载荷；没有登记的任务（MCP 提交、重启后的存量任务）按当前配置解析。
_TEXT_TASK_SERVICES: dict[str, tuple[ProjectScope, Services]] = {}


def _queued_generation_problem(problem: ToolProblem) -> GenerationProblem:
    try:
        action = GenerationAction(problem.action) if problem.action is not None else GenerationAction.RETRY
    except ValueError:
        action = GenerationAction.RETRY
    if problem.code == "generation_refused" and problem.action is None:
        action = GenerationAction.FIX_INPUT
    return GenerationProblem(code=problem.code, detail=problem.detail, action=action, params=problem.params or {})


async def _submit_text_task(
    *,
    task_type: str,
    operation: str,
    unit_id: str,
    payload: dict[str, Any],
    scope: ProjectScope,
    caller: CallerContext,
    services: Services,
    conflict_resource_ids: Sequence[str] = (),
) -> ToolOutcome[Any]:
    """提交一个文本任务。``conflict_resource_ids`` 是与 ``unit_id`` 互斥的其他占用槽：其中有在途任务时一律冲突；
    ``unit_id`` 上的在途任务只在请求事实不同时冲突，相同时并入它。"""
    active = await services.queue.get_active_tasks_for_resources(
        project_name=scope.project_name,
        task_type=task_type,
        resource_ids=list(dict.fromkeys([unit_id, *conflict_resource_ids])),
        user_id=caller.user_id,
    )
    conflicting = next(
        (
            task
            for task in active
            if task.get("resource_id") != unit_id
            or text_task_request_facts(task.get("payload")) != text_task_request_facts(payload)
        ),
        None,
    )
    if conflicting is not None:
        return ToolOutcome(
            problem=ToolProblem(
                "generation_active_task_conflict",
                "generation_active_task_conflict",
                action=GenerationAction.WAIT_FOR_TASK,
                params={"task_id": conflicting["task_id"], "status": conflicting["status"]},
            )
        )
    snapshot = GenerationBatchRequestSnapshot(
        selection=GenerationSelectionMode.EXPLICIT,
        requested=[GenerationBatchRequestedItem(unit_id=unit_id)],
    )
    batch_id = await services.queue.create_generation_batch(
        project_name=scope.project_name,
        operation=operation,
        requested=snapshot,
        blocked=[],
        source=caller.source,
        user_id=caller.user_id,
    )
    try:
        enqueue = await enqueue_task_only(
            project_name=scope.project_name,
            task_type=task_type,
            media_type="text",
            resource_id=unit_id,
            payload=payload,
            source=caller.source,
            user_id=caller.user_id,
            batch_id=batch_id,
            batch_unit_id=unit_id,
            queue=services.queue,
        )
        registered_services = caller.source == "embedded" and not enqueue.get("deduped")
        if registered_services:
            _TEXT_TASK_SERVICES[enqueue["task_id"]] = (scope, services)
        try:
            batch = await services.queue.get_generation_batch(
                project_name=scope.project_name, batch_id=batch_id, user_id=caller.user_id
            )
            if caller.source != "embedded":
                return ToolOutcome(value=batch)
            task = await wait_for_task(enqueue["task_id"], queue=services.queue)
        finally:
            if registered_services:
                _TEXT_TASK_SERVICES.pop(enqueue["task_id"], None)
        if task["status"] == "cancelled":
            problem = problem_from_task_failure(task.get("error_message"), cancelled=True)
            return ToolOutcome(problem=ToolProblem(**problem.model_dump(mode="json")))
        if task["status"] == "failed":
            problem = problem_from_task_failure(task.get("error_message"))
            return ToolOutcome(problem=ToolProblem(**problem.model_dump(mode="json")))
        result = task.get("result") or {}
        return ToolOutcome(value=TextGenerationResult(**result))
    except BaseException as exc:
        await cleanup_fresh_generation_batch(
            services.queue,
            project_name=scope.project_name,
            batch_id=batch_id,
            user_id=caller.user_id,
            failure=exc,
        )
        if isinstance(exc, WorkerOfflineError):
            return ToolOutcome(problem=ToolProblem(**enqueue_problem(str(exc)).model_dump(mode="json")))
        if isinstance(exc, ActiveTaskRequestConflict):
            return ToolOutcome(
                problem=ToolProblem(
                    "generation_active_task_conflict",
                    "generation_active_task_conflict",
                    action=GenerationAction.WAIT_FOR_TASK,
                    params={"task_id": exc.existing_task_id},
                )
            )
        raise


async def _execute_text_handler(
    operation: str,
    handler: Callable[..., Awaitable[TextGenerationResult]],
    request: TextGenerationRequest,
    scope: ProjectScope,
    services: Services,
) -> ToolOutcome[TextGenerationResult]:
    return await _run_text_generation(
        operation,
        handler(
            request,
            project_name=scope.project_name,
            projects=services.projects,
            config_resolver=services.capabilities,
        ),
    )


_TextInstructions = Annotated[
    str,
    Field(
        max_length=MAX_INSTRUCTIONS_LEN,
        description=(
            "用户对本次生成的附加指令原文（可选）；原样注入 prompt 末尾的「附加指令」分节，"
            f"遵循强度由正文表达，缺省/空白视同未传，最长 {MAX_INSTRUCTIONS_LEN} 字符"
        ),
    ),
]
_DRY_RUN_DESCRIPTION = "仅返回 prompt，不调用模型"


class GenerateEpisodeScriptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    episode_id: PositiveEpisode = Field(description=EPISODE_ID_DESCRIPTION)
    instructions: _TextInstructions | SkipJsonSchema[None] = None
    entry_ids: list[Annotated[str, Field(min_length=1)]] | SkipJsonSchema[None] = Field(
        default=None,
        description="本次编写的范围（分镜 / 单元 id）；省略时为全部待编写条目。点名只划定范围，不授权覆盖已有内容",
    )
    rewrite: bool = Field(
        default=False,
        description=(
            "显式重写范围内条目的本次提示词字段；省略时补缺，已有的本次字段保留。"
            "分镜类（segments/scenes/shots）必须与 fields 同用：省略 fields 时本次写的是 image_prompt，"
            "光一句 rewrite=true 等于「没说改哪层就改分镜提示词」，服务端直接拒绝"
        ),
    )
    fields: list[Literal["image_prompt", "video_prompt"]] | SkipJsonSchema[None] = Field(
        default=None,
        description="仅编写指定提示词字段；省略时默认写 image_prompt，video_prompt 必须在参考素材确认后单独请求",
    )
    overwrite_revision: str | SkipJsonSchema[None] = Field(
        default=None,
        description=(
            "用户听完丢失清单并同意覆盖后，才传入的令牌：提示词重写取 prompt_overwrite.revision，不覆盖已有内容时不必给"
        ),
    )
    dry_run: bool = Field(default=False, description=_DRY_RUN_DESCRIPTION)

    @model_validator(mode="after")
    def _single_prompt_field(self) -> Self:
        if self.fields is not None and {"image_prompt", "video_prompt"} <= set(self.fields):
            raise ValueError("image_prompt 和 video_prompt 不能在同一次提示词编写中同时生成")
        return self

    def text_request(self) -> TextGenerationRequest:
        return TextGenerationRequest(
            episode=self.episode_id,
            instructions=self.instructions,
            entry_ids=tuple(self.entry_ids or ()),
            rewrite=self.rewrite,
            fields=tuple(self.fields or ()),
            overwrite_revision=self.overwrite_revision,
            dry_run=self.dry_run,
        )


async def generate_episode_script(
    request: ToolRequest[GenerateEpisodeScriptRequest],
    scope: ProjectScope,
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[Any]:
    text_request = request.value.text_request()
    if text_request.dry_run:
        return await _execute_text_handler(
            "generate_episode_script", generate_episode_script_handler, text_request, scope, services
        )
    try:
        await asyncio.to_thread(
            functools.partial(
                prompt_authoring_preflight,
                services.projects.get_project_path(scope.project_name),
                text_request.episode,
                entry_ids=text_request.entry_ids,
                rewrite=text_request.rewrite,
                fields=text_request.fields,
                overwrite_revision=text_request.overwrite_revision,
            )
        )
    except PromptOverwriteRequiredError as exc:
        return ToolOutcome(problem=_prompt_overwrite_problem(exc))
    except OperationNotAdmittedError as exc:
        return ToolOutcome(problem=_not_admitted_problem(exc))
    except TextGenerationError as exc:
        return ToolOutcome(problem=ToolProblem("generation_refused", str(exc)))
    except Exception as exc:
        return ToolOutcome(problem=ToolProblem("internal_error", f"generate_episode_script 失败: {exc}"))
    return await _submit_text_task(
        task_type=_TEXT_EPISODE_SCRIPT,
        operation="generate_episode_script",
        unit_id=f"episode-{text_request.episode}",
        payload=text_request.to_payload(),
        scope=scope,
        caller=_caller,
        services=services,
    )


class ImportShortDramaArtifactItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(description="source/short_drama/ 下的七类标准相对路径")
    content_base64: str = Field(description="工件原始字节的 Base64 编码")


class IngestShortDramaArtifactsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifacts: list[ImportShortDramaArtifactItem] = Field(min_length=1, description="原样导入的短剧工厂 Markdown 工件")


async def ingest_short_drama_artifacts_tool(
    request: ToolRequest[IngestShortDramaArtifactsRequest],
    scope: ProjectScope,
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[Any]:
    from lib.short_drama_ingest import (
        ShortDramaArtifactError,
        decode_artifact_content,
        ingest_short_drama_artifacts,
    )

    try:
        await asyncio.to_thread(services.projects.load_project, scope.project_name)
        payload = [(item.path, decode_artifact_content(None, item.content_base64)) for item in request.value.artifacts]
        result = await _run_sync_transaction(
            ingest_short_drama_artifacts,
            services.projects,
            scope.project_name,
            payload,
        )
    except FileNotFoundError as exc:
        return ToolOutcome(problem=ToolProblem("project_not_found", str(exc)))
    except ShortDramaArtifactError as exc:
        return ToolOutcome(problem=ToolProblem("short_drama_import_refused", str(exc)))
    return ToolOutcome(
        value={"imported": len(result), "artifacts": [item.as_dict(include_content=False) for item in result]}
    )


class ShortDramaFactoryGenRequest(BaseModel):
    """短剧工厂文本生成请求：把某一类工厂工件的写作交给文本模型。

    Agent 负责从 references / 模板 / 本集上下文 / 指令组装好 ``prompt``（与可选
    ``system_prompt``），本工具经 ``TextGenerator`` 用 COMPLEX 档文本模型产出该工件
    Markdown 原文并返回；不解析、不转写为 ArcReel JSON。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(description="组装好的写作 prompt（含 references / 模板 / 本集上下文 / 指令）")
    system_prompt: str | None = Field(default=None, description="可选的系统提示，约束文风与输出格式")
    artifact: str | None = Field(
        default=None, description="工件类型标签（如 episode_plan / episode_NN），仅用于记账与回显"
    )
    max_output_tokens: int | None = Field(default=None, description="可选输出上限；缺省按模型登记上限")


async def generate_short_drama_factory(
    request: ToolRequest[ShortDramaFactoryGenRequest],
    scope: ProjectScope,
    _caller: CallerContext,
    _services: Services,
) -> ToolOutcome[Any]:
    """工厂文本生成：写作交文本模型（核心原则①），Agent 不自己写正文。

    经 ``TextGenerator``（``TextTaskType.FACTORY`` → COMPLEX 档，复用用户配置的强文本模型）
    产出工厂工件 Markdown；记账 purpose 复用 ``SCRIPT_GENERATION``。
    """
    value = request.value
    try:
        generator = await TextGenerator.create(
            TextTaskType.FACTORY,
            scope.project_name,
            purpose=CallPurpose.SCRIPT_GENERATION,
        )
        result = await generator.generate(
            TextBackendRequest(
                prompt=value.prompt,
                system_prompt=value.system_prompt,
                max_output_tokens=value.max_output_tokens,
            ),
            project_name=scope.project_name,
            require_complete=True,
        )
    except TextOutputTruncatedError as exc:
        return ToolOutcome(
            problem=ToolProblem(
                "text_output_truncated",
                f"工厂文本生成被模型输出上限截断（provider={exc.provider}, model={exc.model}）；"
                "请缩小单批体量或调高文本模型输出上限后重试。",
            )
        )
    return ToolOutcome(
        value={
            "text": result.text,
            "model": result.model,
            "provider": result.provider,
            "truncated": result.truncated,
            "artifact": value.artifact,
        }
    )


class NoArguments(BaseModel):
    """不带参数的工具请求。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceTextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(
        description="源文的项目相对路径，须位于 source/ 下，如 source/episode_1.txt；取自 list_source_files"
    )


class EpisodeScriptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    script: str = Field(description="剧本纯文件名（不含目录），如 episode_1.json")


class ProjectFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(
        description="业务文件的项目相对路径，如 project.json、scripts/episode_1.json；取自 list_project_files"
    )


class ProjectContent(BaseModel):
    revision: str
    project: dict[str, Any]


class EpisodeScriptContent(BaseModel):
    revision: str
    script_filename: str
    script: dict[str, Any]


class ProjectFileEntry(BaseModel):
    path: str
    size: int
    etag: str


class SourceFilesContent(BaseModel):
    revision: str
    files: list[ProjectFileEntry]


class SourceTextContent(BaseModel):
    revision: str
    etag: str
    path: str
    text: str


class ProjectFilesContent(BaseModel):
    revision: str
    files: list[ProjectFileEntry]


class ProjectFileContent(BaseModel):
    revision: str
    etag: str
    path: str
    content: Any


_EPISODE_DIR_RE = re.compile(r"episode_[1-9][0-9]*\Z")
BUSINESS_FILE_MAX_BYTES = 50 * 1024 * 1024
_SCRIPT_PLAN_BUSINESS_FILENAMES = frozenset(
    {
        *SCRIPT_PLAN_FILENAMES.values(),
        *(name for names in SCRIPT_PLAN_LEGACY_FILENAMES.values() for name in names),
        REFERENCE_VIDEO_SCRIPT_PLAN_FILENAME,
        REFERENCE_VIDEO_SCRIPT_PLAN_LEGACY_FILENAME,
        DRAMA_SCRIPT_PLAN_QUARANTINE_FILENAME,
        NARRATION_SCRIPT_PLAN_QUARANTINE_FILENAME,
        REFERENCE_VIDEO_SCRIPT_PLAN_QUARANTINE_FILENAME,
        REFERENCE_VIDEO_PROMPT_AUTHORING_QUARANTINE_FILENAME,
    }
)


def _business_path_parts(value: str) -> tuple[str, ...]:
    if not is_str(value) or not value or "\\" in value or "\x00" in value:
        raise ValueError("path 必须是项目内业务文件的 POSIX 相对路径")
    pure = PurePosixPath(value)
    parts = pure.parts
    if pure.is_absolute() or ".." in parts or any(part.startswith(".") for part in parts):
        raise ValueError("path 不在业务文件白名单内")
    allowed = (
        parts == ("project.json",)
        or (len(parts) == 2 and parts[0] == "source" and pure.suffix.lower() in {".txt", ".md"})
        or (len(parts) == 2 and parts[0] == "scripts" and pure.suffix.lower() == ".json")
        or (
            len(parts) == 3
            and parts[0] == "drafts"
            and _EPISODE_DIR_RE.fullmatch(parts[1]) is not None
            and parts[2] in _SCRIPT_PLAN_BUSINESS_FILENAMES
        )
    )
    if not allowed:
        raise ValueError("path 不在业务文件白名单内")
    return parts


def _resolve_business_file(project_dir: Path, relative: str) -> Path:
    parts = _business_path_parts(relative)
    lexical = project_dir
    for part in parts:
        lexical /= part
        if lexical.is_symlink():
            raise ValueError("symbolic links are not allowed")
    return safe_join(project_dir, *parts, require_file=True)


class _BusinessFileTooLargeError(ValueError):
    pass


def _read_business_file(project_dir: Path, relative: str) -> tuple[bytes, Path]:
    parts = _business_path_parts(relative)
    path = project_dir.joinpath(*parts)
    if os.open not in os.supports_dir_fd:
        resolved = _resolve_business_file(project_dir, relative)
        with resolved.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            current = os.lstat(resolved)
            if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
                raise ValueError("path 必须指向普通文件")
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError("path 在安全校验后发生变化")
            raw = handle.read(BUSINESS_FILE_MAX_BYTES + 1)
    else:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_fd = os.open(project_dir, directory_flags)
        try:
            for part in parts[:-1]:
                child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = child_fd
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                    raise ValueError("path 必须指向普通文件")
                with os.fdopen(file_fd, "rb", closefd=False) as handle:
                    raw = handle.read(BUSINESS_FILE_MAX_BYTES + 1)
            finally:
                os.close(file_fd)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ValueError("path 必须指向无 symlink 的普通文件") from exc
        finally:
            os.close(directory_fd)
    if len(raw) > BUSINESS_FILE_MAX_BYTES:
        raise _BusinessFileTooLargeError(f"文件超过 {BUSINESS_FILE_MAX_BYTES} 字节读取上限")
    return raw, path


def _decode_business_file(project_dir: Path, relative: str) -> tuple[Any, str, str, int]:
    raw, path = _read_business_file(project_dir, relative)
    etag = prefixed(hashlib.sha256(raw).hexdigest())
    text = raw.decode("utf-8")
    if path.suffix.lower() == ".json":
        content = json.loads(text)
        revision = prefixed_canonical_json_digest(content)
    else:
        content = text
        revision = etag
    return content, revision, etag, len(raw)


def _business_file_entries(
    project_dir: Path,
    *,
    source_only: bool = False,
) -> list[ProjectFileEntry]:
    candidates = [] if source_only else [project_dir / "project.json"]
    for dirname in ("source",) if source_only else ("source", "scripts"):
        directory = project_dir / dirname
        if directory.is_dir() and not directory.is_symlink():
            candidates.extend(directory.iterdir())
    drafts = project_dir / "drafts"
    if not source_only and drafts.is_dir() and not drafts.is_symlink():
        for episode_dir in drafts.iterdir():
            if episode_dir.is_dir() and not episode_dir.is_symlink() and _EPISODE_DIR_RE.fullmatch(episode_dir.name):
                candidates.extend(episode_dir.iterdir())

    entries: list[ProjectFileEntry] = []
    for candidate in candidates:
        try:
            relative = candidate.relative_to(project_dir).as_posix()
            raw, _path = _read_business_file(project_dir, relative)
            etag = prefixed(hashlib.sha256(raw).hexdigest())
            size = len(raw)
        except (FileNotFoundError, OSError, TypeError, ValueError):
            continue
        entries.append(ProjectFileEntry(path=relative, size=size, etag=etag))
    return sorted(entries, key=lambda entry: entry.path)


def _file_problem(name: str, exc: BaseException) -> ToolProblem:
    if isinstance(exc, FileNotFoundError):
        return ToolProblem("file_not_found", f"{name} 文件不存在")
    if isinstance(exc, (json.JSONDecodeError, UnicodeError)):
        return ToolProblem("invalid_content", f"{name} 文件不是有效的 UTF-8 JSON/文本")
    if isinstance(exc, _BusinessFileTooLargeError):
        return ToolProblem("file_too_large", str(exc))
    if isinstance(exc, (TypeError, ValueError)):
        return ToolProblem("unsafe_path", str(exc))
    return ToolProblem("internal_error", f"{name} 失败: {exc}")


def _get_project_content_sync(project_name: str, projects: ProjectManager) -> ProjectContent:
    project = projects.load_project(project_name)
    return ProjectContent(revision=prefixed_canonical_json_digest(project), project=project)


async def get_project_content(
    _request: ToolRequest[NoArguments],
    scope: ProjectScope,
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[ProjectContent]:
    try:
        content = await asyncio.to_thread(_get_project_content_sync, scope.project_name, services.projects)
    except FileNotFoundError as exc:
        return ToolOutcome(problem=ToolProblem("project_not_found", f"项目未找到或缺 project.json: {exc}"))
    except Exception as exc:
        return ToolOutcome(problem=ToolProblem("internal_error", f"get_project_content 失败: {exc}"))
    return ToolOutcome(value=content)


async def get_episode_script(
    request: ToolRequest[EpisodeScriptRequest],
    scope: ProjectScope,
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[EpisodeScriptContent]:
    """读一集剧本并签发 revision；迁移失败的项目不签发 revision，返回完整的迁移 problem。"""
    filename = request.value.script
    if not filename or "/" in filename or "\\" in filename or filename in {".", ".."}:
        return ToolOutcome(problem=ToolProblem("invalid_request", "script 必须是纯文件名"))
    try:
        failure = await asyncio.to_thread(project_migration_failure, scope.project_name, services.projects)
        if failure is not None:
            return ToolOutcome(problem=_migration_tool_problem(failure))
        script = await asyncio.to_thread(services.projects.load_script_readonly, scope.project_name, filename)
    except FileNotFoundError as exc:
        return ToolOutcome(problem=ToolProblem("file_not_found", str(exc)))
    except (TypeError, ValueError) as exc:
        return ToolOutcome(problem=ToolProblem("invalid_request", str(exc)))
    except Exception as exc:
        return ToolOutcome(problem=ToolProblem("internal_error", f"get_episode_script 失败: {exc}"))
    return ToolOutcome(
        value=EpisodeScriptContent(revision=script_revision(script), script_filename=filename, script=script)
    )


class PromptPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    script: str = Field(min_length=1, description="剧本纯文件名（不含目录），如 episode_1.json")
    item_id: str = Field(min_length=1, description="分镜条目 id（segment_id / scene_id / shot_id / unit_id），如 E1S01")


async def get_prompt_preview(
    request: ToolRequest[PromptPreviewRequest],
    scope: ProjectScope,
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[ItemPromptPreview]:
    """渲染一个条目当前会送进图像 / 视频模型的最终提示词文本。

    与执行路径共用同一渲染出口，结果逐字等于本次生成实际发出的提示词。只读：不向供应商
    发请求、不产生费用、不写产物清单。``unavailable`` 是稳定的机器可读原因码，两个 host
    原样透传，UI 侧的成品文案由 REST 路由按请求语言渲染。
    """
    filename = request.value.script
    if "/" in filename or "\\" in filename or filename in {".", ".."}:
        return ToolOutcome(problem=ToolProblem("invalid_request", "script 必须是纯文件名"))
    try:
        if problem := await migration_gate(scope, services):
            return ToolOutcome(problem=problem)
        preview = await preview_item_prompts(
            scope.project_name, filename, request.value.item_id, projects=services.projects
        )
    except ScriptItemNotFound:
        return ToolOutcome(problem=ToolProblem("item_not_found", f"剧本中不存在分镜 {request.value.item_id}"))
    except FileNotFoundError as exc:
        return ToolOutcome(problem=ToolProblem("file_not_found", str(exc)))
    except (TypeError, ValueError) as exc:
        return ToolOutcome(problem=ToolProblem("invalid_request", str(exc)))
    except Exception as exc:
        return ToolOutcome(problem=ToolProblem("internal_error", f"get_prompt_preview 失败: {exc}"))
    return ToolOutcome(value=preview)


async def list_source_files(
    _request: ToolRequest[NoArguments],
    scope: ProjectScope,
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[SourceFilesContent]:
    try:
        project_dir = services.projects.get_project_path(scope.project_name)
        files = await asyncio.to_thread(
            _business_file_entries,
            project_dir,
            source_only=True,
        )
        revision = prefixed_canonical_json_digest([entry.model_dump() for entry in files])
        return ToolOutcome(value=SourceFilesContent(revision=revision, files=files))
    except Exception as exc:
        return ToolOutcome(problem=_file_problem("list_source_files", exc))


async def get_source_text(
    request: ToolRequest[SourceTextRequest],
    scope: ProjectScope,
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[SourceTextContent]:
    path = request.value.path
    try:
        project_dir = services.projects.get_project_path(scope.project_name)
        if not path.startswith("source/"):
            raise ValueError("path 必须指向 source/ 下的文本文件")
        content, revision, etag, _size = await asyncio.to_thread(_decode_business_file, project_dir, path)
        if not isinstance(content, str):
            raise ValueError("source 文件必须是文本")
        return ToolOutcome(value=SourceTextContent(revision=revision, etag=etag, path=path, text=content))
    except Exception as exc:
        return ToolOutcome(problem=_file_problem("get_source_text", exc))


async def list_project_files(
    _request: ToolRequest[NoArguments],
    scope: ProjectScope,
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[ProjectFilesContent]:
    try:
        files = await asyncio.to_thread(
            _business_file_entries,
            services.projects.get_project_path(scope.project_name),
        )
        revision = prefixed_canonical_json_digest([entry.model_dump() for entry in files])
        return ToolOutcome(value=ProjectFilesContent(revision=revision, files=files))
    except Exception as exc:
        return ToolOutcome(problem=_file_problem("list_project_files", exc))


async def read_project_file(
    request: ToolRequest[ProjectFileRequest],
    scope: ProjectScope,
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[ProjectFileContent]:
    path = request.value.path
    try:
        project_dir = services.projects.get_project_path(scope.project_name)
        content, revision, etag, _size = await asyncio.to_thread(_decode_business_file, project_dir, path)
        return ToolOutcome(value=ProjectFileContent(revision=revision, etag=etag, path=path, content=content))
    except Exception as exc:
        return ToolOutcome(problem=_file_problem("read_project_file", exc))


class CreateProjectToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(description="项目唯一标识，只能包含字母、数字和连字符；后续工具以它寻址项目")
    title: str = Field(default="", description="用户可见的项目标题")
    content_mode: Literal["drama"] = Field(default="drama", description="短剧工厂项目")
    generation_mode: Literal["storyboard"] = Field(
        default="storyboard", description="工厂制作：AI 分镜、参考素材和视频"
    )
    aspect_ratio: str = Field(default="9:16", description="画面比例，如 9:16、16:9")
    default_duration: int | None = Field(
        default=None, gt=0, description="默认单镜时长（秒）；缺省不写入，由视频模型能力决定；广告/短片项目不可用"
    )
    narration_delivery: NarrationDelivery = Field(
        default=POST_PRODUCTION,
        description=(
            "旁白交付方式：post_production 后期配音（画外音文字随字幕交付，创作者自行配音）、"
            "use_tts 由 ArcReel 生成 TTS 旁白配音；创建后用户可在项目设置里修改"
        ),
    )
    audio_backend: str | SkipJsonSchema[None] = Field(
        default=None,
        description="TTS 模型，形如 provider/model；仅 use_tts 使用，省略时取全局默认音频模型",
    )
    narration_voice: str | SkipJsonSchema[None] = Field(
        default=None, description="旁白音色 id；仅 use_tts 使用，省略时取全局默认音色"
    )
    narration_speed: float | None = Field(
        default=None,
        description="配音语速倍率（正数）；仅 use_tts 使用，省略时取全局默认，null 表示不向供应商传语速",
    )


async def list_projects(
    _request: ToolRequest[NoArguments],
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[list[dict[str, Any]]]:
    def _list() -> list[dict[str, Any]]:
        result = []
        for name in sorted(services.projects.list_projects()):
            try:
                project = services.projects.load_project(name)
            except (FileNotFoundError, ValueError):
                continue
            result.append(
                {
                    "name": name,
                    "title": project.get("title", ""),
                    "content_mode": project.get("content_mode"),
                    "generation_mode": project.get("generation_mode"),
                }
            )
        return result

    try:
        return ToolOutcome(value=await asyncio.to_thread(_list))
    except Exception as exc:
        return ToolOutcome(problem=ToolProblem("internal_error", f"list_projects 失败: {exc}"))


_NARRATION_CONFIG_PROBLEMS = {
    "narration_tts_model_required": "use_tts 需要 TTS 模型：传 audio_backend（provider/model），或先让用户配置音频供应商",
    "narration_tts_model_invalid": "audio_backend 不是可用的 TTS 模型，需为音频供应商下的 provider/model",
    "narration_tts_voice_required": "use_tts 需要非空的 narration_voice",
    "narration_tts_speed_invalid": "narration_speed 必须是正的有限数值或 null",
}


async def create_project(
    request: ToolRequest[CreateProjectToolRequest],
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[dict[str, Any]]:
    value = request.value
    try:
        narration = await new_project_narration_fields(
            NarrationSettingsInput(
                delivery=value.narration_delivery,
                audio_backend=value.audio_backend,
                narration_voice=value.narration_voice,
                narration_speed=value.narration_speed,
                provided=frozenset(value.model_fields_set & {"audio_backend", "narration_voice", "narration_speed"}),
            ),
            resolver=services.capabilities,
        )
    except NarrationConfigError as exc:
        return ToolOutcome(problem=ToolProblem("invalid_request", _NARRATION_CONFIG_PROBLEMS[exc.code]))

    def _create() -> dict[str, Any]:
        name = services.projects.normalize_project_name(value.name)
        services.projects.create_project(name, content_mode=value.content_mode, publish=False)
        try:
            project = services.projects.create_project_metadata(
                name,
                value.title,
                content_mode=value.content_mode,
                aspect_ratio=value.aspect_ratio,
                default_duration=value.default_duration,
                narration=narration,
            )
        except Exception:
            services.projects.delete_project_directory(name)
            raise
        return {"name": name, "project": project}

    try:
        return ToolOutcome(value=await _run_sync_transaction(_create))
    except FileExistsError as exc:
        return ToolOutcome(problem=ToolProblem("project_exists", str(exc)))
    except ValueError as exc:
        return ToolOutcome(problem=ToolProblem("invalid_request", str(exc)))
    except Exception as exc:
        return ToolOutcome(problem=ToolProblem("internal_error", f"create_project 失败: {exc}"))


async def get_workflow_plan(
    request: ToolRequest[WorkflowPlanRequest],
    scope: ProjectScope,
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[WorkflowPlan]:
    try:
        plan = await services.workflow_planner.get_plan(
            scope.project_name,
            request.value,
            user_id=_caller.user_id,
            queue=services.queue,
            config_resolver=services.capabilities,
        )
    except WorkflowRequestError as exc:
        return ToolOutcome(problem=ToolProblem("invalid_request", str(exc)))
    except Exception as exc:
        return ToolOutcome(problem=ToolProblem("internal_error", f"get_workflow_plan 失败: {exc}"))
    return ToolOutcome(value=plan)


async def get_video_capabilities(
    _request: ToolRequest[NoArguments],
    scope: ProjectScope,
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[dict[str, Any]]:
    try:
        project = await asyncio.to_thread(services.projects.load_project, scope.project_name)
        payload = await services.capabilities.video_capabilities_for_project(project)
        payload["duration_constraints"] = duration_constraints_payload(
            await capability_request_facts(
                project,
                generation_type=video_bucket_for_generation_mode(caps_generation_mode(project)),
                config_resolver=services.capabilities,
            )
        )
    except FileNotFoundError as exc:
        return ToolOutcome(problem=ToolProblem("project_not_found", f"项目未找到或缺 project.json: {exc}"))
    except VideoRequestFactsError as exc:
        failure = exc.failure
        return ToolOutcome(
            problem=ToolProblem(
                failure.code,
                f"无法解析视频模型能力: {failure.summary()}",
                action=failure.action,
                params=failure.parameters(),
            )
        )
    except ValueError as exc:
        return ToolOutcome(problem=ToolProblem("capabilities_unresolved", f"无法解析视频模型能力: {exc}"))
    except Exception as exc:
        return ToolOutcome(problem=ToolProblem("internal_error", f"get_video_capabilities 失败: {exc}"))
    return ToolOutcome(value=payload)


def _script_edit_failure(
    request: PatchEpisodeScriptRequest,
    current: dict[str, Any],
    *,
    code: str,
    reason: str,
    next_action: str,
    operation_index: int | None = None,
    unit_id: str | None = None,
    location: tuple[str | int, ...] = (),
) -> ScriptBatchEditResult:
    revision = script_revision(current)
    episode = current.get("episode")
    return ScriptBatchEditResult(
        success=False,
        script=request.script,
        episode=episode if isinstance(episode, int) and not isinstance(episode, bool) else None,
        before_revision=revision,
        revision=revision,
        problems=(
            ScriptBatchEditProblem(
                code=code,
                operation_index=operation_index,
                unit_id=unit_id,
                locations=(ScriptBatchEditLocation(path=location),) if location else (),
                reason=reason,
                next_action=next_action,
            ),
        ),
    )


def _project_patch_operations(
    script: dict[str, Any],
    operations: list[PatchEpisodeScriptOperation],
) -> tuple[list[dict[str, Any]], list[int], frozenset[int]]:
    """Project public structural ops onto the existing transactional command."""
    preview = copy.deepcopy(script)
    projected: list[dict[str, Any]] = []
    source_indexes: list[int] = []
    fresh_insert_indexes: set[int] = set()

    def append(operation: dict[str, Any], source_index: int, *, fresh_insert: bool = False) -> None:
        if fresh_insert:
            fresh_insert_indexes.add(len(projected))
        projected.append(operation)
        source_indexes.append(source_index)

    for index, operation in enumerate(operations):

        def apply(
            action: Callable[[], Any],
            *,
            location: tuple[str | int, ...],
            unit_id: str | None = None,
            operation_index: int = index,
        ) -> Any:
            try:
                return action()
            except ScriptEditError as exc:
                exc.params.update(operation_index=operation_index, location=location, unit_id=unit_id)
                raise

        if isinstance(operation, PatchUpdateOperation):
            item_id = operation.id
            for field, value in operation.fields.items():
                apply(
                    lambda field=field, value=value, item_id=item_id: patch_field(preview, item_id, field, value),
                    location=("fields", *field.split(".")),
                    unit_id=item_id,
                )
            append(operation.model_dump(mode="python"), index)
            continue

        if isinstance(operation, PatchInsertOperation):
            insert_after_id = operation.after_id
            new_item = operation.item
            apply(
                lambda insert_after_id=insert_after_id, new_item=new_item: insert_segment(
                    preview, insert_after_id, new_item
                ),
                location=("after_id",),
                unit_id=insert_after_id,
            )
            items, id_field, _kind = resolve_items(preview)
            anchor = (
                -1
                if insert_after_id is None
                else next(i for i, item in enumerate(items) if str(item.get(id_field)) == insert_after_id)
            )
            append(
                {"op": "insert_after", "after_id": insert_after_id, "item": copy.deepcopy(items[anchor + 1])},
                index,
                fresh_insert=True,
            )
            continue

        if isinstance(operation, PatchMoveOperation):
            item_id = operation.id
            move_after_id = operation.after_id
            items, id_field, _kind = resolve_items(preview)
            if not any(str(item.get(id_field)) == item_id for item in items):
                raise ScriptEditError(
                    f"未找到 id={item_id!r} 的分镜", operation_index=index, location=("id",), unit_id=item_id
                )
            apply(
                lambda item_id=item_id, move_after_id=move_after_id: move_segment(preview, item_id, move_after_id),
                location=("after_id",),
                unit_id=item_id,
            )
            append({"op": "move_after", "id": item_id, "after_id": move_after_id}, index)
            continue

        if isinstance(operation, PatchRemoveOperation):
            item_id = operation.id
            apply(lambda item_id=item_id: remove_segment(preview, item_id), location=("id",), unit_id=item_id)
            append(operation.model_dump(mode="python"), index)
            continue

        item_id = operation.id
        parts = operation.parts
        items, id_field, _kind = resolve_items(preview)
        original_index = next(
            (i for i, item in enumerate(items) if str(item.get(id_field)) == item_id),
            None,
        )
        if original_index is None:
            raise ScriptEditError(
                f"未找到 id={item_id!r} 的分镜",
                operation_index=index,
                location=("id",),
                unit_id=item_id,
            )
        previous_id = str(items[original_index - 1].get(id_field)) if original_index else None
        apply(
            lambda item_id=item_id, parts=parts: split_segment(preview, item_id, parts),
            location=("parts",),
            unit_id=item_id,
        )
        items, id_field, _kind = resolve_items(preview)
        anchor = next(i for i, item in enumerate(items) if str(item.get(id_field)) == item_id)
        generated = items[anchor : anchor + len(parts)]
        append({"op": "remove", "id": item_id}, index)
        split_after_id = previous_id
        for part_index, item in enumerate(generated):
            append(
                {"op": "insert_after", "after_id": split_after_id, "item": copy.deepcopy(item)},
                index,
                fresh_insert=part_index > 0,
            )
            split_after_id = str(item[id_field])

    return projected, source_indexes, frozenset(fresh_insert_indexes)


def _remap_operation_indexes(result: ScriptBatchEditResult, source_indexes: list[int]) -> ScriptBatchEditResult:
    if not result.problems:
        return result
    remapped: list[ScriptBatchEditProblem] = []
    for problem in result.problems:
        internal_index = problem.operation_index
        public_index = (
            source_indexes[internal_index]
            if internal_index is not None and 0 <= internal_index < len(source_indexes)
            else internal_index
        )
        locations: list[ScriptBatchEditLocation] = []
        for location in problem.locations:
            path = location.path
            if len(path) >= 2 and path[0] == "operations" and isinstance(path[1], int):
                path = (path[0], public_index if public_index is not None else path[1], *path[2:])
            locations.append(location.model_copy(update={"path": path}))
        remapped.append(problem.model_copy(update={"operation_index": public_index, "locations": tuple(locations)}))
    return result.model_copy(update={"problems": tuple(remapped)})


def _patch_episode_script_sync(
    request: ToolRequest[PatchEpisodeScriptRequest],
    scope: ProjectScope,
    services: Services,
) -> ToolOutcome[ScriptBatchEditResult]:
    try:
        current = services.projects.load_script(scope.project_name, request.value.script)
    except FileNotFoundError:
        return ToolOutcome(problem=ToolProblem("script_not_found", f"剧本不存在: {request.value.script}"))
    except Exception as exc:
        return ToolOutcome(problem=ToolProblem("internal_error", f"patch_episode_script 失败: {exc}"))

    # 省略 revision 时以当前服务端版本执行；显式提供时保持并发冲突保护。
    requested_revision = request.value.base_revision
    expected_revision = requested_revision if requested_revision is not None else script_revision(current)

    if requested_revision is not None and requested_revision != script_revision(current):
        return ToolOutcome(
            value=_script_edit_failure(
                request.value,
                current,
                code="revision_conflict",
                reason="revision_mismatch",
                next_action="refresh_script",
            )
        )

    try:
        projected, source_indexes, fresh_insert_indexes = _project_patch_operations(current, request.value.operations)
    except ScriptEditError as exc:
        latest = services.projects.load_script(scope.project_name, request.value.script)
        if requested_revision is not None and requested_revision != script_revision(latest):
            return ToolOutcome(
                value=_script_edit_failure(
                    request.value,
                    latest,
                    code="revision_conflict",
                    reason="revision_mismatch",
                    next_action="refresh_script",
                )
            )
        raw_operation_index = exc.params.get("operation_index")
        operation_index = raw_operation_index if isinstance(raw_operation_index, int) else None
        raw_location = exc.params.get("location")
        operation_location = raw_location if isinstance(raw_location, tuple) else ()
        raw_unit_id = exc.params.get("unit_id")
        unit_id = raw_unit_id if isinstance(raw_unit_id, str) else None
        return ToolOutcome(
            value=_script_edit_failure(
                request.value,
                current,
                code="operation_invalid",
                reason="operation_invalid",
                next_action="fix_operation",
                operation_index=operation_index,
                unit_id=unit_id,
                location=("operations", operation_index, *operation_location) if operation_index is not None else (),
            )
        )

    command = ScriptBatchEditCommand.model_validate(
        {
            "script": request.value.script,
            "expected_revision": expected_revision,
            "operations": projected,
        }
    )
    result = ScriptBatchEditor(services.projects).execute(
        scope.project_name,
        command,
        fresh_insert_indexes=fresh_insert_indexes,
    )
    return ToolOutcome(value=_remap_operation_indexes(result, source_indexes))


async def patch_episode_script(
    request: ToolRequest[PatchEpisodeScriptRequest],
    scope: ProjectScope,
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[ScriptPatchResult]:
    outcome = await _run_sync_transaction(_patch_episode_script_sync, request, scope, services)
    if outcome.value is None:
        return ToolOutcome(problem=outcome.problem)
    return ToolOutcome(value=_script_patch_result(request.value, outcome.value))


ASSET_TABLES = tuple(spec.bucket_key for spec in ASSET_SPECS.values())
MERGEABLE_ASSET_TABLES = tuple(ASSET_SPECS[asset_type].bucket_key for asset_type in MERGEABLE_ASSET_TYPES)
PROJECT_SETTINGS = (
    EPISODE_TARGET_DURATION_FIELD,
    "source_language",
    "narration_voice",
    "narration_speed",
    "character_voice_binding",
)
EPISODE_META_FIELDS = ("title",)

_SOURCE_LANGUAGE_VALUES = ("zh", "en", "vi")


class ToolMessage(BaseModel):
    message: str


class PatchProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    table: str | None = Field(
        default=None,
        description=f"（资产 upsert 分支，与 entries 同时给出）资产表，取值 {list(ASSET_TABLES)} 之一",
        json_schema_extra={"enum": [*ASSET_TABLES, None]},
    )
    entries: dict[str, Any] | None = Field(
        default=None,
        description=(
            "（资产 upsert 分支）{ 名称: { description, voice_style 等字段 } } 映射，至少一条；名称不存在则新增、"
            "存在则合并改字段。角色条目可带 derivatives: { 衍生名: { description } }，登记本体之外的另一套外观，"
            "按衍生名合并（同名改描述、新名加入、未提及的保留），description 写相对本体的变化"
        ),
    )
    settings: dict[str, Any] | None = Field(
        default=None,
        description=f"（settings 写入分支）顶层字段映射，键须在白名单 {list(PROJECT_SETTINGS)} 内，值为 null 时清除该字段",
    )

    @model_validator(mode="after")
    def _validate_shape(self) -> PatchProjectRequest:
        has_upsert = self.table is not None or self.entries is not None
        branches = (has_upsert, self.settings is not None)
        if sum(branches) > 1:
            raise ValueError("table/entries 与 settings 二选一,不能同时给出多个")
        if not any(branches):
            raise ValueError("必须提供 table+entries(资产 upsert)或 settings(顶层字段)之一")
        if has_upsert:
            if self.table is None or self.entries is None:
                raise ValueError("资产 upsert 分支必须同时提供 table 和 entries")
            if self.table not in ASSET_TABLES:
                raise ValueError(f"table 必须是 {list(ASSET_TABLES)} 之一")
            if not self.entries:
                raise ValueError("entries 必须是非空 { 名称: 字段对象 } 映射")
        if self.settings is not None and not self.settings:
            raise ValueError("settings 必须是非空 { 字段名: 值 } 映射")
        return self


class PatchProjectResult(ToolMessage):
    operation: Literal["assets", "settings"]
    changes: dict[str, Any]


class PatchEpisodeMetaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    script: str = Field(description="剧本纯文件名（不含目录），如 episode_1.json")
    field: Literal["title"] = Field(description=f"要编辑的剧本顶层字段，白名单 {list(EPISODE_META_FIELDS)}")
    value: str = Field(description="新值；title 须为非空字符串，首尾空白会被裁剪")

    @field_validator("script")
    @classmethod
    def _validate_script(cls, value: str) -> str:
        if not value or "/" in value or "\\" in value or value in (".", ".."):
            raise ValueError(f"script 必须是纯文件名，禁止路径分隔符: {value!r}")
        return value

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title 必须是非空字符串")
        return value.strip()


class PatchEpisodeMetaResult(ToolMessage):
    script: str
    field: str
    value: str


class RenameAssetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    table: str = Field(
        description=f"资产表，取值 {list(ASSET_TABLES)} 之一", json_schema_extra={"enum": list(ASSET_TABLES)}
    )
    old_name: str = Field(description="现有资产名")
    new_name: str = Field(description="新资产名；与同表既有资产冲突（按 NFC 归一判定）时整体拒绝")

    @field_validator("table")
    @classmethod
    def _validate_table(cls, value: str) -> str:
        if value not in ASSET_TABLES:
            raise ValueError(f"table 必须是 {list(ASSET_TABLES)} 之一")
        return value


class RenameAssetResult(ToolMessage):
    table: str
    old_name: str
    new_name: str
    episodes: int
    references: int
    files: int


class MergeAssetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    table: str = Field(
        description=f"资产表，取值 {list(MERGEABLE_ASSET_TABLES)} 之一",
        json_schema_extra={"enum": list(MERGEABLE_ASSET_TABLES)},
    )
    source: str = Field(description="被并方：合并后从资产表删除")
    target: str = Field(description="保留方：同表的另一个资产，原样保留")
    as_derivative: bool = Field(
        default=False,
        description="仅 characters：true 时把 source 并为 target 的衍生，衍生名取 source 的名字，"
        "描述取 source 的描述，衍生资产图待生成",
    )
    dry_run: bool = Field(default=False, description="true 时只返回按集列出的影响，不做任何更改")

    @field_validator("table")
    @classmethod
    def _validate_table(cls, value: str) -> str:
        if value not in MERGEABLE_ASSET_TABLES:
            raise ValueError(f"table 必须是 {list(MERGEABLE_ASSET_TABLES)} 之一")
        return value


class MergeAssetEpisodeImpact(BaseModel):
    episode_id: int
    script_plan: int
    script: int
    draft: int
    prompt_text: int
    speaker: int
    storyboards: int
    videos: int


class MergeAssetResult(ToolMessage):
    table: str
    source: str
    target: str
    as_derivative: bool
    dry_run: bool
    references: int
    aliases_added: list[str]
    derivative_created: str | None
    derivatives_moved: list[str]
    derivatives_folded: list[str]
    episodes: list[MergeAssetEpisodeImpact]


class RetryProjectMigrationResult(ToolMessage):
    workflow_plan: WorkflowPlan


def _unexpected(name: str, exc: BaseException) -> ToolProblem:
    return ToolProblem("internal_error", f"{name} 失败: {exc}")


def _migration_tool_problem(failure: MigrationFailureRecord) -> ToolProblem:
    payload = migration_problem(failure).model_dump(mode="json")
    return ToolProblem(
        code=str(payload["code"]),
        detail=str(payload["detail"]),
        action=str(payload["action"]),
        params=payload.get("params"),
    )


async def migration_gate(scope: ProjectScope, services: Services) -> ToolProblem | None:
    failure = await asyncio.to_thread(project_migration_failure, scope.project_name, services.projects)
    return _migration_tool_problem(failure) if failure is not None else None


def _text_result_payload(value: TextGenerationResult) -> dict[str, Any]:
    """任务结果里的文本回执：``warnings`` 与 ``new_assets`` 只在非空时写入，读侧按 ``result.warnings`` 渲染。"""
    payload: dict[str, Any] = {"message": value.message}
    if value.warnings:
        payload["warnings"] = list(value.warnings)
    return payload


async def execute_queued_text_task(task: dict[str, Any], *, services: Services | None = None) -> dict[str, Any]:
    """只执行工厂提示词任务，源稿生成由工厂任务服务负责。"""
    if task["task_type"] != _TEXT_EPISODE_SCRIPT:
        raise ValueError(f"unsupported text task_type: {task['task_type']}")
    payload = task.get("payload") or {}
    registered = _TEXT_TASK_SERVICES.pop(str(task["task_id"]), None)
    if registered is not None:
        scope, services = registered
    else:
        scope = ProjectScope(project_name=str(task["project_name"]), data_root=DataRootLayout.current().root)
        services = services or Services.defaults(ProjectManager(scope.data_root))
    request = TextGenerationRequest(
        episode=payload["episode"],
        instructions=payload.get("instructions"),
        dry_run=bool(payload.get("dry_run")),
        entry_ids=tuple(payload.get("entry_ids") or ()),
        rewrite=bool(payload.get("rewrite")),
        fields=tuple(payload.get("fields") or ()),
        overwrite_revision=payload.get("overwrite_revision"),
    )
    outcome = await _execute_text_handler(
        "generate_episode_script", generate_episode_script_handler, request, scope, services
    )
    if outcome.problem is not None:
        raise RuntimeError(encode_generation_problem(_queued_generation_problem(outcome.problem)))
    if outcome.value is None:
        raise RuntimeError("text task returned no result")
    return _text_result_payload(outcome.value)


def _coerce_numeric_string(value: str, parser: Callable[[str], int | float], message: str) -> int | float:
    try:
        return parser(value.strip())
    except ValueError:
        raise ValueError(message) from None


def _coerce_setting_value(key: str, value: Any) -> Any:
    if key == EPISODE_TARGET_DURATION_FIELD:
        if value is None:
            return None
        message = (
            f"{EPISODE_TARGET_DURATION_FIELD} 必须是 {MIN_EPISODE_TARGET_DURATION}-"
            f"{MAX_EPISODE_TARGET_DURATION} 秒的整数或 null,收到 {value!r}"
        )
        if isinstance(value, str):
            value = _coerce_numeric_string(value, int, message)
        if not is_valid_episode_target_duration(value):
            raise ValueError(message)
        return value
    if key == "source_language":
        if value is not None and (not isinstance(value, str) or value not in _SOURCE_LANGUAGE_VALUES):
            raise ValueError(f"source_language 必须是 {list(_SOURCE_LANGUAGE_VALUES)} 之一或 null,收到 {value!r}")
        return value
    if key == "brief":
        if value is not None and not isinstance(value, str):
            raise ValueError(f"brief 必须是字符串或 null,收到 {value!r}")
        return value
    if key == "character_voice_binding":
        if value is not None and (not isinstance(value, str) or value not in VALID_CHARACTER_VOICE_BINDINGS):
            raise ValueError(
                f"character_voice_binding 必须是 {sorted(VALID_CHARACTER_VOICE_BINDINGS)} 之一或 null,收到 {value!r}"
            )
        return value
    if key == "narration_voice":
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"narration_voice 必须是非空字符串或 null,收到 {value!r}")
        return value
    if key == "narration_speed":
        if value is None:
            return None
        if isinstance(value, str):
            value = _coerce_numeric_string(value, float, f"narration_speed 必须是正的有限数值或 null,收到 {value!r}")
        is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
        try:
            is_valid = is_number and math.isfinite(value) and value > 0
        except OverflowError:
            is_valid = False
        if not is_valid:
            raise ValueError(f"narration_speed 必须是正的有限数值或 null,收到 {value!r}")
        return value
    raise ValueError(f"settings 字段 {key!r} 缺类型校验")


def _format_settings(changes: dict[str, tuple[str, Any]]) -> str:
    set_items = [(key, value) for key, (operation, value) in changes.items() if operation == "set"]
    cleared = [key for key, (operation, _) in changes.items() if operation == "clear"]
    unchanged = [key for key, (operation, _) in changes.items() if operation == "noop"]
    parts = []
    if set_items:
        parts.append("已更新 " + ", ".join(f"{key}={value}" for key, value in set_items))
    if cleared:
        parts.append("已清除 " + ", ".join(cleared))
    if unchanged:
        parts.append("无变更 " + ", ".join(unchanged))
    icon = "ℹ️" if not set_items and not cleared else "✅"
    return f"{icon} settings: {'; '.join(parts) if parts else '无变更'}"


def _format_upsert(table: str, result: dict[str, Any]) -> str:
    added = sorted(result.get("added") or [])
    merged = sorted(result.get("merged") or [])
    noop = sorted(result.get("noop") or [])
    dropped_fields = result.get("dropped_fields") or {}
    dropped_legacy = result.get("dropped_legacy") or {}
    parts = []
    if added:
        parts.append(f"新增 {len(added)} 个: {', '.join(added)}")
    if merged:
        parts.append(f"合并改字段 {len(merged)} 个: {', '.join(merged)}")
    if noop:
        parts.append(f"无可写字段已跳过 {len(noop)} 个: {', '.join(noop)}")
    lines = [
        f"{'ℹ️' if not added and not merged else '✅'} {table}: {'; '.join(parts) if parts else '无变更（所有条目均无可写字段）'}"
    ]
    if dropped_fields:
        detail = "; ".join(f"{name}: {', '.join(fields)}" for name, fields in sorted(dropped_fields.items()))
        lines += [
            f"⚠️  以下字段不在 Agent 可编辑范围,已忽略 → {detail}",
            "   说明: reference_image 由用户上传/系统管理;",
            "   character_sheet / scene_sheet / prop_sheet 由资产生成流水线回写,不可手动设置。",
        ]
    if dropped_legacy:
        detail = "; ".join(f"{name}: {', '.join(fields)}" for name, fields in sorted(dropped_legacy.items()))
        lines.append(f"ℹ️  以下历史字段已废弃,本次未持久化 → {detail}")
    return "\n".join(lines)


def _patch_project_sync(
    request: ToolRequest[PatchProjectRequest],
    scope: ProjectScope,
    services: Services,
) -> ToolOutcome[PatchProjectResult]:
    value = request.value
    try:
        if value.settings is not None:
            coerced = {}
            for key, field_value in value.settings.items():
                if key not in PROJECT_SETTINGS:
                    raise ValueError(f"settings 字段 {key!r} 不在白名单 {list(PROJECT_SETTINGS)} 内")
                coerced[key] = _coerce_setting_value(key, field_value)
            diagnostics: dict[str, tuple[str, Any]] = {}

            def mutate_settings(project_data: dict[str, Any]) -> None:
                for key, field_value in coerced.items():
                    current = project_data.get(key)
                    if field_value is None:
                        diagnostics[key] = ("clear", None) if key in project_data else ("noop", None)
                        project_data.pop(key, None)
                    elif current == field_value:
                        diagnostics[key] = ("noop", current)
                    else:
                        diagnostics[key] = ("set", field_value)
                        project_data[key] = field_value
                try:
                    validate_project_narration_config(project_data)
                except NarrationConfigError as exc:
                    raise ValueError(
                        f"项目的旁白交付方式是 TTS 配音，TTS 快照必须完整，本次修改被拒绝（{exc.code}）"
                    ) from exc

            services.projects.update_project(scope.project_name, mutate_settings)
            return ToolOutcome(
                value=PatchProjectResult(
                    message=_format_settings(diagnostics), operation="settings", changes=diagnostics
                )
            )
        assert value.table is not None
        assert value.entries is not None
        changes = services.projects.upsert_assets(scope.project_name, value.table, value.entries)
        return ToolOutcome(
            value=PatchProjectResult(message=_format_upsert(value.table, changes), operation="assets", changes=changes)
        )
    except Exception as exc:
        return ToolOutcome(problem=_unexpected("patch_project", exc))


async def patch_project(
    request: ToolRequest[PatchProjectRequest],
    scope: ProjectScope,
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[PatchProjectResult]:
    return await _run_sync_transaction(_patch_project_sync, request, scope, services)


def _patch_episode_meta_sync(
    request: ToolRequest[PatchEpisodeMetaRequest],
    scope: ProjectScope,
    services: Services,
) -> ToolOutcome[PatchEpisodeMetaResult]:
    value = request.value
    try:
        with services.projects.locked_script(scope.project_name, value.script) as script:
            script[value.field] = value.value
    except Exception as exc:
        return ToolOutcome(problem=_unexpected("patch_episode_meta", exc))
    return ToolOutcome(
        value=PatchEpisodeMetaResult(
            message=f"✅ 已更新分集{value.field}为「{value.value}」",
            script=value.script,
            field=value.field,
            value=value.value,
        )
    )


async def patch_episode_meta(
    request: ToolRequest[PatchEpisodeMetaRequest],
    scope: ProjectScope,
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[PatchEpisodeMetaResult]:
    return await _run_sync_transaction(_patch_episode_meta_sync, request, scope, services)


def _merge_episode_line(project: Mapping[str, Any], impact: AssetMergeEpisodeImpact) -> str:
    counts = (
        ("脚本规划", impact.script_plan),
        ("正式脚本", impact.script),
        ("草稿", impact.draft),
        ("提示词正文", impact.prompt_text),
        ("说话人", impact.speaker),
    )
    references = "、".join(f"{label} {count} 处" for label, count in counts if count) or "无引用改写"
    return (
        f"- {describe_episode_for_agent(project, impact.episode)}：{references}；"
        f"过期分镜图 {impact.storyboards} 张、视频 {impact.videos} 段"
    )


def _merge_asset_message(project: Mapping[str, Any], report: AssetMergeReport) -> str:
    how = f"并为 {report.target!r} 的衍生" if report.as_derivative else f"并入 {report.target!r}"
    head = (
        f"预览：把 {report.table} 资产 {report.source!r} {how}，将改写 {report.references} 处引用。"
        if report.dry_run
        else f"已把 {report.table} 资产 {report.source!r} {how}，改写 {report.references} 处引用。"
    )
    lines = [head, *(_merge_episode_line(project, impact) for impact in report.episodes)]
    if report.aliases_added:
        lines.append("追加为保留方别名：" + "、".join(report.aliases_added))
    if report.derivative_created is not None:
        lines.append(f"新建衍生 {report.derivative_created!r}，资产图待生成")
    if report.derivatives_moved:
        lines.append("迁到保留方名下的衍生：" + "、".join(report.derivatives_moved))
    if report.derivatives_folded:
        lines.append("与保留方已有衍生同名、并入已有衍生：" + "、".join(report.derivatives_folded))
    lines.append(
        "被并方的描述、资产图及版本历史、声音设置、原图与参考音频不保留。"
        if report.dry_run
        else "被并方的描述、资产图及版本历史、声音设置、原图与参考音频已删除。"
    )
    return "\n".join(lines)


async def merge_asset(
    request: ToolRequest[MergeAssetRequest],
    scope: ProjectScope,
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[MergeAssetResult]:
    value = request.value

    def _merge() -> tuple[AssetMergeReport, dict[str, Any]]:
        report = services.projects.merge_asset(
            scope.project_name,
            value.table,
            value.source,
            value.target,
            as_derivative=value.as_derivative,
            dry_run=value.dry_run,
        )
        return report, services.projects.load_project(scope.project_name)

    try:
        report, project = await _run_sync_transaction(_merge)
    except AssetMergeNotFoundError as exc:
        return ToolOutcome(problem=ToolProblem("invalid_request", f"{value.table} 中不存在名为 {exc.name!r} 的资产"))
    except AssetMergeRejectedError as exc:
        return ToolOutcome(problem=ToolProblem("invalid_request", str(exc)))
    except Exception as exc:
        return ToolOutcome(problem=_unexpected("merge_asset", exc))
    return ToolOutcome(
        value=MergeAssetResult(
            message=_merge_asset_message(project, report),
            table=value.table,
            source=report.source,
            target=report.target,
            as_derivative=report.as_derivative,
            dry_run=report.dry_run,
            references=report.references,
            aliases_added=list(report.aliases_added),
            derivative_created=report.derivative_created,
            derivatives_moved=list(report.derivatives_moved),
            derivatives_folded=list(report.derivatives_folded),
            episodes=[
                MergeAssetEpisodeImpact(
                    episode_id=impact.episode,
                    script_plan=impact.script_plan,
                    script=impact.script,
                    draft=impact.draft,
                    prompt_text=impact.prompt_text,
                    speaker=impact.speaker,
                    storyboards=impact.storyboards,
                    videos=impact.videos,
                )
                for impact in report.episodes
            ],
        )
    )


async def rename_asset(
    request: ToolRequest[RenameAssetRequest],
    scope: ProjectScope,
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[RenameAssetResult]:
    value = request.value
    try:
        report = await _run_sync_transaction(
            services.projects.rename_asset,
            scope.project_name,
            value.table,
            value.old_name,
            value.new_name,
        )
    except Exception as exc:
        return ToolOutcome(problem=_unexpected("rename_asset", exc))
    message = (
        f"已把 {value.table} 资产 {report.old_name!r} 重命名为 {report.new_name!r}:"
        f"更新 {report.episodes} 集共 {report.references} 处引用,迁移 {report.files} 个关联文件。"
    )
    return ToolOutcome(
        value=RenameAssetResult(
            message=message,
            table=value.table,
            old_name=report.old_name,
            new_name=report.new_name,
            episodes=report.episodes,
            references=report.references,
            files=report.files,
        )
    )


async def retry_project_migration(
    _request: ToolRequest[NoArguments],
    scope: ProjectScope,
    _caller: CallerContext,
    services: Services,
) -> ToolOutcome[RetryProjectMigrationResult]:
    project_dir = services.projects.get_project_path(scope.project_name)
    try:
        failure = await _run_sync_transaction(
            migrate_project_with_verdict,
            project_dir,
            recorded_episode_ids=recorded_episode_ids_on(asyncio.get_running_loop()),
        )
        if failure is not None:
            return ToolOutcome(problem=_migration_tool_problem(failure))
        plan = await services.workflow_planner.get_plan(
            scope.project_name,
            WorkflowPlanRequest(),
            user_id=_caller.user_id,
            queue=services.queue,
            config_resolver=services.capabilities,
        )
    except Exception as exc:
        try:
            residual = load_migration_failure(project_dir)
        except (FileNotFoundError, ValueError, OSError):
            residual = None
        return ToolOutcome(
            problem=_migration_tool_problem(residual) if residual else _unexpected("retry_project_migration", exc)
        )
    return ToolOutcome(
        value=RetryProjectMigrationResult(
            message="✅ 数据升级已完成，项目解除阻断。当前制作计划：\n" + plan.model_dump_json(),
            workflow_plan=plan,
        )
    )


__all__ = [
    "ASSET_TABLES",
    "EPISODE_META_FIELDS",
    "MERGEABLE_ASSET_TABLES",
    "PROJECT_SETTINGS",
    "CallerContext",
    "CreateProjectToolRequest",
    "EpisodeScriptContent",
    "EpisodeScriptRequest",
    "GenerateEpisodeScriptRequest",
    "GenerationBatchToolRequest",
    "MergeAssetRequest",
    "NoArguments",
    "PatchEpisodeMetaRequest",
    "PatchEpisodeScriptRequest",
    "PatchProjectRequest",
    "ProjectContent",
    "ProjectFileContent",
    "ProjectFileEntry",
    "ProjectFileRequest",
    "ProjectFilesContent",
    "ProjectScope",
    "PromptPreviewRequest",
    "RenameAssetRequest",
    "ScriptPatchResult",
    "ScriptSpeechAdmission",
    "ScriptSpeechProblem",
    "Services",
    "SourceFilesContent",
    "SourceTextContent",
    "SourceTextRequest",
    "TextGenerationError",
    "TextGenerationRequest",
    "TextGenerationResult",
    "ToolOutcome",
    "ToolProblem",
    "ToolRequest",
    "cancel_generation_batch",
    "create_project",
    "generate_episode_script",
    "get_episode_script",
    "get_generation_batch",
    "get_project_content",
    "get_prompt_preview",
    "get_source_text",
    "get_video_capabilities",
    "get_workflow_plan",
    "list_project_files",
    "list_projects",
    "list_source_files",
    "merge_asset",
    "patch_episode_meta",
    "patch_episode_script",
    "patch_project",
    "read_project_file",
    "rename_asset",
    "retry_project_migration",
]
