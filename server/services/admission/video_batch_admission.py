"""Project-state adapter for the all-or-nothing batch video admission.

Web and Agent batch entries share this seam. It resolves the current
request-projection, quote and queue state for every target of one request, then
folds the per-unit verdicts with :mod:`lib.generation.batch_admission`. Durable request
facts stay in ``lib.script.reference_video``; this module only adapts server state onto
them and never submits a task.

Both entries evaluate the same request options once — any confirmed request tiers —
so the two cannot reach different conclusions about the same project.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from lib.artifacts.artifact_activation import (
    ArtifactCurrencyResolver,
    active_artifact_currency_resolver,
)
from lib.artifacts.artifact_input_claims import resolve_usable_storyboard_video_slots
from lib.artifacts.artifact_manifest import ArtifactKey
from lib.config.resolver import ConfigResolver, video_bucket_for_generation_mode
from lib.db import async_session_factory
from lib.db.base import DEFAULT_USER_ID
from lib.generation.batch_admission import (
    BatchAdmission,
    UnitAdmissionTicket,
    refused_ticket,
)
from lib.generation.generation_queue import GenerationQueue
from lib.generation.generation_queue_client import TaskSpec, get_active_tasks_for_resources
from lib.generation.generation_result import (
    GenerationAction,
    GenerationCandidate,
    GenerationProblem,
    GenerationProblemCode,
    GenerationSelectionMode,
    GenerationTargetState,
    artifact_state_problem,
    observe_artifact_status,
)
from lib.generation.video_request_facts import (
    CONFIGURED_VIDEO_IDENTITY,
    VideoRequestFacts,
    VideoRequestFactsFailure,
    evaluate_video_request_facts,
)
from lib.prompts.prompt_utils import (
    is_structured_video_prompt,
    render_storyboard_video_prompt,
)
from lib.references.reference_admission import admit_storyboard_item
from lib.references.reference_catalog import build_reference_catalog
from lib.script.grid.keyframe_parser import keyframe_reference_problems
from lib.script.script_models import get_generated_assets
from lib.script.storyboard_sequence import StoryboardImageUnavailable, StoryboardVideoSlots
from lib.script.storyboard_video_request import resolve_storyboard_audio_inputs, storyboard_asset_links
from lib.speech.speech_composition import (
    SpeechAdmission,
    SpeechAdmissionError,
    require_script_unit_admitted,
    speech_policy_for_project,
)
from lib.speech.voice_settings import VoiceRenderSettings
from server.services.admission.reference_admission import reference_admission_problems
from server.services.tasks.video_caps import (
    AudioSwitchConflict,
    assert_audio_switch_supported,
    resolve_project_is_silent,
)


def video_target_states(
    units: Sequence[Any],
    id_field: str,
    *,
    episode: int,
    resolver: ArtifactCurrencyResolver,
) -> dict[str, GenerationTargetState]:
    """Observe each unit's video artifact standing once, for the whole request.

    Every video entry reports the artifact axis from this one observation, so a
    task result and the artifact's current/stale standing never come from two
    different reads.
    """

    states: dict[str, GenerationTargetState] = {}
    for unit in units:
        if not isinstance(unit, dict):
            continue
        unit_id = str(storyboard_item_id(unit, id_field) or "")
        if not unit_id or unit_id in states:
            continue
        artifact_path = get_generated_assets(unit).get("video_clip")
        key = ArtifactKey.episode_video(episode, unit_id)
        status, blocker = observe_artifact_status(resolver=resolver, key=key, artifact_path=artifact_path)
        states[unit_id] = GenerationTargetState(
            candidate=GenerationCandidate(
                unit_id=unit_id,
                artifact_key=key,
                artifact_path=artifact_path if isinstance(artifact_path, str) else None,
            ),
            status=status,
            blocker=blocker,
        )
    return states


def artifact_state_tickets(states: Sequence[GenerationTargetState]) -> list[UnitAdmissionTicket]:
    """把产物状态不可读的目标折成准入票，让它们参加同一场全有或全无的判定。

    这一态既不能判为可复用、也不能当作缺失去重生。只记进结果契约而不带进准入的话，
    同批健康的目标仍会入队并计费——那正是这道门要防的部分成批。
    """

    return [UnitAdmissionTicket(unit_id=state.unit_id, problems=(artifact_state_problem(state),)) for state in states]


def storyboard_item_id(item: dict[str, Any], id_field: str) -> object:
    """条目自报的 id，按各入口共用的回退顺序取。

    只在规范字段缺失或写成空串时才退到别名：规范字段写成 ``0`` / ``[]`` 这类非字符串时也退，
    等于让别名替它蒙混过筛查，条目按别名入队计费，而执行期只按规范字段定位，谁也做不出来。
    """

    canonical = item.get(id_field)
    if canonical is None or (isinstance(canonical, str) and not canonical.strip()):
        return item.get("scene_id") or item.get("segment_id")
    return canonical


def diagnostic_unit_id(base: str, taken: Collection[str]) -> str:
    """成不了目标的条目按位置记名，并保证这个名字不与剧本里的合法 id 相同。

    结果契约按 unit_id 记名一次：诊断名与某个真实 id 撞上时，两条不同的条目会被当作
    同一个，写第二遍时 fail loud，用户拿到的是一句通用报错而不是逐目标的结论。
    """

    unit_id = base
    while unit_id in taken:
        unit_id += "*"
    return unit_id


def storyboard_item_aliases(item: dict[str, Any], id_field: str) -> set[str]:
    """点名时能寻址到这个条目的全部写法。

    各入口既认规范 ``id_field`` 也认 ``scene_id`` 别名，两者在剧本里可以不同；按哪一个
    点名都要指到同一个条目，否则同一个名字在不同入口指向不同条目。
    """

    # 先按类型过滤再进集合：脏剧本里的 list / dict 别名不可哈希，直接建集合会抛 TypeError，
    # 逐目标的拒绝契约就塌成一句通用报错。
    aliases = (item.get(id_field), item.get("scene_id"))
    return {alias.strip() for alias in aliases if isinstance(alias, str) and alias.strip()}


def screen_storyboard_items(
    items: Sequence[Any],
    id_field: str,
    *,
    requested_ids: Collection[str] | None,
) -> tuple[list[dict[str, Any]], list[UnitAdmissionTicket]]:
    """把剧本条目分成「能当目标的」与「成不了目标的」两份，后者按位置记名。

    非对象条目、id 不是字符串、id 为空、以及同一个 id 出现多次，都会让后面按 id 索引的每一步
    失手：条目被静默滤掉时同批健康的目标独自入队计费，撞上集合查询时又把逐目标的拒绝契约打成
    一句通用报错。数字与布尔 id 混过 ``str()`` 进队列后，执行期按原值比对同样找不到目标。
    各入口在读 id 之前先经这一道筛，这些失手都变成一张记名的准入票。

    缺失即生成把整个剧本当作目标集合，剧本里任何一处脏条目都参与判定；点名生成的目标集合由
    调用方给定，只有点到的 id 上的脏（同一个 id 的副本）才参与，否则别处的脏数据会否决一次
    精确点名的重做。

    记名用带方括号的位置写法，与合法 id 不共用命名空间：同名会让结果契约把两条不同的条目
    当作同一个，写第二遍时 fail loud，用户拿到的又是一句通用报错。
    """

    clean: list[dict[str, Any]] = []
    tickets: list[UnitAdmissionTicket] = []
    seen: set[str] = set()
    addressable: list[tuple[dict[str, Any], str]] = []
    taken = {
        str(storyboard_item_id(item, id_field)).strip()
        for item in items
        if isinstance(item, dict) and isinstance(storyboard_item_id(item, id_field), str)
    }
    named = set(requested_ids) if requested_ids is not None else None
    if named is not None:
        # 点名的 ID 也占着记名空间：点到剧本里没有的名字时上游还会记一条「不存在」，
        # 诊断名与它同名会把两条并成一条。
        taken |= named
    refused_names: set[str] = set()
    for index, item in enumerate(items):
        detail: str | None = None
        item_id = ""
        if not isinstance(item, dict):
            detail = "该条目不是对象"
        else:
            raw_id = storyboard_item_id(item, id_field)
            if raw_id is not None and not isinstance(raw_id, str):
                detail = "该条目的 ID 不是字符串"
            else:
                item_id = (raw_id or "").strip()
                if not item_id:
                    detail = "该条目没有可用的 ID"
        if detail is not None:
            if named is None:
                tickets.append(
                    refused_ticket(
                        diagnostic_unit_id(f"items[{index}]", taken),
                        code=GenerationProblemCode.UNIT_REQUEST_INVALID,
                        detail=detail,
                        action=GenerationAction.FIX_INPUT,
                    )
                )
            elif isinstance(item, dict):
                # 点名点中的正好是这个脏条目：按点名的写法给结论，否则调用方只收到一句
                # 「不存在」，而这个名字在剧本里明明有条目。
                for name in sorted(storyboard_item_aliases(item, id_field) & named):
                    if name in refused_names:
                        continue
                    refused_names.add(name)
                    tickets.append(
                        refused_ticket(
                            name,
                            code=GenerationProblemCode.UNIT_REQUEST_INVALID,
                            detail=detail,
                            action=GenerationAction.FIX_INPUT,
                        )
                    )
            continue
        if named is not None:
            addressable.append((item, item_id))
            continue
        if item_id in seen:
            tickets.append(
                refused_ticket(
                    diagnostic_unit_id(f"{item_id}#{index}", taken),
                    code=GenerationProblemCode.UNIT_REQUEST_INVALID,
                    detail=f"ID {item_id} 在剧本中重复出现",
                    action=GenerationAction.FIX_INPUT,
                )
            )
            continue
        seen.add(item_id)
        clean.append(item)
    if named is None:
        return clean, tickets

    # 点名可以用规范 ID，也可以用 ``scene_id`` 别名，两者在剧本里可以不同；执行期按规范 ID
    # 定位目标。因此一个名字指到几个条目，要把「直接被它寻址的条目」连同「与之共用规范 ID
    # 的兄弟」一起数：只按名字数会漏掉别名不同、规范 ID 相同的那种，各入口按各自的查法分别
    # 选中头一个或末一个，同一次点名在不同入口做的是不同条目。
    by_canonical: dict[str, list[dict[str, Any]]] = {}
    for item, item_id in addressable:
        by_canonical.setdefault(item_id, []).append(item)
    ambiguous: set[int] = set()
    for name in sorted(named - refused_names):
        owners = {id(item) for item, _ in addressable if name in storyboard_item_aliases(item, id_field)}
        targets = {
            id(sibling) for item, item_id in addressable if id(item) in owners for sibling in by_canonical[item_id]
        }
        if len(targets) <= 1:
            continue
        ambiguous |= targets
        tickets.append(
            refused_ticket(
                name,
                code=GenerationProblemCode.UNIT_REQUEST_INVALID,
                detail=f"ID {name} 在剧本中指向多个条目",
                action=GenerationAction.FIX_INPUT,
            )
        )
    clean.extend(item for item, _ in addressable if id(item) not in ambiguous)
    return clean, tickets


_SPEECH_ACTIONS: dict[str, GenerationAction] = {
    "replan_unit": GenerationAction.REPLAN_UNIT,
    "assign_speaker": GenerationAction.FIX_INPUT,
    "fix_input": GenerationAction.FIX_INPUT,
}

_PROBLEM_ACTIONS: dict[str, GenerationAction] = {
    "replan_unit": GenerationAction.REPLAN_UNIT,
    "confirm_request_duration": GenerationAction.CONFIRM_REQUEST_DURATION,
    "configure_provider": GenerationAction.CONFIGURE_PROVIDER,
    "fix_input": GenerationAction.FIX_INPUT,
    "assign_speaker": GenerationAction.FIX_INPUT,
    "confirm_duration": GenerationAction.CONFIRM_REQUEST_DURATION,
    "configure_video_model": GenerationAction.CONFIGURE_PROVIDER,
    "enable_model_audio": GenerationAction.CONFIGURE_PROVIDER,
    "repair_reference_assets": GenerationAction.GENERATE_DEPENDENCY,
    "review_reference_selection": GenerationAction.FIX_INPUT,
    "review_request_configuration": GenerationAction.FIX_INPUT,
}


def generation_action_for(raw: object) -> GenerationAction:
    """Map a request-planning action onto the generation-result action set.

    Anything the planning modules add later degrades to ``FIX_INPUT``: telling a
    caller to look at its own request is the only next step that is safe when the
    verdict's meaning is not yet known here.
    """

    return _PROBLEM_ACTIONS.get(str(raw), GenerationAction.FIX_INPUT)


def _facts_failure_problem(failure: VideoRequestFactsFailure, unit_id: str) -> GenerationProblem:
    """视频请求事实失败折成该目标的阻断问题：问题码、参数与修复指引与投影侧同一出口。"""

    return GenerationProblem(
        code=failure.code,
        detail=failure.summary(),
        action=generation_action_for(failure.action),
        params={"unit_id": unit_id, **failure.parameters()},
    )


def speech_admission_problems(admission: SpeechAdmission) -> tuple[GenerationProblem, ...]:
    """Lift every speech blocker into the shared generation problem contract."""

    payload = admission.to_dict()
    return tuple(
        GenerationProblem(
            code=problem.code.value,
            detail=problem.reason.value,
            action=_SPEECH_ACTIONS.get(problem.action.value, GenerationAction.FIX_INPUT),
            params={"unit_id": admission.unit_id, "speech_admission": payload},
        )
        for problem in admission.problems
    )


def active_task_problem(task: Mapping[str, Any]) -> GenerationProblem:
    return GenerationProblem(
        code=GenerationProblemCode.ACTIVE_TASK_CONFLICT,
        detail=f"该 unit 已有在途任务（状态：{task.get('status')}），等待其结束后再提交本次批量请求",
        action=GenerationAction.WAIT_FOR_TASK,
        params={"task_id": str(task.get("id") or task.get("task_id") or ""), "status": str(task.get("status") or "")},
    )


async def _active_conflicts(
    *,
    project_name: str,
    task_type: str,
    script_file: str | None,
    unit_ids: Sequence[str],
    user_id: str = DEFAULT_USER_ID,
    queue: GenerationQueue | None = None,
) -> dict[str, dict[str, Any]]:
    """Map unit id → the active task that already occupies it, if any."""

    if not unit_ids:
        return {}
    active = await get_active_tasks_for_resources(
        project_name=project_name,
        task_type=task_type,
        resource_ids=list(unit_ids),
        script_file=script_file,
        user_id=user_id,
        queue=queue,
    )
    return {str(task["resource_id"]): task for task in active if task.get("resource_id")}


async def admit_storyboard_video_batch(
    *,
    project_name: str,
    project: dict[str, Any],
    script_file: str,
    items: Sequence[tuple[str, dict[str, Any]]],
    operation: str,
    selection: GenerationSelectionMode,
    extra_tickets: Sequence[UnitAdmissionTicket] = (),
    user_id: str = DEFAULT_USER_ID,
    queue: GenerationQueue | None = None,
    config_resolver: ConfigResolver | None = None,
    video_request_facts: VideoRequestFacts | VideoRequestFactsFailure | None = None,
) -> BatchAdmission:
    """Evaluate every storyboard unit of one request against the current state.

    ``items`` are ``(resource_id, script item)`` pairs. This route takes its tier from
    the script alone at execution, so the shared gates here are the active-task
    conflict, reference admission and the route's video request facts; each remaining
    unit is admitted as it would be on its own.
    """

    resource_ids = [resource_id for resource_id, _item in items]
    conflicts = await _active_conflicts(
        project_name=project_name,
        task_type="video",
        script_file=script_file,
        unit_ids=resource_ids,
        user_id=user_id,
        queue=queue,
    )
    generation_type = video_bucket_for_generation_mode(project.get("generation_mode"))
    catalog = build_reference_catalog(project)
    if items and video_request_facts is None:
        video_request_facts = await evaluate_video_request_facts(
            project,
            route="storyboard",
            generation_type=generation_type,
            identity=CONFIGURED_VIDEO_IDENTITY,
            resolver=config_resolver or ConfigResolver(async_session_factory),
        )

    tickets: list[UnitAdmissionTicket] = list(extra_tickets)
    for resource_id, item in items:
        if resource_id in conflicts:
            tickets.append(
                UnitAdmissionTicket(unit_id=resource_id, problems=(active_task_problem(conflicts[resource_id]),))
            )
            continue
        reference_problems = reference_admission_problems(admit_storyboard_item(catalog, item), unit_id=resource_id)
        if isinstance(video_request_facts, VideoRequestFactsFailure):
            # 事实失败与引用缺口同属这一目标的已知问题，一次报全：用户改完模型配置不该再撞见引用缺口。
            tickets.append(
                UnitAdmissionTicket(
                    unit_id=resource_id,
                    problems=(_facts_failure_problem(video_request_facts, resource_id), *reference_problems),
                )
            )
            continue
        tickets.append(UnitAdmissionTicket(unit_id=resource_id, problems=reference_problems))

    return BatchAdmission(
        operation=operation,
        selection=selection,
        tickets=tuple(tickets),
    )


def speech_admission_ticket(unit_id: str, exc: SpeechAdmissionError) -> UnitAdmissionTicket:
    """Refuse a target on its speech admission, carrying that verdict through.

    The admission's own problem code and action are kept unchanged, so a caller
    never has to read the rendered text to decide what to do next.
    """

    problem = exc.admission.problems[0]
    return refused_ticket(
        unit_id or exc.admission.unit_id,
        code=problem.code.value,
        detail=f"发声准入未通过：{json.dumps(exc.admission.to_dict(), ensure_ascii=False)}",
        action=(GenerationAction.REPLAN_UNIT if problem.action.value == "replan_unit" else GenerationAction.FIX_INPUT),
        params={"speech_admission": exc.admission.to_dict()},
    )


def storyboard_video_prompt(
    item: dict[str, Any],
    *,
    content_mode: str,
    voice_characters: dict[str, Any] | None = None,
    slots: StoryboardVideoSlots | None = None,
    asset_links: str = "",
    audio_speakers: tuple[str, ...] = (),
) -> str:
    prompt = item.get("video_prompt")
    if not prompt:
        item_id = item.get("segment_id") or item.get("scene_id")
        raise ValueError(f"分镜缺少 video_prompt 字段: {item_id}")
    if isinstance(prompt, dict) and not is_structured_video_prompt(prompt):
        item_id = item.get("segment_id") or item.get("scene_id")
        raise ValueError(f"分镜 video_prompt 为对象但格式不符合结构化规范: {item_id}")
    if not isinstance(prompt, (dict, str)):
        item_id = item.get("segment_id") or item.get("scene_id")
        raise TypeError(f"分镜 video_prompt 类型无效（期望 str 或 dict）: {item_id}")
    # 与执行路径共用 render_storyboard_video_prompt：入队快照与实发文本同构，反向约束尾词
    # 由该出口统一追加（执行期对已带尾词的字符串幂等）。
    rendered = render_storyboard_video_prompt(
        prompt,
        item,
        content_mode=content_mode,
        voice_characters=voice_characters,
        slots=slots,
        asset_links=asset_links,
        audio_speakers=audio_speakers,
    )
    # K 引用有界（D-17）：越界文本写回时已被拒，这里在计费前再拦一次——声明了不存在的参考图
    # 只会在供应商那边失败，成本更高。
    assets = get_generated_assets(item)
    grid = assets.get("shot_grid")
    cells = grid.get("cell_paths") if isinstance(grid, Mapping) else None
    count = slots.cell_count if slots is not None else len(cells) if isinstance(cells, list) else 0
    problems = keyframe_reference_problems(rendered, count)
    if problems:
        item_id = item.get("segment_id") or item.get("scene_id") or item.get("shot_id")
        raise ValueError(
            f"分镜 video_prompt 引用了不存在的关键帧: {'、'.join(problems)}（本分镜宫格共 {count} 格，id={item_id}）"
        )
    return rendered


async def resolve_voice_context(project: dict[str, Any], content_mode: str) -> dict[str, Any] | None:
    """供 Voice_Profiles 注入的角色资产（``None`` 表示不注入）。

    非 drama 不注入；drama 按无声判据排除（C 类模型不产音、或本集关闭了音频，两条路径同口径）。
    台词不受影响、照常下发。
    """
    if content_mode != "drama":
        return None
    if await resolve_project_is_silent(project):
        return None
    return project.get("characters") or {}


async def audio_switch_conflict(
    project: dict[str, Any], *, request_facts: VideoRequestFacts | VideoRequestFactsFailure | None = None
) -> GenerationProblem | None:
    """分镜图生视频的音频闸门（``assert_audio_switch_supported``，与 WebUI 提交入口同一判据）。

    成片恒有声的模型收不到关闭音频的请求，放行只会让无声判据把音色约束整批裁掉。闸门与创作类型
    无关，narration/ad 同样受检。

    调用点固定在「确有目标要请求」之后：整集已完成、或条目全被 :func:`build_storyboard_video_specs`
    过滤时本就不会产生任何请求，此时拒绝等于把一次正常的空转变成报错。
    参考生视频由公共 request projection 给出同一音频能力判定。

    返回冲突问题，参数带冲突的供应商与模型供界面按显示名渲染；无冲突返回 ``None``。调用方
    把它折成逐目标的准入结论，与其它缺口一起在建任务之前一次报全。
    """
    try:
        await assert_audio_switch_supported(
            project,
            video_bucket_for_generation_mode(project.get("generation_mode")),
            request_facts=request_facts,
        )
    except AudioSwitchConflict as exc:
        return GenerationProblem(
            code="video_audio_switch_not_supported",
            detail=str(exc),
            action=GenerationAction.CONFIGURE_PROVIDER,
            params={"provider": exc.provider_id, "model": exc.model_id},
        )
    return None


def build_storyboard_video_specs(
    *,
    items: list[dict[str, Any]],
    id_field: str,
    content_mode: str,
    skeleton_kind: str,
    script_filename: str,
    project_dir: Path,
    skip_ids: list[str] | None,
    project: dict[str, Any] | None = None,
    episode: int = 1,
    resolver: ArtifactCurrencyResolver | None = None,
    voice_characters: dict[str, Any] | None = None,
    storyboard_items: Sequence[Any] | None = None,
) -> tuple[list[TaskSpec], list[UnitAdmissionTicket]]:
    """Build the Storyboard-mode specs, refusing each unit that cannot be requested.

    A unit whose speech, inputs or prompt are unusable is refused with its own code
    and next action instead of being dropped into a log line, so one bad unit never
    silently shrinks the batch — and, because the refusal is a ticket rather than a
    recorded block, it can hold the whole batch back before any task exists.

    ``skeleton_kind`` 取生成模式闸门给出的剧本实际骨架种类，而不是按创作类型反推：族内的历史
    形态（narration 数据落 ``scenes`` 键）按反推值去适配，合法的旧剧本会被整批判成解析失败。

    发声准入在这里执行，与参考生视频由 ``reference_unit_task_spec`` 承担的位置对应：
    构造 TaskSpec 的这一步是「这个 unit 能不能被请求」的唯一口径，四个 storyboard 入口
    （整集 / 全部 / 点名 / 单条）与只读的工作流计划都经由它，混合发声与 needs_replan 因此
    在任何入口都会在建任务之前扣住整批，计划预告的准入结论也与真正提交时一致。
    """

    item_type = "分镜"
    skip_set = set(skip_ids or [])

    specs: list[TaskSpec] = []
    refused: list[UnitAdmissionTicket] = []
    project = project or {}
    if resolver is None:
        resolver = active_artifact_currency_resolver(project_dir, project)
    for item in items:
        item_id = str(storyboard_item_id(item, id_field))
        if item_id in skip_set:
            continue

        try:
            require_script_unit_admitted(skeleton_kind, item, policy=speech_policy_for_project(project))
        except SpeechAdmissionError as exc:
            # 逐 ID 契约与既有 speech_admission 载荷并存：前者说「这个 ID 没做成、
            # 下一步做什么」，后者带着准入自己的完整定位信息。
            refused.append(speech_admission_ticket(str(item_id), exc))
            continue

        try:
            slots = resolve_usable_storyboard_video_slots(
                project_path=project_dir,
                project=project,
                episode=episode,
                resource_id=str(item_id),
                items=storyboard_items if storyboard_items is not None else items,
                id_field=id_field,
                resolver=resolver,
            )
        except (OSError, ValueError) as exc:
            # 分镜图本身不可用是最常见的一种，给出下一步该跑什么，而不是只报路径。
            advice = "，请先运行 generate_storyboards" if isinstance(exc, StoryboardImageUnavailable) else ""
            refused.append(
                refused_ticket(
                    str(item_id),
                    code=GenerationProblemCode.UNIT_INPUT_UNUSABLE,
                    detail=f"{item_type} {item_id} 的视频输入不可用: {exc}{advice}",
                    action=GenerationAction.GENERATE_DEPENDENCY,
                )
            )
            continue

        try:
            prompt = storyboard_video_prompt(
                item,
                content_mode=content_mode,
                voice_characters=voice_characters,
                slots=slots,
                asset_links=storyboard_asset_links(project, item, project_dir.name),
            )
        except Exception as exc:
            refused.append(
                refused_ticket(
                    str(item_id),
                    code=GenerationProblemCode.UNIT_REQUEST_INVALID,
                    detail=f"{item_type} {item_id} 的 video_prompt 无效: {exc}",
                    action=GenerationAction.FIX_INPUT,
                )
            )
            continue

        # duration 是能力维度，留待执行层在 provider 解析后校验（见 ADR-0001）；
        # 原样透传调用方显式指定的值，不在入队侧做 int() 截断式归一化（否则会把
        # 本应被执行层拒绝的非法值静默修正）。缺省由执行层按 caps 收口默认。
        extra_payload: dict[str, Any] = {}
        extra_payload["storyboard_reference_count"] = len(slots.references)
        duration = item.get("duration_seconds")
        if duration is not None:
            extra_payload["duration_seconds"] = duration

        # 构造 TaskSpec 本身也是可入队性判定（空提示词等，``TaskSpecValidationError``
        # 是 ValueError 子类）：不接住就会让一个坏条目把整批打成一句通用报错，
        # 其余条目的结论无从得知。
        try:
            spec = TaskSpec.from_request(
                task_type="video",
                media_type="video",
                resource_id=item_id,
                prompt=prompt,
                script_file=script_filename,
                extra_payload=extra_payload or None,
            )
        except ValueError as exc:
            refused.append(
                refused_ticket(
                    str(item_id),
                    code=GenerationProblemCode.UNIT_REQUEST_INVALID,
                    detail=f"{item_type} {item_id} 无法构造入队请求: {exc}",
                    action=GenerationAction.FIX_INPUT,
                )
            )
            continue
        specs.append(spec)
    return specs, refused


async def storyboard_video_request_facts(
    project: dict[str, Any], config_resolver: ConfigResolver | None = None
) -> VideoRequestFacts | VideoRequestFactsFailure:
    """分镜图生视频按项目当前配置的视频请求事实：准入与报价读同一份。"""

    return await evaluate_video_request_facts(
        project,
        route="storyboard",
        generation_type=video_bucket_for_generation_mode(project.get("generation_mode")),
        identity=CONFIGURED_VIDEO_IDENTITY,
        resolver=config_resolver or ConfigResolver(async_session_factory),
    )


async def admit_storyboard_video_request(
    *,
    project_name: str,
    project: dict[str, Any],
    script_file: str,
    items: Sequence[dict[str, Any]],
    id_field: str,
    specs: Sequence[TaskSpec],
    operation: str,
    selection: GenerationSelectionMode,
    extra_tickets: Sequence[UnitAdmissionTicket],
    user_id: str = DEFAULT_USER_ID,
    queue: GenerationQueue | None = None,
    config_resolver: ConfigResolver | None = None,
    video_request_facts: VideoRequestFacts | VideoRequestFactsFailure | None = None,
    project_dir: Path | None = None,
    storyboard_items: Sequence[Any] | None = None,
    episode: int = 1,
    content_mode: str | None = None,
) -> BatchAdmission:
    """Admit one Storyboard-mode request from the specs it would actually enqueue.

    目标集取自即将入队的 spec，被拒的单元不在其中。``video_request_facts`` 由已求值过的调用方
    传入（批量预览还要用同一份事实报价），缺省时在此求值。

    音频开关冲突属于请求自身已知的配置缺口，在这里与其余缺口折进同一批逐目标结论：
    短路返回只会报出这一条，用户改完配置重试才撞见下一个已知缺口。
    """

    items_by_id = {str(storyboard_item_id(item, id_field) or ""): item for item in items}
    targets: list[tuple[str, dict[str, Any]]] = []
    for spec in specs:
        item = items_by_id.get(spec.resource_id)
        if item is None:
            raise ValueError(f"找不到待生成条目: {spec.resource_id}")
        targets.append((spec.resource_id, item))
    request_facts = (
        (video_request_facts or await storyboard_video_request_facts(project, config_resolver)) if specs else None
    )
    conflict = await audio_switch_conflict(project, request_facts=request_facts) if specs else None
    admission = await admit_storyboard_video_batch(
        project_name=project_name,
        project=project,
        script_file=script_file,
        items=targets,
        operation=operation,
        selection=selection,
        extra_tickets=extra_tickets,
        user_id=user_id,
        queue=queue,
        config_resolver=config_resolver,
        video_request_facts=request_facts,
    )
    # 音频开关冲突与投影侧的缺口写进同一批票：短路返回只会报出这一条，用户改完配置
    # 重试才撞见下一个已知缺口，正是这道门要免掉的逐条试探。
    target_ids = {spec.resource_id for spec in specs}
    shape_problems: dict[str, tuple[GenerationProblem, ...]] = {}
    if isinstance(request_facts, VideoRequestFacts):
        for spec in specs:
            problems: list[GenerationProblem] = []
            count = (spec.payload or {}).get("storyboard_reference_count", 0)
            if project_dir is not None:
                try:
                    slots = resolve_usable_storyboard_video_slots(
                        project_path=project_dir,
                        project=project,
                        episode=episode,
                        items=storyboard_items if storyboard_items is not None else items,
                        id_field=id_field,
                        resource_id=spec.resource_id,
                    )
                    count = len(slots.references)
                    if spec.payload is not None:
                        spec.payload["storyboard_reference_count"] = count
                    audio = resolve_storyboard_audio_inputs(
                        project=project,
                        project_path=project_dir,
                        item=items_by_id[spec.resource_id],
                        slots=slots,
                        voice=VoiceRenderSettings.from_request_facts(request_facts),
                    )
                    if spec.payload is not None:
                        spec.payload["prompt"] = storyboard_video_prompt(
                            items_by_id[spec.resource_id],
                            content_mode=content_mode or str(project.get("content_mode") or "narration"),
                            voice_characters=None
                            if not request_facts.requested_generate_audio or request_facts.voice_consistency == "none"
                            else project.get("characters") or {},
                            slots=slots,
                            asset_links=storyboard_asset_links(
                                project, items_by_id[spec.resource_id], project_dir.name
                            ),
                            audio_speakers=audio.speakers,
                        )
                    if audio.paths and (
                        request_facts.voice_consistency != "native"
                        or len(audio.paths) > request_facts.max_reference_audio_count
                    ):
                        problems.append(
                            GenerationProblem(
                                code="video_reference_audio_exceeded"
                                if request_facts.voice_consistency == "native"
                                else "video_reference_audio_unsupported",
                                detail="参考音频不符合模型能力，请更换模型或调整角色参考音频",
                                action=GenerationAction.CONFIGURE_PROVIDER,
                                params={
                                    "provider": request_facts.provider_id,
                                    "model": request_facts.model_id,
                                    "count": len(audio.paths),
                                    "limit": request_facts.max_reference_audio_count,
                                },
                            )
                        )
                except (OSError, ValueError) as exc:
                    problems.append(
                        GenerationProblem(
                            code=GenerationProblemCode.UNIT_INPUT_UNUSABLE,
                            detail=str(exc),
                            action=GenerationAction.FIX_INPUT,
                        )
                    )
            limit = request_facts.max_reference_images
            if limit is not None and count > limit:
                problems.append(
                    GenerationProblem(
                        code="video_reference_images_exceeded" if limit else "video_reference_images_unsupported",
                        detail="关键帧数量超过模型上限，请调整宫格规格或更换模型",
                        action=GenerationAction.CONFIGURE_PROVIDER,
                        params={
                            "provider": request_facts.provider_id,
                            "model": request_facts.model_id,
                            "count": count,
                            "limit": limit,
                        },
                    )
                )
            shape_problems[spec.resource_id] = tuple(problems)
    return replace(
        admission,
        tickets=tuple(
            replace(
                ticket,
                problems=(
                    *ticket.problems,
                    *((conflict,) if conflict is not None else ()),
                    *shape_problems.get(ticket.unit_id, ()),
                ),
            )
            if ticket.unit_id in target_ids
            else ticket
            for ticket in admission.tickets
        ),
    )


__all__ = [
    "active_task_problem",
    "admit_storyboard_video_batch",
    "admit_storyboard_video_request",
    "artifact_state_tickets",
    "audio_switch_conflict",
    "build_storyboard_video_specs",
    "resolve_voice_context",
    "screen_storyboard_items",
    "speech_admission_problems",
    "speech_admission_ticket",
    "storyboard_item_aliases",
    "storyboard_video_prompt",
    "storyboard_video_request_facts",
    "video_target_states",
]
