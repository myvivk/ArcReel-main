"""Project story planning, source-backed observations and bounded authoring context."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from lib.infra.api_errors import ConflictError, UnprocessableError
from lib.infra.content_digest import prefixed_canonical_json_digest
from lib.infra.schema_guards import is_shape

if TYPE_CHECKING:
    from lib.project.project_manager import ProjectManager


class StoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    kind: Literal["relationship", "event", "fact"]
    text: str = Field(min_length=1, max_length=2000)
    characters: list[str] = Field(default_factory=list, max_length=20)
    episode: int | None = Field(default=None, gt=0)
    status: Literal["pending", "confirmed"] = "pending"
    source_file: str | None = Field(default=None, min_length=1)
    source_id: str | None = Field(default=None, min_length=1)
    source_revision: str | None = Field(default=None, min_length=1)


class StoryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    premise: str = Field(default="", max_length=4000)
    records: list[StoryRecord] = Field(default_factory=list, max_length=1000)
    context_budget: int = Field(default=2400, ge=400, le=6000)


class StorySnapshot(BaseModel):
    plan: StoryPlan
    revision: str
    stale_ids: list[str]


def plan_from_project(project: dict[str, Any]) -> StoryPlan:
    raw = project.get("story_plan")
    return StoryPlan() if raw is None else StoryPlan.model_validate(raw)


def story_revision(plan: StoryPlan) -> str:
    return prefixed_canonical_json_digest(plan.model_dump())


def _source_rows(script: dict[str, Any]) -> list[Any]:
    rows = script.get("scenes", [])
    return rows if is_shape(rows, list) else []


def _content_revision(row: dict[str, Any]) -> str:
    return prefixed_canonical_json_digest(
        {
            key: row.get(key)
            for key in ("scene_id", "source_text", "scene_description", "utterances", "characters_in_scene")
        }
    )


def stale_record_ids(
    project: dict[str, Any],
    plan: StoryPlan,
    load_script: Callable[[str], dict[str, Any]],
) -> list[str]:
    """Compare only linked narrative content; visual prompt edits do not stale observations."""
    bound = {str(ep.get("script_file", "")).removeprefix("scripts/") for ep in project.get("episodes", [])}
    cache: dict[str, dict[str, Any]] = {}
    stale: list[str] = []
    for record in plan.records:
        if not record.source_file:
            continue
        if record.source_file not in bound:
            stale.append(record.id)
            continue
        if record.source_file not in cache:
            try:
                script = load_script(record.source_file)
            except (FileNotFoundError, ValueError):
                script = {}
            cache[record.source_file] = {
                str(row.get("scene_id")): row for row in _source_rows(script) if is_shape(row, dict)
            }
        row = cache[record.source_file].get(record.source_id or "")
        if row is None or _content_revision(row) != record.source_revision:
            stale.append(record.id)
    return stale


def snapshot(pm: ProjectManager, name: str) -> StorySnapshot:
    project = pm.load_project(name)
    plan = plan_from_project(project)
    return StorySnapshot(
        plan=plan,
        revision=story_revision(plan),
        stale_ids=stale_record_ids(project, plan, lambda filename: pm.load_script_readonly(name, filename)),
    )


def save_plan(pm: ProjectManager, name: str, plan: StoryPlan, revision: str) -> StorySnapshot:
    """Compare-and-save within the existing project transaction; source provenance is immutable."""

    def mutate(project: dict[str, Any]) -> None:
        previous = plan_from_project(project)
        if story_revision(previous) != revision:
            raise ConflictError("story_plan_conflict")
        ids = [record.id for record in plan.records]
        if len(ids) != len(set(ids)):
            raise UnprocessableError("story_plan_invalid")
        characters = set(project.get("characters", {}))
        episodes = {ep["episode"] for ep in project.get("episodes", [])}
        old = {record.id: record for record in previous.records}
        stale = set(stale_record_ids(project, previous, lambda filename: pm.load_script_readonly(name, filename)))
        for record in plan.records:
            if not set(record.characters) <= characters or (
                record.episode is not None and record.episode not in episodes
            ):
                raise UnprocessableError("story_plan_invalid")
            if record.kind in ("event", "fact") and record.episode is None:
                raise UnprocessableError("story_plan_invalid")
            if record.kind == "relationship" and len(set(record.characters)) < 2:
                raise UnprocessableError("story_plan_invalid")
            prior = old.get(record.id)
            provenance = (record.source_file, record.source_id, record.source_revision)
            expected = (prior.source_file, prior.source_id, prior.source_revision) if prior else (None, None, None)
            if provenance != expected:
                raise UnprocessableError("story_plan_invalid")
            if (
                prior
                and prior.source_file
                and (
                    record.text != prior.text
                    or record.characters != prior.characters
                    or record.episode != prior.episode
                    or record.kind != prior.kind
                )
            ):
                raise UnprocessableError("story_plan_invalid")
            if record.id in stale and record.status == "confirmed" and prior and prior.status != "confirmed":
                raise ConflictError("story_plan_source_changed")
        project["story_plan"] = plan.model_dump()

    pm.update_project(name, mutate)
    return snapshot(pm, name)


def extract_script_events(pm: ProjectManager, name: str, revision: str) -> StorySnapshot:
    """Copy explicit scene content as pending observations; never infer relationships or facts."""
    project = pm.load_project(name)
    additions: list[StoryRecord] = []
    for ep in project.get("episodes", []):
        filename = str(ep.get("script_file") or "").removeprefix("scripts/")
        if not filename:
            continue
        script = pm.load_script_readonly(name, filename)
        for row in _source_rows(script):
            if not is_shape(row, dict):
                continue
            sid = row.get("scene_id")
            text = row.get("scene_description") or row.get("source_text")
            if not isinstance(sid, str) or not isinstance(text, str) or not text.strip():
                continue
            chars = row.get("characters_in_scene")
            additions.append(
                StoryRecord(
                    id="source-" + prefixed_canonical_json_digest([ep["episode"], filename, sid]).split(":")[1][:24],
                    kind="event",
                    text=text[:2000],
                    episode=ep["episode"],
                    characters=[c for c in chars if c in project.get("characters", {})]
                    if is_shape(chars, list)
                    else [],
                    source_file=filename,
                    source_id=sid,
                    source_revision=_content_revision(row),
                )
            )

    def mutate(current: dict[str, Any]) -> None:
        plan = plan_from_project(current)
        if story_revision(plan) != revision:
            raise ConflictError("story_plan_conflict")
        existing = {record.id: record for record in plan.records}
        for record in additions:
            entry = next((ep for ep in current.get("episodes", []) if ep["episode"] == record.episode), None)
            if entry is None or str(entry.get("script_file") or "").removeprefix("scripts/") != record.source_file:
                raise ConflictError("story_plan_source_changed")
            script = pm.load_script_readonly(name, record.source_file or "")
            row = next(
                (
                    row
                    for row in _source_rows(script)
                    if is_shape(row, dict) and row.get("scene_id") == record.source_id
                ),
                None,
            )
            if row is None or _content_revision(row) != record.source_revision:
                raise ConflictError("story_plan_source_changed")
            prior = existing.get(record.id)
            if prior is None or prior.source_revision != record.source_revision:
                existing[record.id] = record
        plan.records = list(existing.values())
        project_plan = StoryPlan.model_validate(plan.model_dump())
        current["story_plan"] = project_plan.model_dump()

    pm.update_project(name, mutate)
    return snapshot(pm, name)


def authoring_context(
    pm: ProjectManager,
    name: str,
    episode: int,
    *,
    characters: set[str] | None = None,
    visual: bool = False,
) -> str:
    """Select by broadcast order, confirmation and provenance, then fit complete records in a character budget."""
    project = pm.load_project(name)
    plan = plan_from_project(project)
    stale = stale_record_ids(project, plan, lambda filename: pm.load_script_readonly(name, filename))
    return project_authoring_context(
        project,
        episode,
        stale_ids=stale,
        characters=characters,
        visual=visual,
    )


def project_authoring_context(
    project: dict[str, Any],
    episode: int,
    *,
    stale_ids: list[str] | tuple[str, ...] = (),
    characters: set[str] | None = None,
    visual: bool = False,
) -> str:
    """Project a previously validated plan without performing I/O."""
    plan = plan_from_project(project)
    order = {ep["episode"]: position for position, ep in enumerate(project.get("episodes", []))}
    if episode not in order:
        return ""
    stale = set(stale_ids)
    selected: list[StoryRecord] = []
    for record in plan.records:
        if record.status != "confirmed" or record.id in stale:
            continue
        if characters is not None and record.characters and not characters.intersection(record.characters):
            continue
        if record.episode is not None:
            position = order.get(record.episode)
            if position is None or position > order[episode]:
                continue
            # Events describe planned beats; visual authoring only consumes this episode's beats.
            if record.kind == "event" and record.episode != episode:
                continue
            # A fact is the end-of-episode state; it is only safe for later episodes.
            if record.kind == "fact" and position >= order[episode]:
                continue
        selected.append(record)
    selected.sort(key=lambda r: (r.episode != episode, r.kind != "relationship"))
    if not selected and (visual or not plan.premise.strip()):
        return ""
    header = "故事规划参考（仅已确认记录；分集事件是计划，连续性事实来自前集。以当前原文和定稿内容为准"
    header += "，只影响表演与画面，不改变分镜、台词或情节）：\n" if visual else "）：\n"
    output = header
    if not visual and plan.premise.strip():
        direction = (
            "- [故事走向，仅为创作计划，不是已发生事实] " + plan.premise.replace("<", "＜").replace(">", "＞") + "\n"
        )
        if len(output) + len(direction) <= plan.context_budget:
            output += direction
    for record in selected:
        label = {"relationship": "人物关系", "event": "分集事件", "fact": "前集事实"}[record.kind]
        line = f"- [{label}] {'、'.join(record.characters)}：{record.text}\n"
        line = line.replace("<", "＜").replace(">", "＞")
        if len(output) + len(line) <= plan.context_budget:
            output += line
    return output if output != header else ""
