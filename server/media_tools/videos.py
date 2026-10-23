"""Host-neutral tools for video generation (episode / scene / all / selected)."""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from lib.artifacts.artifact_activation import (
    ArtifactCurrencyResolver,
    active_artifact_currency_resolver,
    resolve_artifact_episode,
)
from lib.artifacts.artifact_manifest import ArtifactStatus
from lib.episode.episode_ids import describe_episode_for_agent
from lib.generation.batch_admission import (
    BatchAdmission,
    UnitAdmissionTicket,
    refused_ticket,
)
from lib.generation.generation_queue_client import (
    BatchTaskResult,
    TaskSpec,
)
from lib.generation.generation_result import (
    GenerationAction,
    GenerationBatchResult,
    GenerationCandidate,
    GenerationProblemCode,
    GenerationResultBuilder,
    GenerationSelectionMode,
    GenerationTargetState,
    artifact_is_reusable,
    normalize_requested_ids,
    record_batch_outcomes,
    select_generation_targets,
)
from lib.generation.video_request_facts import VideoRequestFacts, VideoRequestFactsFailure
from lib.project.project_manager import ProjectManager
from lib.script.factory_episode import require_factory_script
from lib.script.script_models import resolve_content_mode
from lib.script.script_skeleton import ensure_route_skeleton, resolve_script_kind
from lib.script.storyboard_sequence import get_storyboard_items
from lib.speech.speech_composition import (
    SpeechAdmissionError,
)
from server.media_tools.context import (
    GenerationToolValue,
    RequestedIds,
    ScriptFilename,
    generation_batch_submission_outcome,
    generation_result_outcome,
    tool_error,
)
from server.services.admission.video_batch_admission import (
    admit_storyboard_video_request,
    artifact_state_tickets,
    build_storyboard_video_specs,
    resolve_voice_context,
    screen_storyboard_items,
    storyboard_item_aliases,
    storyboard_item_id,
    storyboard_video_request_facts,
    video_target_states,
)
from server.services.admission.video_quote import quote_sheet, quote_storyboard_video_units
from server.tool_runtime import (
    CallerContext,
    ProjectScope,
    Services,
    ToolOutcome,
    ToolProblem,
    ToolRequest,
    submit_media_generation,
)

logger = logging.getLogger(__name__)

#: 已退役的参数只返回当前分镜工具的字段指引。
_RETIRED_PARAMS: dict[str, str] = {
    "resume": "查询 durable batch，并用 selected scope、force=false 只重发未成功的 ID",
    "shot_ids": "改用 target.ids，并选择 scene（单个）或 selected（批量）scope",
    "unit_id": "改用 target.ids 的分镜 scene_id",
    "unit_ids": "改用 target.ids 的分镜 scene_id 列表",
}


def _reject_retired_params(args: Mapping[str, Any]) -> None:
    """入参里出现已退役的参数名时 fail loud，并指明当下该怎么写。

    Raises:
        ValueError: 命中 :data:`_RETIRED_PARAMS`。
    """
    for name, guidance in _RETIRED_PARAMS.items():
        if name in args:
            raise ValueError(f"参数 {name!r} 已不存在：{guidance}")


_PositiveInt = Annotated[StrictInt, Field(ge=1)]


class EpisodeTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Literal["episode"] = Field(description="整集：只补缺视频的单元，已有可用成片一律复用")
    episode_id: _PositiveInt = Field(description="集 ID，须与 script 所属那一集的集 ID 一致")


class AllTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Literal["all"] = Field(description="剧本内全部单元：只补缺视频的单元，已有可用成片一律复用")


class SceneTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Literal["scene"] = Field(description="单个单元")
    ids: list[str] = Field(
        min_length=1,
        max_length=1,
        description="恰好一个分镜 scene_id",
    )


class SelectedTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Literal["selected"] = Field(description="点名的一批单元")
    ids: RequestedIds = Field(description="分镜 scene_id 列表")


VideoTarget = Annotated[
    EpisodeTarget | AllTarget | SceneTarget | SelectedTarget,
    Field(discriminator="scope"),
]


class GenerateVideosRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    script: ScriptFilename = Field(description="剧本纯文件名（不含目录），如 episode_1.json")
    target: VideoTarget = Field(description="目标选择器；scope 取 episode / all / scene / selected")
    force: StrictBool = Field(
        default=False,
        description="是否强制重生已有可用成片；默认复用 current / stale 成片，只允许用于 scene / selected",
    )
    preview: StrictBool = Field(
        default=False,
        description=(
            "true 时只预检、不入队：按正式提交同一份准入，返回 video_quote 报价单"
            "（逐 unit 的去向、编排时长、申请档位、是否变档、预计费用与缺口）。"
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _refuse_retired_params(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            _reject_retired_params(data)
        return data

    @model_validator(mode="after")
    def _force_needs_explicit_ids(self) -> Self:
        if self.force and self.target.scope not in ("scene", "selected"):
            raise ValueError("force=true 只允许用于带显式 ID 的 scene/selected scope")
        return self


@dataclass(frozen=True, slots=True)
class _VideoCall:
    """一次视频工具调用：目标项目、调用方与协作者。"""

    scope: ProjectScope
    caller: CallerContext
    services: Services
    #: 只预检、不入队：走到整批准入为止，把结论折成报价单返回。
    preview: bool = False

    @property
    def project_name(self) -> str:
        return self.scope.project_name

    @property
    def projects(self) -> ProjectManager:
        return self.services.projects

    @property
    def project_path(self) -> Path:
        return self.services.projects.get_project_path(self.scope.project_name)

    def stop_on_failure_caller(self) -> CallerContext:
        """等待器在批次内任一任务失败即停止等待其余任务的调用方。"""
        return self.caller.waiting_with(stop_on_failure=True)


_OPERATION = "generate_videos"


def _state_for(states: dict[str, GenerationTargetState], unit_id: str) -> GenerationTargetState:
    return states.get(unit_id) or GenerationTargetState(candidate=GenerationCandidate(unit_id=unit_id))


def _missing_only_reusable_ids(states: dict[str, GenerationTargetState]) -> list[str]:
    """Missing-only 下原样保留的分镜：Manifest 认定 current / stale（含已登记的上传视频）。"""

    return [unit_id for unit_id, state in states.items() if artifact_is_reusable(state)]


def _sole_speech_admission(result: GenerationBatchResult) -> dict[str, Any]:
    """整个请求只卡在一个单元的发声准入上时，把准入载荷原样带回响应顶层。

    单元级的定位信息（哪一句台词、哪条路径）比逐 ID 契约细一层，点名单个单元的调用方
    需要它才能一步定位到要改的地方；批量请求没有「这一个」单元可指，就不带。
    """

    admissions = [
        item.problem.params["speech_admission"]
        for item in result.items
        if item.problem is not None and "speech_admission" in item.problem.params
    ]
    if len(result.requested) == 1 and len(admissions) == 1:
        return {"speech_admission": admissions[0]}
    return {}


def _speech_admission_error(name: str, exc: SpeechAdmissionError, log: list[str] | None = None) -> ToolOutcome[Any]:
    payload = exc.admission.to_dict()
    text = f"{name} 失败: unit {exc.admission.unit_id} 发声准入未通过；请按 problems 的 action 修复"
    if log:
        text = "\n".join([text, *log])
    return ToolOutcome(problem=ToolProblem("speech_admission_refused", text, params={"speech_admission": payload}))


@dataclass(frozen=True)
class BatchAdmissionRefused:
    """整批准入未通过：本次调用不产生任何任务。"""

    admission: BatchAdmission


def _preview_outcome(
    admission: BatchAdmission | None,
    builder: GenerationResultBuilder,
    log: list[str],
    *,
    storyboard_durations: Mapping[str, object] | None = None,
    storyboard_costs: Mapping[str, Mapping[str, object] | None] | None = None,
) -> ToolOutcome[Any]:
    """把预检的准入结论与复用记名折成报价单响应。"""

    sheet = quote_sheet(
        admission,
        reused_ids=[item.unit_id for item in builder.build().skipped],
        storyboard_durations=storyboard_durations,
        storyboard_costs=storyboard_costs,
    )
    return ToolOutcome(value={"video_quote": sheet.to_payload(), "summary": sheet.summary(log)})


def _blocked_lines(admission: BatchAdmission) -> list[str]:
    lines = ["本次批量请求未通过准入，未入队任何任务。逐 unit 缺口如下："]
    for ticket in admission.refused_tickets:
        lines.extend(
            f"- {ticket.unit_id}：{problem.code}（下一步：{problem.action.value}）{problem.detail}"
            for problem in ticket.problems
        )
    lines.append("修复全部缺口后重试即可一次性提交整批。")
    return lines


def _batch_admission_response(
    refusal: BatchAdmissionRefused,
    log: list[str],
    builder: GenerationResultBuilder,
    states: dict[str, GenerationTargetState] | None = None,
) -> ToolOutcome[Any]:
    """转述整批拒绝：零任务入队，每个目标都带自己的机器可读结论。

    待确认档位是入队前的正常拦截点，不是异常——``is_error`` 不跟着 blocked 走，
    这批 unit 仍等待用户决定，不代表请求失败。
    """

    admission = refusal.admission
    admission.record_refusal(builder, states=states)
    lines = [*log, *_blocked_lines(admission)]
    result = builder.build()
    payload: dict[str, Any] = {
        "generation_result": result,
        "batch_admission": admission.to_payload(),
        "request_projections": admission.projections(),
        **_sole_speech_admission(result),
        "summary": "\n".join(lines),
    }
    return ToolOutcome(value=payload)


async def _admit_storyboard_specs(
    *,
    call: _VideoCall,
    project: dict[str, Any],
    script: dict[str, Any],
    script_filename: str,
    items: list[dict[str, Any]],
    id_field: str,
    specs: list[TaskSpec],
    operation: str,
    selection: GenerationSelectionMode,
    extra_tickets: list[UnitAdmissionTicket],
    video_request_facts: VideoRequestFacts | VideoRequestFactsFailure | None = None,
) -> BatchAdmission:
    """Admit the Storyboard-mode specs.

    The admission is the shared one the read-only plan also consults, so a preview
    and the submission it predicts cannot reach different verdicts.
    """

    return await admit_storyboard_video_request(
        project_name=call.project_name,
        project=project,
        script_file=script_filename,
        items=items,
        id_field=id_field,
        specs=specs,
        operation=operation,
        selection=selection,
        extra_tickets=extra_tickets,
        user_id=call.caller.user_id,
        queue=call.services.queue,
        config_resolver=call.services.capabilities,
        video_request_facts=video_request_facts,
        project_dir=call.projects.get_project_path(call.project_name),
        storyboard_items=get_storyboard_items(script)[0],
        episode=ProjectManager.resolve_episode_from_script(script, script_filename),
        content_mode=resolve_content_mode(script, project),
    )


def _scene_fallback_relpath(resource_id: str) -> str:
    return f"videos/scene_{resource_id}.mp4"


@dataclass(frozen=True)
class _VideoRequestContext:
    """视频工具的请求前导：入参投影、剧本与生成模式判定。"""

    script_filename: str
    project_dir: Path
    script: dict[str, Any]


def _video_request_context(call: _VideoCall, request: GenerateVideosRequest) -> _VideoRequestContext:
    """把入参折成本次请求的投影选项、载入剧本，并定下走哪条生成模式。"""

    script = call.projects.load_script(call.project_name, request.script)
    project = call.projects.load_project(call.project_name)
    if project.get("content_mode") != "drama" or project.get("generation_mode") != "storyboard":
        raise ValueError("video generation requires a short-drama factory project")
    ensure_route_skeleton(script, "drama", "storyboard")
    require_factory_script(
        call.project_path, project, script, ProjectManager.resolve_episode_from_script(script, request.script)
    )
    return _VideoRequestContext(
        script_filename=request.script,
        project_dir=call.project_path,
        script=script,
    )


@dataclass(frozen=True)
class _StoryboardScreening:
    """分镜图生视频的目标条目、ID 字段、骨架种类与筛查记名。"""

    items: list[dict[str, Any]]
    id_field: str
    skeleton_kind: str
    refused: list[UnitAdmissionTicket]


def _screen_script_targets(
    script: dict[str, Any],
    *,
    requested_ids: Collection[str] | None,
) -> _StoryboardScreening:
    """取分镜条目并筛掉成不了目标的脏条目。

    骨架种类取剧本实际形态（与生成模式闸门同一份判别），族内历史形态才不会被按创作类型
    反推的种类误判成解析失败。
    """

    items, id_field, _chars, _scenes, _props = get_storyboard_items(script)
    skeleton_kind = resolve_script_kind(script)
    items, screen_refused = screen_storyboard_items(items, id_field, requested_ids=requested_ids)
    return _StoryboardScreening(
        items=items,
        id_field=id_field,
        skeleton_kind=skeleton_kind,
        refused=screen_refused,
    )


@dataclass(frozen=True)
class _StoryboardContext:
    """分镜图生视频的项目侧上下文：项目、集号与创作类型。"""

    project: dict[str, Any]
    episode: int
    content_mode: str


def _storyboard_context(call: _VideoCall, request: _VideoRequestContext) -> _StoryboardContext:
    project = call.projects.load_project(call.project_name)
    episode = resolve_artifact_episode(
        project=project,
        script=request.script,
        script_filename=request.script_filename,
    )
    return _StoryboardContext(
        project=project,
        episode=episode,
        content_mode=resolve_content_mode(request.script, project),
    )


@dataclass(frozen=True)
class _StoryboardBatch:
    """分镜图生视频一次请求的批次上下文：目标口径、准入身份与结果构造器。

    四种 scope 的目标集合与提交方式各不相同，但构造 TaskSpec、整批准入与逐目标记录
    结论这三步共用同一份口径——集中在这里，改一处判定不会只改到其中一个入口。
    """

    call: _VideoCall
    request: _VideoRequestContext
    sb: _StoryboardContext
    screening: _StoryboardScreening
    resolver: ArtifactCurrencyResolver
    operation: str
    selection: GenerationSelectionMode
    builder: GenerationResultBuilder
    states: dict[str, GenerationTargetState]
    log: list[str]

    def result(self) -> ToolOutcome[Any]:
        """把已记录的逐目标结论折成响应；预检时折成报价单。"""

        if self.call.preview:
            return _preview_outcome(None, self.builder, self.log)
        return generation_result_outcome(self.builder.build(), self.log)

    async def build_specs(
        self,
        *,
        items: list[dict[str, Any]],
        skip_ids: list[str] | None,
    ) -> tuple[list[TaskSpec], list[UnitAdmissionTicket]]:
        """按本次请求的目标条目构造分镜图生视频的 TaskSpec。"""

        voice_characters = await resolve_voice_context(self.sb.project, self.sb.content_mode)
        return build_storyboard_video_specs(
            items=items,
            id_field=self.screening.id_field,
            content_mode=self.sb.content_mode,
            skeleton_kind=self.screening.skeleton_kind,
            script_filename=self.request.script_filename,
            project_dir=self.request.project_dir,
            project=self.sb.project,
            episode=self.sb.episode,
            resolver=self.resolver,
            skip_ids=skip_ids,
            voice_characters=voice_characters,
            storyboard_items=self.screening.items,
        )

    def _record(self, successes: list[BatchTaskResult], failures: list[BatchTaskResult]) -> None:
        record_batch_outcomes(
            self.builder,
            successes=successes,
            failures=failures,
            states=self.states,
            resolver=self.resolver,
            fallback_path=_scene_fallback_relpath,
        )

    async def admit_and_submit(
        self,
        *,
        items: list[dict[str, Any]],
        specs: list[TaskSpec],
        extra_tickets: list[UnitAdmissionTicket],
    ) -> ToolOutcome[Any]:
        """整批准入后提交；准入未通过则零任务入队地转述拒绝，预检则只报价。"""

        # 准入与预检报价读同一份视频请求事实，两次求值之间配置一变，报价就不再是准入认的那一个。
        facts = (
            await storyboard_video_request_facts(self.sb.project, self.call.services.capabilities) if specs else None
        )
        admission = await _admit_storyboard_specs(
            call=self.call,
            project=self.sb.project,
            script=self.request.script,
            script_filename=self.request.script_filename,
            items=items,
            id_field=self.screening.id_field,
            specs=specs,
            operation=self.operation,
            selection=self.selection,
            extra_tickets=extra_tickets,
            video_request_facts=facts,
        )
        if self.call.preview:
            items_by_id = {str(storyboard_item_id(item, self.screening.id_field) or ""): item for item in items}
            durations = {spec.resource_id: items_by_id[spec.resource_id].get("duration_seconds") for spec in specs}
            return _preview_outcome(
                admission,
                self.builder,
                self.log,
                storyboard_durations=durations,
                storyboard_costs=await quote_storyboard_video_units(facts, durations),
            )
        if not admission.admitted:
            return _batch_admission_response(BatchAdmissionRefused(admission), self.log, self.builder, self.states)

        for spec in specs:
            spec.unit_id = spec.resource_id
            spec.source = self.call.caller.source

        submitted = await submit_media_generation(
            scope=self.call.scope,
            caller=self.call.stop_on_failure_caller(),
            services=self.call.services,
            operation=self.operation,
            preflight=self.builder.build(),
            pending_ids=[spec.resource_id for spec in specs],
            specs=specs,
            states=self.states,
            admission={str(item["unit_id"]): item for item in admission.projections()},
        )
        if submitted.successes is None or submitted.failures is None:
            return generation_batch_submission_outcome(submitted.batch)
        self._record(submitted.successes, submitted.failures)
        return generation_result_outcome(self.builder.build(), self.log, batch_id=submitted.batch.batch_id)


def _check_target_episode(call: _VideoCall, request: _VideoRequestContext, episode: int) -> None:
    """整集选择器点名的集 ID 必须就是剧本所属的那一集：点错集不能按剧本那一集花钱。"""

    project = call.projects.load_project(call.project_name)
    actual_episode = resolve_artifact_episode(
        project=project,
        script=request.script,
        script_filename=request.script_filename,
    ) or ProjectManager.resolve_episode_from_script(request.script, request.script_filename)
    if episode != actual_episode:
        raise ValueError(f"target.episode_id={episode} 与剧本所属的集 ID {actual_episode} 不一致")


async def _generate_episode(call: _VideoCall, request: _VideoRequestContext, log: list[str]) -> ToolOutcome[Any]:
    project_dir = request.project_dir

    screening = _screen_script_targets(request.script, requested_ids=None)
    items, id_field, screen_refused = screening.items, screening.id_field, screening.refused
    sb = _storyboard_context(call, request)
    episode = sb.episode
    if not items and not screen_refused:
        raise ValueError(f"{describe_episode_for_agent(sb.project, episode)} 的剧本为空：{request.script_filename}")

    currency = active_artifact_currency_resolver(project_dir, sb.project)
    states = video_target_states(items, id_field, episode=episode, resolver=currency)
    # 整集生成只补缺失，从不强制重生：仍可用的旧分镜（含 stale 与上传的视频）原样保留。
    already_done = _missing_only_reusable_ids(states)
    builder = GenerationResultBuilder(_OPERATION, GenerationSelectionMode.MISSING_ONLY)
    batch = _StoryboardBatch(
        call=call,
        request=request,
        sb=sb,
        screening=screening,
        resolver=currency,
        operation=_OPERATION,
        selection=GenerationSelectionMode.MISSING_ONLY,
        builder=builder,
        states=states,
        log=log,
    )
    for done_id in already_done:
        state = _state_for(states, str(done_id))
        builder.skip(state)

    # currency 之外的第三态：Manifest 读不出该分镜的产物状态（BLOCKED），既不能
    # 判定为可复用（进 already_done）也不能安全当作缺失去入队——不可读不等于没有，
    # 花钱重生可能覆盖一份实际仍然可用的分镜。all scope 走
    # select_generation_targets 已经把这一态折进 selection.unavailable，这里是
    # 同一场判定手写的另一条腿，必须同步处理。
    already_done_set = set(already_done)
    blocked_states = [
        state
        for unit_id, state in states.items()
        if unit_id not in already_done_set and state.status == ArtifactStatus.BLOCKED
    ]
    blocked_ids = [state.unit_id for state in blocked_states]
    refused = artifact_state_tickets(blocked_states)
    refused.extend(screen_refused)

    specs, spec_refused = await batch.build_specs(
        items=items,
        skip_ids=[*already_done, *blocked_ids],
    )
    refused.extend(spec_refused)

    if not specs and not refused and not builder.recorded_ids:
        raise RuntimeError("没有可生成的分镜")

    return await batch.admit_and_submit(
        items=items,
        specs=specs,
        extra_tickets=refused,
    )


async def _generate_all(call: _VideoCall, request: _VideoRequestContext, log: list[str]) -> ToolOutcome[Any]:
    project_dir = request.project_dir

    screening = _screen_script_targets(request.script, requested_ids=None)
    items, id_field, screen_refused = screening.items, screening.id_field, screening.refused
    sb = _storyboard_context(call, request)
    currency = active_artifact_currency_resolver(project_dir, sb.project)
    states = video_target_states(items, id_field, episode=sb.episode, resolver=currency)
    selection = select_generation_targets(
        candidates=[state.candidate for state in states.values()],
        requested_ids=None,
        resolver=currency,
    )
    # 产物状态不可读的目标由准入报告（折成准入票），结果契约里不重复记录：
    # 同一个 unit 记两次会让结果构造器 fail loud。
    unavailable_tickets = artifact_state_tickets(selection.unavailable)
    builder = GenerationResultBuilder.from_selection(_OPERATION, replace(selection, unavailable=()))
    batch = _StoryboardBatch(
        call=call,
        request=request,
        sb=sb,
        screening=screening,
        resolver=currency,
        operation=_OPERATION,
        selection=GenerationSelectionMode.MISSING_ONLY,
        builder=builder,
        states=states,
        log=log,
    )
    if not selection.targets and not unavailable_tickets and not screen_refused:
        if call.preview:
            return batch.result()
        submitted = await submit_media_generation(
            scope=call.scope,
            caller=call.caller,
            services=call.services,
            operation=_OPERATION,
            preflight=builder.build(),
            pending_ids=[],
            specs=[],
            states=states,
        )
        if submitted.successes is None:
            return generation_batch_submission_outcome(submitted.batch)
        return generation_result_outcome(
            builder.build(),
            log,
            batch_id=submitted.batch.batch_id,
        )

    # 与 ``_video_target_states`` 用同一套 ID 回退规则：条目若缺 ``id_field``
    # 但带 ``scene_id``/``segment_id``，selection 已按回退 ID 记为 target，
    # 这里若只认 ``id_field`` 会把它筛没——进了 requested 却永远不入队。
    target_id_set = set(selection.target_ids)
    pending = [item for item in items if str(storyboard_item_id(item, id_field) or "") in target_id_set]
    specs, refused = await batch.build_specs(items=pending, skip_ids=None)
    # 产物状态不可读的分镜被选目标环节排除在 targets 之外，但它属于这次请求：
    # 不带进准入，同批健康的分镜会照常入队并计费，剩下这一个被无声略过。
    refused.extend(unavailable_tickets)
    refused.extend(screen_refused)
    if not specs and not refused:
        return batch.result()

    return await batch.admit_and_submit(
        items=pending,
        specs=specs,
        extra_tickets=refused,
    )


async def _generate_selected(
    call: _VideoCall,
    request: _VideoRequestContext,
    log: list[str],
    *,
    scene_ids: list[str],
    force: bool,
) -> ToolOutcome[Any]:
    project_dir = request.project_dir

    screening = _screen_script_targets(request.script, requested_ids=set(scene_ids))
    items, id_field, screen_refused = screening.items, screening.id_field, screening.refused
    sb = _storyboard_context(call, request)
    episode = sb.episode

    items_by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        # 按同一份「能寻址到它的写法」建索引：直接拿原值当键，脏剧本里的 list / dict
        # 别名会抛 TypeError，逐目标的结论就塌成一句通用报错。
        for alias in storyboard_item_aliases(item, id_field):
            items_by_id[alias] = item

    builder = GenerationResultBuilder(_OPERATION, GenerationSelectionMode.EXPLICIT)
    selected: list[dict[str, Any]] = []
    refused: list[UnitAdmissionTicket] = []
    seen_canonical: set[str] = set()
    # ``items_by_id`` 同时按 ``id_field`` 与 ``scene_id`` 索引同一个 item，
    # 调用方若把两个值都列入 ``scene_ids`` 会让同一分镜重复入队——必须按
    # 规范 ``id_field`` 再去一次重。
    screened_ids = {ticket.unit_id for ticket in screen_refused}
    for sid in scene_ids:
        if sid in screened_ids:
            # 筛查已经按这个名字记过一条结论，重复记名会撞上结果契约的唯一性。
            continue
        if sid not in items_by_id:
            refused.append(
                refused_ticket(
                    sid,
                    code=GenerationProblemCode.UNIT_NOT_FOUND,
                    detail=f"分镜 '{sid}' 不存在",
                    action=GenerationAction.FIX_INPUT,
                )
            )
            continue
        item = items_by_id[sid]
        canonical = str(item.get(id_field, ""))
        if canonical and canonical in seen_canonical:
            continue
        seen_canonical.add(canonical)
        selected.append(item)
    if not selected and not refused and not screen_refused:
        if call.preview:
            return _preview_outcome(None, builder, log)
        return generation_result_outcome(builder.build(), log)

    currency = active_artifact_currency_resolver(project_dir, sb.project)
    already_done: list[str] = []
    states = video_target_states(selected, id_field, episode=episode, resolver=currency)
    if not force:
        already_done = list(dict.fromkeys(state.unit_id for state in states.values() if artifact_is_reusable(state)))
    for done_id in already_done:
        builder.skip(_state_for(states, str(done_id)))
    batch = _StoryboardBatch(
        call=call,
        request=request,
        sb=sb,
        screening=screening,
        resolver=currency,
        operation=_OPERATION,
        selection=GenerationSelectionMode.EXPLICIT,
        builder=builder,
        states=states,
        log=log,
    )

    specs, spec_refused = await batch.build_specs(items=selected, skip_ids=already_done)
    refused.extend(spec_refused)
    refused.extend(screen_refused)

    return await batch.admit_and_submit(
        items=selected,
        specs=specs,
        extra_tickets=refused,
    )


async def generate_videos(
    request: ToolRequest[GenerateVideosRequest],
    scope: ProjectScope,
    caller: CallerContext,
    services: Services,
) -> ToolOutcome[GenerationToolValue]:
    args = request.value
    call = _VideoCall(scope=scope, caller=caller, services=services, preview=args.preview)
    target = args.target
    log: list[str] = []
    try:
        context = _video_request_context(call, args)
        if isinstance(target, EpisodeTarget):
            _check_target_episode(call, context, target.episode_id)
            return await _generate_episode(call, context, log)
        if isinstance(target, AllTarget):
            return await _generate_all(call, context, log)
        # 去重以避免同一 ID 重复入队；保留首次出现顺序便于人读日志。
        scene_ids = normalize_requested_ids(target.ids, field="target.ids") or []
        return await _generate_selected(call, context, log, scene_ids=scene_ids, force=args.force)
    except SpeechAdmissionError as exc:
        return _speech_admission_error(_OPERATION, exc, log)
    except Exception as exc:
        return tool_error(_OPERATION, exc, log)


__all__ = [
    "AllTarget",
    "EpisodeTarget",
    "GenerateVideosRequest",
    "SceneTarget",
    "SelectedTarget",
    "generate_videos",
]
