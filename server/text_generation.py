"""工厂分镜的提示词编写服务，供 HTTP、Agent 与持久任务共用。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

from lib.config.resolver import ConfigResolver
from lib.episode.episode_ids import describe_episode_for_agent
from lib.generation.video_request_facts import VideoRequestFactsError, VideoRequestFactsFailure
from lib.i18n import _ as translate
from lib.infra.schema_guards import is_int, is_str
from lib.project.project_manager import ProjectManager
from lib.script.prompt_authoring_scope import (
    DEFAULT_AUTHORING_FIELDS,
    PromptOverwriteRequired,
    prompt_overwrite_with_text,
)
from lib.script.script_generator import PromptAuthoringTargetError, ScriptGenerator
from lib.script.script_skeleton import resolve_declared_kind
from lib.script.storyboard_mentions import render_storyboard_mention_warnings, storyboard_mention_warnings
from lib.workflow.operation_admission import AdmissionReason, OperationAdmission

MAX_INSTRUCTIONS_LEN = 4000


@dataclass(frozen=True, slots=True)
class TextGenerationRequest:
    episode: int
    instructions: str | None = None
    dry_run: bool = False
    #: 提示词编写的范围；为空时是全部待编写条目。
    entry_ids: tuple[str, ...] = ()
    #: 提示词编写显式重写范围内条目的本次字段；为 False 时只补缺失的本次字段。
    rewrite: bool = False
    #: 提示词编写的字段范围；为空时分镜类默认只写 image_prompt，video_prompt 须显式单独请求。
    fields: tuple[str, ...] = ()
    #: 用户认可覆盖的正式脚本指纹，取自 ``prompt_overwrite.revision``。
    overwrite_revision: str | None = None

    def __post_init__(self) -> None:
        if not is_int(self.episode, minimum=1):
            raise ValueError("episode must be a positive integer")
        # 队列 payload 经 JSON 往返后 entry_ids 是 list：在此归一为 tuple，让「从工具入口构造」
        # 与「从 payload 还原」两条路径得到同一个值，任务事实比对才不会因容器类型分叉。
        entry_ids = tuple(self.entry_ids)
        if any(not is_str(entry_id) or not entry_id for entry_id in entry_ids):
            raise ValueError("entry_ids must be non-empty strings")
        fields = tuple(self.fields)
        if any(field not in {"image_prompt", "video_prompt"} for field in fields):
            raise ValueError("fields must be image_prompt/video_prompt")
        if {"image_prompt", "video_prompt"} <= set(fields):
            raise ValueError("fields must choose either image_prompt or video_prompt, not both")
        object.__setattr__(self, "entry_ids", entry_ids)
        object.__setattr__(self, "fields", fields)

    def to_payload(self) -> dict[str, object]:
        """队列任务 payload：只用 JSON 原生类型。

        ``asdict`` 会把 ``entry_ids`` 留成 tuple，而从数据库读回来的同一份 payload 是 list；
        「本次请求与在跑任务是否同一件事」的比对按值相等判定，两种容器类型会让同一个请求
        判成不同，把幂等提交变成一次任务冲突。
        """
        return {**asdict(self), "entry_ids": list(self.entry_ids), "fields": list(self.fields)}


@dataclass(frozen=True, slots=True)
class TextGenerationResult:
    message: str
    #: locale-neutral 的 ``{"key", "params"}`` 提示条目（如画面描述里没绑定参考图的 ``@[名称]``），
    #: 与任务 ``result.warnings`` 同一形态，读侧按语言渲染。
    warnings: list[dict[str, Any]] = dataclass_field(default_factory=list)


class TextGenerationError(Exception):
    """Expected refusal from a text-generation handler."""


class PromptOverwriteRequiredError(TextGenerationError):
    """提示词显式重写会替换已有视觉层内容，而调用方未认可覆盖；携带丢失清单（含 ``text``）。"""

    def __init__(self, message: str, overwrite: dict[str, Any]) -> None:
        super().__init__(message)
        self.overwrite = overwrite


def _prompt_overwrite_error(episode_label: str, exc: PromptOverwriteRequired) -> PromptOverwriteRequiredError:
    overwrite = prompt_overwrite_with_text(exc.overwrite.to_dict(), translate)
    # 重写分镜提示词的代价不止那一段文本：依赖它的分镜图与宫格参考素材会一起过期。
    cascade = ""
    if "image_prompt" in {field for entry in exc.overwrite.entries for field in entry.overwritten}:
        cascade = (
            "\n⚠️ 清单里含 image_prompt（分镜提示词）：一旦重写，这些分镜已生成的分镜图与已确认的宫格"
            "参考素材会一并转为「已过期」，必须重新生成；转述给用户时一并说明，请对方确认这些产物可以重做。"
        )
    return PromptOverwriteRequiredError(
        f"⚠️ {episode_label} 的显式重写需要用户确认覆盖，本次未调用文本模型、未写入。\n{overwrite['text']}{cascade}\n"
        "须先把上面的丢失清单原文转述给用户，得到明确同意后，再以 "
        "overwrite_revision=params.prompt_overwrite.revision 重新调用；"
        "正式脚本在此期间又有变化时会按新清单再次拒绝。",
        overwrite,
    )


class VideoRequestFactsUnavailableError(TextGenerationError):
    """视频请求事实解析不出（模型能力不满足、时长档位缺失等），文本任务无法继续；携带失败对象。

    ``failure`` 的问题码、参数与修复指引原样进入工具问题与任务失败，读侧按问题码渲染原因。
    """

    def __init__(self, failure: VideoRequestFactsFailure) -> None:
        super().__init__(video_facts_failure_text(failure))
        self.failure = failure


def _instructions(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TextGenerationError("❌ 参数错误：instructions 必须是文本")
    if len(value) > MAX_INSTRUCTIONS_LEN:
        raise TextGenerationError(
            f"❌ 参数错误：instructions 过长（{len(value)} 字符，上限 {MAX_INSTRUCTIONS_LEN}），请精简后重试"
        )
    return value.strip() or None


def video_facts_failure_text(failure: VideoRequestFactsFailure) -> str:
    """视频请求事实解析不出时回给 Agent 的说明：按问题码陈述原因与出路，附问题码与参数摘要。

    问题码与参数就是失败契约，与预检、执行同码。
    """
    params: dict[str, Any] = failure.parameters()
    return f"❌ {translate(failure.code, **params)}（{failure.summary()}）"


class OperationNotAdmittedError(TextGenerationError):
    """AI 操作的结构准入不成立；``reason`` 与制作状态 ``operations`` 里同一操作的理由码一致。"""

    def __init__(self, operation: str, admission: OperationAdmission, message: str) -> None:
        super().__init__(message)
        self.operation = operation
        self.reason = admission.reason


def _refusal_text(reason: AdmissionReason | None, episode: int | None, detail: str = "") -> str:
    where = translate("operation_episode", episode=episode) if episode is not None else translate("operation_project")
    code = reason.value if reason is not None else "refused"
    text = f"⏸️ {translate(f'operation_{code}', where=where)}（{code}）。"
    return f"{text}{detail}" if detail else text


def require_admitted(
    operation: str, admission: OperationAdmission, *, episode: int | None = None, detail: str = ""
) -> None:
    """准入不成立时拒绝：理由码与制作状态同源，文案只作转述；``detail`` 补充入口自己知道的处置方式。"""
    if not admission.admitted:
        raise OperationNotAdmittedError(operation, admission, _refusal_text(admission.reason, episode, detail))


def prompt_authoring_preflight(
    project_path: Path,
    episode: int,
    *,
    entry_ids: Sequence[str] = (),
    rewrite: bool = False,
    fields: Sequence[str] = (),
    overwrite_revision: str | None = None,
) -> None:
    """调用生成器的同一份工厂准入、范围选择与覆盖检查。"""
    try:
        ScriptGenerator(project_path).prompt_overwrite(
            episode, entry_ids=entry_ids, rewrite=rewrite, fields=fields, overwrite_revision=overwrite_revision
        )
    except PromptOverwriteRequired as exc:
        projects = ProjectManager.for_project_dir(project_path)
        raise _prompt_overwrite_error(
            describe_episode_for_agent(projects.load_project(project_path.name), episode), exc
        ) from exc
    except PromptAuthoringTargetError as exc:
        raise TextGenerationError(str(exc)) from exc


async def generate_episode_script(
    request: TextGenerationRequest,
    *,
    project_name: str,
    projects: ProjectManager,
    config_resolver: ConfigResolver,
) -> TextGenerationResult:
    episode = request.episode
    instructions = _instructions(request.instructions)
    project_path = projects.get_project_path(project_name)
    await asyncio.to_thread(
        prompt_authoring_preflight,
        project_path,
        episode,
        entry_ids=request.entry_ids,
        fields=request.fields,
        rewrite=request.rewrite,
        overwrite_revision=request.overwrite_revision,
    )

    try:
        if request.dry_run:
            generator = await asyncio.to_thread(
                ScriptGenerator,
                project_path,
                config_resolver=config_resolver,
            )
            prompt = await generator.build_prompt(
                episode,
                instructions=instructions,
                entry_ids=request.entry_ids,
                rewrite=request.rewrite,
                fields=request.fields,
            )
            return TextGenerationResult(f"DRY RUN — 以下是将发送给文本模型的 Prompt:\n\n{prompt}")

        generator = await ScriptGenerator.create(
            project_path,
            config_resolver=config_resolver,
        )
        rewritten: list[str] = []
        skipped: list[str] = []
        result_path = await generator.generate(
            episode=episode,
            instructions=instructions,
            entry_ids=request.entry_ids,
            fields=request.fields,
            rewrite=request.rewrite,
            overwrite_revision=request.overwrite_revision,
            rewritten_entry_ids=rewritten,
            skipped_entry_ids=skipped,
        )
    except PromptOverwriteRequired as exc:
        raise _prompt_overwrite_error(_describe(projects, project_name, episode), exc) from exc
    except PromptAuthoringTargetError as exc:
        # 点名的条目不在正式剧本内是调用方的错：报「拒绝生成」而不是让它冒成 internal_error，
        # 后者会引导 Agent 原样重试同一份必然失败的参数。
        raise TextGenerationError(f"❌ 编写范围无效: {exc}") from exc
    except VideoRequestFactsError as exc:
        raise VideoRequestFactsUnavailableError(exc.failure) from exc
    except FileNotFoundError as exc:
        raise TextGenerationError(f"❌ 文件错误: {exc}") from exc
    skipped_note = (
        f"\n   视觉层已齐、补缺未改动的条目: {'、'.join(skipped)}（要覆盖请传 rewrite=true）" if skipped else ""
    )
    # 写了哪一层必须在回执里自证：调用方除了看进展，得先确认这一步动的是地方。
    written_fields = _written_fields(projects, project_name, request)
    fields_note = f"\n   本次编写字段: {'、'.join(written_fields)}" if written_fields else ""
    if not rewritten:
        redo = "要重新分镜请回到工厂 AI 分镜步骤"
        return TextGenerationResult(
            f"✅ {describe_episode_for_agent(generator.project_json, episode)} 没有待编写的条目，"
            f"未调用文本模型，正式脚本未改动: {result_path}{skipped_note}\n"
            f"   要覆盖已有视觉层请用 entry_ids 点名并传 rewrite=true；{redo}。"
        )
    rewritten_note = "、".join(rewritten)
    summary = f"✅ 剧本生成完成: {result_path}\n   本次编写条目: {rewritten_note}{fields_note}{skipped_note}"
    warnings = await asyncio.to_thread(_rewritten_mention_warnings, projects, project_name, result_path, rewritten)
    summary += _unbound_mentions_note(warnings)
    return TextGenerationResult(summary, warnings)


def _written_fields(projects: ProjectManager, project_name: str, request: TextGenerationRequest) -> tuple[str, ...]:
    """本次编写动了哪些视觉层字段：回执要能自证，取不到就干脆不写这一行。

    省略 ``fields`` 时的缺省值按项目声明的骨架种类取值。这里刻意绕开 ``ScriptGenerator``
    实例重新求一次：它的 ``generate`` 在测试里常被替身替换，回执不该依赖替身有没有那些成员。
    """

    if request.fields:
        return tuple(request.fields)
    try:
        project = projects.load_project(project_name)
    except (OSError, ValueError):
        return ()
    return DEFAULT_AUTHORING_FIELDS.get(
        resolve_declared_kind(project.get("content_mode"), project.get("generation_mode")), ()
    )


def _rewritten_mention_warnings(
    projects: ProjectManager,
    project_name: str,
    result_path: Path,
    rewritten: Sequence[str],
) -> list[dict[str, Any]]:
    """本次重写条目的画面描述里没绑定参考图的 ``@[名称]``；剧本读不回来时不阻断回执。"""
    if not rewritten:
        return []
    try:
        project = projects.load_project(project_name)
        script = projects.load_script_readonly(project_name, result_path.name)
    except (OSError, ValueError):
        # 剧本已落盘，回执不能因为读回失败（文件缺失、I/O 故障、JSON 不合法）而失败。
        return []
    return storyboard_mention_warnings(project, script, unit_ids=rewritten)


def _unbound_mentions_note(warnings: Sequence[Mapping[str, Any]]) -> str:
    return "".join(f"\n   ⚠️ {line}" for line in render_storyboard_mention_warnings(warnings, translate))


def _describe(projects: ProjectManager, project_name: str, episode: int) -> str:
    return describe_episode_for_agent(projects.load_project(project_name), episode)
