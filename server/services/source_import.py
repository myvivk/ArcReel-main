"""导入原文的服务端链路：校验 → 组装上下文 → 改写 + 返修 → 结构/机检 → 原子落盘。

确定性链路：每次请求自己完成一次服务端改写（``TextGenerator`` SCRIPT 档），不依赖 Agent 会话；
结构校验阻塞落盘，机检 FAIL 只返修一次、仍失败则落盘并在响应里回带告警（方案 R-2）。
生成在锁外进行；源稿、原文、meta 与账本登记在 ``locked_source_registration`` 的同一把项目锁内提交。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lib.backends.providers import CallPurpose
from lib.backends.text_backends.base import (
    TextGenerationRequest,
    TextOutputTruncatedError,
    TextTaskType,
)
from lib.backends.text_generator import TextGenerator
from lib.episode.episode_paths import episode_source_relpath
from lib.episode.episode_target_duration import EPISODE_TARGET_DURATION_FIELD
from lib.episode.episode_total import TOTAL_EPISODES_FIELD
from lib.infra.api_errors import (
    BadGatewayError,
    ConflictError,
    NotFoundError,
    UnprocessableError,
)
from lib.infra.async_thread import run_sync_transaction
from lib.project.asset_types import asset_name_comparison_key
from lib.project.project_change_hints import emit_project_change_hint
from lib.project.project_manager import ProjectManager
from lib.project.project_schema import resolve_creation_mode
from lib.prompts.source_import_prompts import (
    SourceImportPromptContext,
    build_source_import_prompt,
    build_source_import_repair_prompt,
)
from lib.script.short_drama_import import assets_missing_description
from lib.script.short_drama_source import ShortDramaSourceConflict
from lib.script.source_import import (
    DeclaredAsset,
    OriginalMeta,
    PreviousEpisodeContext,
    SourceImportTier,
    collect_previous_episode_context,
    derive_tier,
    original_meta_relative_path,
    original_text_relative_path,
    read_original,
    read_original_at,
    read_short_drama_source_revision,
    source_path,
    source_revision,
    validate_source_text,
    validate_structure,
    write_original_meta,
    write_original_text,
    write_short_drama_source_locked,
)
from lib.short_drama_ingest import register_factory_episode_sources_locked
from lib.short_drama_quality import QualityIssue, check_episode
from lib.speech.speech_rate import SPEECH_RATE_FIELD
from server.services.short_drama_workflow import script_has_media_artifacts

logger = logging.getLogger(__name__)

#: 连续性上下文注入提示词的条数预算（已登记资产与每类前序集声明各一批）。
_MAX_REGISTERED_LINES = 20
_MAX_PREVIOUS_LINES = 20

#: 结构校验失败 / 机检失败最多各返修一次（共 2 次生成）。
_GENERATION_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class ImportedEpisode:
    """一次导入成功的结果；``as_dict`` 即 HTTP 响应体。"""

    episode: int
    path: str
    revision: str
    markdown: str
    quality_warnings: tuple[dict[str, object], ...]
    model: dict[str, str]
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "episode": self.episode,
            "path": self.path,
            "revision": self.revision,
            "markdown": self.markdown,
            "quality_warnings": list(self.quality_warnings),
            "model": self.model,
        }
        if self.warnings:
            payload["warnings"] = list(self.warnings)
        return payload


@dataclass(frozen=True, slots=True)
class OriginalView:
    """GET 原文的响应体。"""

    text: str
    revision: str
    generated_from_revision: str | None
    stale: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "revision": self.revision,
            "generated_from_revision": self.generated_from_revision,
            "stale": self.stale,
            "exists": True,
        }


@dataclass(frozen=True, slots=True)
class OriginalSaved:
    """PUT 原文的响应体。"""

    revision: str
    stale: bool

    def as_dict(self) -> dict[str, object]:
        return {"revision": self.revision, "stale": self.stale}


def _episode_entries(project: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = project.get("episodes")
    return [entry for entry in raw if isinstance(entry, Mapping)] if isinstance(raw, list) else []


def _episode_entry(project: Mapping[str, Any], episode: int) -> Mapping[str, Any]:
    entry = next((item for item in _episode_entries(project) if item.get("episode") == episode), None)
    if entry is None:
        raise NotFoundError("episode_not_found", episode=episode)
    return entry


def _require_source_import(project: Mapping[str, Any]) -> None:
    """导入链路只服务 source_import + drama + storyboard；其余项目一律 422。"""

    if (
        resolve_creation_mode(project) != "source_import"
        or project.get("content_mode") != "drama"
        or project.get("generation_mode") != "storyboard"
    ):
        raise UnprocessableError("source_import_mode_required")


def _load_project(projects: ProjectManager, project_name: str) -> dict[str, Any]:
    try:
        return projects.load_project(project_name)
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=project_name) from exc


def _validate_text(text: str) -> None:
    try:
        validate_source_text(text)
    except ValueError as exc:
        code = str(exc)
        if code in {"source_import_text_required", "source_import_text_too_long"}:
            raise UnprocessableError(code) from exc
        raise


def _check_expected_revision(
    expected_revision: str | None,
    original_revision: str | None,
    script_revision: str | None,
) -> None:
    """客户端持有的 revision 必须是原文或源稿之一；首次导入必须两者都不存在。

    UI 的重新导入传的是「已保存原文」的 revision，方案 §7.1 传的是「源稿」revision——
    两者都接受，真正的防丢更新由提交时对两个 revision 的基线比对完成。
    """

    if expected_revision is None:
        if original_revision is not None or script_revision is not None:
            raise ConflictError("source_import_conflict")
        return
    if expected_revision not in (original_revision, script_revision):
        raise ConflictError("source_import_conflict")


def _registered_lines(project: Mapping[str, Any], bucket_name: str) -> tuple[str, ...]:
    bucket = project.get(bucket_name)
    if not isinstance(bucket, Mapping):
        return ()
    lines: list[str] = []
    for name, value in bucket.items():
        if len(lines) >= _MAX_REGISTERED_LINES:
            break
        if not isinstance(name, str) or not name:
            continue
        description = str(value.get("description") or "").strip() if isinstance(value, Mapping) else ""
        if not description:
            # 没写描述的已登记资产交给前序集声明补外观，避免把占位文案当成设定喂给模型。
            continue
        lines.append(f"{name}｜{description}")
    return tuple(lines)


def _registered_keys(project: Mapping[str, Any], bucket_name: str) -> set[str]:
    """已登记且有描述的资产名比较键；它们优先于前序集声明，同名声明不再重复注入。"""

    bucket = project.get(bucket_name)
    if not isinstance(bucket, Mapping):
        return set()
    return {
        asset_name_comparison_key(str(name))
        for name, value in bucket.items()
        if isinstance(value, Mapping) and str(value.get("description") or "").strip()
    }


def _previous_lines(
    declared: tuple[DeclaredAsset, ...],
    registered: set[str],
) -> tuple[str, ...]:
    """前序集声明行；已登记资产优先，同名声明不再重复注入。"""

    lines: list[str] = []
    for asset in declared:
        if len(lines) >= _MAX_PREVIOUS_LINES:
            break
        if asset_name_comparison_key(asset.name) in registered:
            continue
        lines.append(f"{asset.name}｜{asset.appearance}" if asset.appearance else asset.name)
    return tuple(lines)


def _prompt_context(
    project: Mapping[str, Any],
    entry: Mapping[str, Any],
    episode: int,
    tier: SourceImportTier,
    previous: PreviousEpisodeContext,
    text: str,
) -> SourceImportPromptContext:
    style = str(project.get("style") or "").strip() or str(project.get("style_description") or "").strip()
    total_episodes = project.get(TOTAL_EPISODES_FIELD)
    speech_rate = project.get(SPEECH_RATE_FIELD)
    return SourceImportPromptContext(
        tier=tier,
        project_title=str(project.get("title") or "").strip(),
        style=style,
        aspect_ratio=str(project.get("aspect_ratio") or "9:16"),
        episode=episode,
        episode_title=str(entry.get("title") or "").strip(),
        total_episodes=total_episodes if type(total_episodes) is int else None,
        speech_rate=(
            float(speech_rate) if isinstance(speech_rate, (int, float)) and not isinstance(speech_rate, bool) else None
        ),
        source_text=text.strip(),
        registered_characters=_registered_lines(project, "characters"),
        registered_scenes=_registered_lines(project, "scenes"),
        registered_props=_registered_lines(project, "props"),
        previous_characters=_previous_lines(previous.characters, _registered_keys(project, "characters")),
        previous_scenes=_previous_lines(previous.scenes, _registered_keys(project, "scenes")),
        previous_props=_previous_lines(previous.props, _registered_keys(project, "props")),
    )


async def _create_generator(project_name: str, user_id: str) -> TextGenerator:
    try:
        return await TextGenerator.create(
            TextTaskType.SCRIPT,
            project_name,
            purpose=CallPurpose.SCRIPT_GENERATION,
            user_id=user_id,
        )
    except ValueError as exc:
        # 解析不到可用文本 backend：结构化错误码给「去设置配置文本模型」的出路。
        logger.warning("source import has no text backend for %s: %s", project_name, exc)
        raise ConflictError("text_backend_missing") from exc


async def _generate_once(generator: TextGenerator, prompt: str, *, project_name: str) -> str:
    try:
        result = await generator.generate(
            TextGenerationRequest(prompt=prompt),
            project_name=project_name,
            require_complete=True,
        )
    except TextOutputTruncatedError as exc:
        logger.warning("source import output truncated for %s: %s", project_name, exc)
        raise BadGatewayError("source_import_generation_failed") from exc
    except Exception as exc:
        logger.exception("source import generation failed for %s", project_name)
        raise BadGatewayError("source_import_generation_failed") from exc
    return result.text


def _machine_failures(
    markdown: str,
    tier: SourceImportTier,
    previous: PreviousEpisodeContext,
) -> list[QualityIssue]:
    return [
        issue
        for issue in check_episode(markdown, profile=tier.name, previous=previous.markdowns)
        if issue.level == "fail"
    ]


async def _generate_with_repair(
    generator: TextGenerator,
    prompt: str,
    *,
    project_name: str,
    episode: int,
    tier: SourceImportTier,
    previous: PreviousEpisodeContext,
    source_chars: int,
) -> tuple[str, list[QualityIssue]]:
    """生成 + 最多一次返修：结构失败阻塞（422），机检失败返修后仍失败则带着告警落盘。"""

    failures: list[str] = []
    previous_output = ""
    for attempt in range(_GENERATION_ATTEMPTS):
        request_prompt = (
            prompt if attempt == 0 else build_source_import_repair_prompt(prompt, previous_output, failures)
        )
        markdown = await _generate_once(generator, request_prompt, project_name=project_name)
        structure_failures = validate_structure(
            markdown,
            episode=episode,
            tier=tier,
            source_chars=source_chars,
        )
        if structure_failures:
            if attempt + 1 >= _GENERATION_ATTEMPTS:
                raise UnprocessableError("source_import_structure_failed").with_diagnostic(structure_failures)
            previous_output = markdown
            failures = structure_failures
            continue
        quality = _machine_failures(markdown, tier, previous)
        if quality and attempt == 0:
            previous_output = markdown
            failures = [issue.render() for issue in quality]
            continue
        return markdown, quality
    raise BadGatewayError("source_import_generation_failed")


def _warning_dict(issue: QualityIssue) -> dict[str, object]:
    warning: dict[str, object] = {"code": issue.code, "message": issue.message}
    if issue.line is not None:
        warning["line"] = issue.line
    return warning


def _asset_warnings(project: Mapping[str, Any], markdown: str) -> tuple[str, ...]:
    missing = assets_missing_description(project, markdown)
    if not missing:
        return ()
    names = sorted({str(item.get("name") or "") for item in missing if item.get("name")})
    return (f"以下资产尚未登记或缺少描述，请先到资产页补齐：{'、'.join(names)}",)


async def _has_media_artifacts(
    projects: ProjectManager,
    project_name: str,
    entry: Mapping[str, Any],
) -> bool:
    script_file = entry.get("script_file")
    if not isinstance(script_file, str) or not script_file:
        return False
    try:
        script = await asyncio.to_thread(projects.load_script, project_name, script_file)
    except FileNotFoundError:
        return False
    except Exception:
        logger.warning("读取剧本失败，按无媒体产物处理：%s/%s", project_name, script_file, exc_info=True)
        return False
    return script_has_media_artifacts(script)


def _save_import(
    *,
    projects: ProjectManager,
    project_name: str,
    episode: int,
    markdown: str,
    text: str,
    quality_warnings: tuple[dict[str, object], ...],
    model: dict[str, str],
    expected_revision: str | None,
    baseline_original_revision: str | None,
    baseline_script_revision: str | None,
) -> ImportedEpisode:
    generated_from_revision = source_revision(text)
    generated_at = datetime.now(UTC).isoformat()
    with projects.locked_source_registration(project_name) as (source_dir, project, undo):
        # 生成期间项目模式/账本/原文/源稿都可能变化，提交前在同一把锁内复核。
        _require_source_import(project)
        _episode_entry(project, episode)
        current_original = read_original_at(source_dir, episode)
        current_original_revision = current_original.revision if current_original is not None else None
        current_script_revision = read_short_drama_source_revision(source_dir, episode)
        if (
            current_original_revision != baseline_original_revision
            or current_script_revision != baseline_script_revision
        ):
            raise ConflictError("source_import_conflict")
        _check_expected_revision(expected_revision, current_original_revision, current_script_revision)

        try:
            revision = write_short_drama_source_locked(
                source_dir,
                episode,
                markdown,
                expected_revision=current_script_revision,
                undo=undo,
            )
        except ShortDramaSourceConflict as exc:
            raise ConflictError("source_import_conflict") from exc
        register_factory_episode_sources_locked(
            source_dir.parent,
            project,
            [(f"episodes/episode_{episode:02d}.md", markdown.encode("utf-8"))],
            undo,
        )
        write_original_text(source_dir, episode, text, undo)
        write_original_meta(
            source_dir,
            episode,
            OriginalMeta(
                generated_from_revision=generated_from_revision,
                generated_at=generated_at,
                model=model["model"],
                provider=model["provider"],
            ),
            undo,
        )
        warnings = _asset_warnings(project, markdown)
    path = source_path(episode)
    emit_project_change_hint(
        project_name,
        changed_paths=[
            projects.PROJECT_FILE,
            path,
            original_text_relative_path(episode),
            original_meta_relative_path(episode),
            episode_source_relpath(episode),
        ],
    )
    return ImportedEpisode(
        episode=episode,
        path=path,
        revision=revision,
        markdown=markdown,
        quality_warnings=quality_warnings,
        model=model,
        warnings=warnings,
    )


async def import_source_episode(
    *,
    projects: ProjectManager,
    project_name: str,
    episode: int,
    text: str,
    expected_revision: str | None,
    confirm_artifacts: bool,
    user_id: str,
) -> ImportedEpisode:
    """导入一段原文并生成该集工厂格式剧本。"""

    project = await asyncio.to_thread(_load_project, projects, project_name)
    _require_source_import(project)
    entry = _episode_entry(project, episode)
    _validate_text(text)

    project_dir = projects.get_project_path(project_name)
    original = await asyncio.to_thread(read_original, project_dir, episode)
    baseline_original_revision = original.revision if original is not None else None
    baseline_script_revision = await asyncio.to_thread(
        read_short_drama_source_revision,
        project_dir / "source",
        episode,
    )
    _check_expected_revision(expected_revision, baseline_original_revision, baseline_script_revision)

    if not confirm_artifacts and await _has_media_artifacts(projects, project_name, entry):
        raise ConflictError("source_import_confirmation_required")

    tier = derive_tier(project.get(EPISODE_TARGET_DURATION_FIELD))
    previous = await asyncio.to_thread(
        collect_previous_episode_context,
        project_dir,
        _episode_entries(project),
        episode,
    )
    prompt = build_source_import_prompt(_prompt_context(project, entry, episode, tier, previous, text))
    generator = await _create_generator(project_name, user_id)
    model = {"provider": generator.provider_id, "model": generator.model}
    markdown, quality = await _generate_with_repair(
        generator,
        prompt,
        project_name=project_name,
        episode=episode,
        tier=tier,
        previous=previous,
        source_chars=len(text.strip()),
    )
    return await run_sync_transaction(
        _save_import,
        projects=projects,
        project_name=project_name,
        episode=episode,
        markdown=markdown,
        text=text,
        quality_warnings=tuple(_warning_dict(issue) for issue in quality),
        model=model,
        expected_revision=expected_revision,
        baseline_original_revision=baseline_original_revision,
        baseline_script_revision=baseline_script_revision,
    )


async def load_original(
    *,
    projects: ProjectManager,
    project_name: str,
    episode: int,
) -> OriginalView:
    """读取本集已保存原文；没有原文时 404（前端按「尚无原文」处理）。"""

    project = await asyncio.to_thread(_load_project, projects, project_name)
    _require_source_import(project)
    _episode_entry(project, episode)
    original = await asyncio.to_thread(read_original, projects.get_project_path(project_name), episode)
    if original is None:
        raise NotFoundError("source_import_original_not_found")
    return OriginalView(
        text=original.text,
        revision=original.revision,
        generated_from_revision=original.generated_from_revision,
        stale=original.stale,
    )


def _save_original(
    *,
    projects: ProjectManager,
    project_name: str,
    episode: int,
    text: str,
    expected_revision: str | None,
) -> OriginalSaved:
    with projects.locked_source_registration(project_name) as (source_dir, project, undo):
        _require_source_import(project)
        _episode_entry(project, episode)
        current = read_original_at(source_dir, episode)
        if expected_revision is None:
            if current is not None:
                raise ConflictError("source_import_conflict")
        elif current is None or current.revision != expected_revision:
            raise ConflictError("source_import_conflict")
        revision = write_original_text(source_dir, episode, text, undo)
        meta = current.meta if current is not None else None
    emit_project_change_hint(project_name, changed_paths=[original_text_relative_path(episode)])
    stale = meta is not None and meta.generated_from_revision is not None and meta.generated_from_revision != revision
    return OriginalSaved(revision=revision, stale=stale)


async def save_original(
    *,
    projects: ProjectManager,
    project_name: str,
    episode: int,
    text: str,
    expected_revision: str | None,
) -> OriginalSaved:
    """只保存原文，不触发剧本重生成。"""

    project = await asyncio.to_thread(_load_project, projects, project_name)
    _require_source_import(project)
    _episode_entry(project, episode)
    _validate_text(text)
    return await run_sync_transaction(
        _save_original,
        projects=projects,
        project_name=project_name,
        episode=episode,
        text=text,
        expected_revision=expected_revision,
    )


__all__ = [
    "ImportedEpisode",
    "OriginalSaved",
    "OriginalView",
    "import_source_episode",
    "load_original",
    "save_original",
]
