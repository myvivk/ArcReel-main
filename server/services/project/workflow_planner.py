"""Async current-state adapter for the side-effect-free workflow planner."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lib.config.resolver import ConfigResolver
from lib.db.base import DEFAULT_USER_ID
from lib.generation.generation_queue import GenerationQueue, get_generation_queue
from lib.generation.generation_queue_client import get_active_tasks_for_resources
from lib.generation.generation_result import (
    GenerationProblem,
    GenerationSelectionMode,
    migration_problem,
    provider_checkpoint_from_task,
)
from lib.project.asset_types import ASSET_SPECS
from lib.project.project_manager import ProjectManager, get_project_manager
from lib.project.project_migration_failure import (
    MIGRATION_FAILURE_CODE,
    MigrationFailureRecord,
    load_migration_failure,
)
from lib.script.script_batch_edit import script_revision
from lib.script.script_skeleton import ensure_route_skeleton, resolve_kind_items
from lib.speech.speech_composition import admit_script_unit, speech_policy_for_project
from lib.workflow.workflow_plan import (
    WorkflowPlan,
    WorkflowPlanRequest,
    WorkflowProviderCheckpoint,
    WorkflowTaskObservation,
    build_workflow_plan,
)
from lib.workflow.workflow_state import WorkflowBlocker, WorkflowStateService, WorkflowStatus
from server.services.admission.video_batch_admission import (
    admit_storyboard_video_request,
    build_storyboard_video_specs,
    resolve_voice_context,
    speech_admission_problems,
)


def _task_payload_fields(task: Mapping[str, Any]) -> list[str] | None:
    """文本任务 payload 里的 ``fields``（本次编写哪几个提示词字段）；其余任务为 ``None``。

    视频提示词与分镜提示词共用一个任务类型，进度归行只能靠这一位区分（D-19）。
    """

    payload = task.get("payload")
    raw = payload.get("fields") if isinstance(payload, Mapping) else None
    if not isinstance(raw, list):
        return None
    return [str(field) for field in raw]


@dataclass(frozen=True, slots=True)
class _ScriptFacts:
    project: dict[str, Any]
    project_path: Path
    script: dict[str, Any]
    script_file: str
    kind: str
    id_field: str
    items: tuple[dict[str, Any], ...]
    revision: str


class WorkflowPlanner:
    """Compose durable workflow status with transient request and queue facts."""

    def __init__(self, project_manager: ProjectManager):
        self._pm = project_manager

    async def get_plan(
        self,
        project_name: str,
        request: WorkflowPlanRequest,
        *,
        user_id: str = DEFAULT_USER_ID,
        queue: GenerationQueue | None = None,
        config_resolver: ConfigResolver | None = None,
    ) -> WorkflowPlan:
        queue = queue or get_generation_queue()
        status = await asyncio.to_thread(
            WorkflowStateService(self._pm).get_status,
            project_name,
            request.episode_id,
        )
        blocked = next((b for b in status.blockers if b.code == MIGRATION_FAILURE_CODE), None)
        if blocked is not None:
            # The migration verdict is the whole plan: nothing downstream can be
            # planned off inputs the migration itself refused.
            return build_workflow_plan(
                status,
                project_problems=[await self._migration_problem(project_name, blocked)],
                script_revision=None,
                task_observations=[],
                admission=None,
            )
        facts = await self._script_facts(project_name, status)
        structure_problems = self._structure_problems(facts)
        tasks = await self._active_tasks(project_name, status, facts, user_id=user_id, queue=queue)
        admission = None
        if facts is not None and not structure_problems and status.next_action.type == "generate_videos":
            admission = await self._video_admission(
                project_name,
                status,
                facts,
                request,
                in_flight=_in_flight_video_units(tasks),
                user_id=user_id,
                queue=queue,
                config_resolver=config_resolver,
            )
        return build_workflow_plan(
            status,
            structure_problems=structure_problems,
            script_revision=facts.revision if facts is not None else None,
            task_observations=tasks,
            admission=admission,
        )

    async def _migration_problem(self, project_name: str, blocker: WorkflowBlocker) -> GenerationProblem:
        """Re-read the verdict for the structured locations the blocker cannot carry.

        The blocker already holds the code and the reason; only the record names
        the offending episode and file. A verdict cleared between the two reads
        means a concurrent retry succeeded — the plan still reports the blocker
        it was built from, just without the per-location detail.
        """

        project_path = self._pm.get_project_path(project_name)
        failure = await asyncio.to_thread(load_migration_failure, project_path)
        if failure is None:
            failure = MigrationFailureRecord(schema_version=-1, failed_at="", reason=blocker.reason)
        return migration_problem(failure)

    async def _script_facts(self, project_name: str, status: WorkflowStatus) -> _ScriptFacts | None:
        target = status.target
        script_state = status.artifacts.get("script", {}).get("state")
        if target is None or script_state not in {"current", "stale"}:
            return None

        def _read() -> _ScriptFacts:
            project = self._pm.load_project(project_name)
            script = self._pm.load_script_readonly(project_name, target.script)
            kind = ensure_route_skeleton(
                script,
                status.project.content_mode,
                status.project.generation_mode,
            )
            raw_items, id_field, _kind = resolve_kind_items(script, kind=kind)
            if not isinstance(raw_items, list) or not all(isinstance(item, dict) for item in raw_items):
                raise ValueError(f"{kind} must be an array of objects")
            return _ScriptFacts(
                project=project,
                project_path=self._pm.get_project_path(project_name),
                script=script,
                script_file=target.script_filename,
                kind=kind,
                id_field=id_field,
                items=tuple(raw_items),
                revision=script_revision(script),
            )

        return await asyncio.to_thread(_read)

    @staticmethod
    def _structure_problems(facts: _ScriptFacts | None) -> list[GenerationProblem]:
        if facts is None:
            return []
        problems = []
        policy = speech_policy_for_project(facts.project)
        for item in facts.items:
            admission = admit_script_unit(facts.kind, item, policy=policy)
            problems.extend(speech_admission_problems(admission))
        return problems

    @staticmethod
    async def _active_tasks(
        project_name: str,
        status: WorkflowStatus,
        facts: _ScriptFacts | None,
        *,
        user_id: str,
        queue: GenerationQueue,
    ) -> list[WorkflowTaskObservation]:
        rows: list[dict[str, Any]] = []
        text_queries: list[tuple[str, list[str]]] = []
        if status.target is not None:
            text_queries.append(("text_episode_script", [f"episode-{status.target.episode}"]))
        for task_type, resource_ids in text_queries:
            rows.extend(
                await get_active_tasks_for_resources(
                    project_name=project_name,
                    task_type=task_type,
                    resource_ids=resource_ids,
                    user_id=user_id,
                    queue=queue,
                )
            )

        if facts is not None:
            for asset_type in ASSET_SPECS:
                bucket = facts.project.get(ASSET_SPECS[asset_type].bucket_key)
                resource_ids = list(bucket) if isinstance(bucket, dict) else []
                if resource_ids:
                    rows.extend(
                        await get_active_tasks_for_resources(
                            project_name=project_name,
                            task_type=asset_type,
                            resource_ids=resource_ids,
                            user_id=user_id,
                            queue=queue,
                        )
                    )
            id_field = facts.id_field
            unit_ids = [
                str(item[id_field])
                for item in facts.items
                if isinstance(item.get(id_field), str) and str(item[id_field])
            ]
            task_types = ["video", "storyboard"]
            for task_type in task_types:
                rows.extend(
                    await get_active_tasks_for_resources(
                        project_name=project_name,
                        task_type=task_type,
                        resource_ids=unit_ids,
                        script_file=facts.script_file,
                        user_id=user_id,
                        queue=queue,
                    )
                )
        seen: set[str] = set()
        observations: list[WorkflowTaskObservation] = []
        for task in rows:
            task_id = str(task.get("task_id") or "")
            if not task_id or task_id in seen:
                continue
            seen.add(task_id)
            checkpoint = provider_checkpoint_from_task(task)
            observations.append(
                WorkflowTaskObservation(
                    unit_id=str(task.get("resource_id") or ""),
                    task_id=task_id,
                    batch_id=str(task["batch_id"]) if task.get("batch_id") is not None else None,
                    task_type=str(task.get("task_type") or ""),
                    status=str(task.get("status") or ""),
                    fields=_task_payload_fields(task),
                    provider_checkpoint=(
                        WorkflowProviderCheckpoint(submitted=checkpoint.submitted) if checkpoint is not None else None
                    ),
                )
            )
        return observations

    @staticmethod
    async def _video_admission(
        project_name: str,
        status: WorkflowStatus,
        facts: _ScriptFacts,
        request: WorkflowPlanRequest,
        *,
        in_flight: frozenset[str],
        user_id: str,
        queue: GenerationQueue,
        config_resolver: ConfigResolver | None,
    ) -> dict[str, Any] | None:
        """预告这一批真提交时的准入结论；正在生成的单元与 Web 批量入口一样不算目标，不作为在途冲突报出。"""

        # 计划要预告的是「这一批真提交时会得到什么结论」，所以走的是提交侧同一条缝：
        # spec 由 build_storyboard_video_specs 构造（发声准入、输入可用性、prompt 组装
        # 与逐 ID 拒绝票都在其中），准入由 admit_storyboard_video_request 给出（音频闸门
        # 与投影缺口折在同一批票里）。自行挑目标并把视觉提示词留空，会让计划按另一套
        # 视觉基准判断已付费产物能否复用，与真正提交时的结论分叉。
        requested = set(status.next_action.requested_ids) - in_flight
        if not requested:
            return None
        id_field = facts.id_field
        items = [
            item for item in facts.items if isinstance(item.get(id_field), str) and str(item[id_field]) in requested
        ]
        voice_characters = await resolve_voice_context(facts.project, status.project.content_mode)
        specs, refused = await asyncio.to_thread(
            build_storyboard_video_specs,
            items=items,
            id_field=id_field,
            content_mode=status.project.content_mode,
            skeleton_kind=facts.kind,
            script_filename=facts.script_file,
            project_dir=facts.project_path,
            skip_ids=None,
            project=facts.project,
            episode=status.target.episode if status.target is not None else 1,
            voice_characters=voice_characters,
            storyboard_items=facts.items,
        )
        admission = await admit_storyboard_video_request(
            project_name=project_name,
            project=facts.project,
            script_file=facts.script_file,
            items=items,
            id_field=id_field,
            specs=specs,
            operation=status.next_action.type,
            selection=GenerationSelectionMode.MISSING_ONLY,
            extra_tickets=refused,
            user_id=user_id,
            queue=queue,
            config_resolver=config_resolver,
            project_dir=facts.project_path,
            storyboard_items=facts.items,
            episode=status.target.episode if status.target is not None else 1,
            content_mode=status.project.content_mode,
        )
        return admission.to_payload()


def _in_flight_video_units(tasks: list[WorkflowTaskObservation]) -> frozenset[str]:
    return frozenset(
        task.unit_id for task in tasks if task.task_type == "video" and task.status in {"queued", "running"}
    )


def get_workflow_planner(project_manager: ProjectManager | None = None) -> WorkflowPlanner:
    return WorkflowPlanner(project_manager or get_project_manager())


__all__ = ["WorkflowPlanner", "get_workflow_planner"]
