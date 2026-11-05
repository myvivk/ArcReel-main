"""
script_generator.py - 剧本生成器

读取工厂 AI 分镜派生状态，编写唯一动态分镜提示词
"""

import asyncio
import copy
import functools
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional, cast

from pydantic import BaseModel, ValidationError

from lib.artifacts.artifact_activation import (
    active_artifact_currency_resolver,
)
from lib.artifacts.artifact_manifest import ArtifactBasisDescriptor, ArtifactKey
from lib.artifacts.artifact_provenance import (
    build_episode_script_basis,
)
from lib.backends.providers import CallPurpose
from lib.backends.text_backends.base import DEFAULT_MAX_OUTPUT_TOKENS, TextGenerationRequest, TextTaskType
from lib.backends.text_generator import TextGenerator
from lib.config.registry import PROVIDER_REGISTRY
from lib.config.resolver import ConfigResolver
from lib.i18n import _ as translate
from lib.infra.async_thread import run_sync_transaction
from lib.infra.text_utils import strip_json_code_fences
from lib.project.project_manager import ProjectManager
from lib.project.story_plan_docs import factory_authoring_context
from lib.prompts.model_specs import parse_backend_ref, resolve_video_prompt_spec
from lib.prompts.prompt_builders_script import (
    build_drama_prompt,
    render_drama_content_for_prompt_authoring,
    render_video_model_brief,
)
from lib.script.grid.keyframe_parser import shot_scene_text
from lib.script.prompt_authoring_scope import (
    PromptAuthoringSelection,
    PromptOverwrite,
    PromptOverwriteRequired,
    select_prompt_authoring,
    visual_layer_complete,
)
from lib.script.script_models import (
    PENDING_AUTHORING_FIELD,
    DramaImageVisualScript,
)
from lib.script.script_revision import content_fingerprint, formal_script_filename
from lib.script.script_skeleton import resolve_declared_kind, resolve_kind_items
from lib.script.short_drama_import import assets_missing_description, fragment_scene_names, scene_place_name
from lib.script.short_drama_source import load_short_drama_source, short_drama_source_relative_path
from lib.script.shot_prompt_standard import (
    collapse_blank_lines,
    handoff_problem,
    lint_shot_prompt,
    seam_prop_problem,
    storyboard_motion_prompt_problem,
)
from lib.script.storyboard_semantic_review import review_storyboard_semantics
from lib.speech.speech_composition import require_script_unit_admitted, speech_policy_for_project

logger = logging.getLogger(__name__)


#: dry-run 在本次没有条目要编写时的回答：此时真实运行不会调用文本模型，也就没有 prompt 可预览。
_NO_ENTRY_TO_AUTHOR_NOTE = (
    "本次没有要编写的条目：运行时不会调用文本模型；要覆盖已有视觉层，请用 entry_ids 点名条目并传 rewrite=true。"
)

#: 编写产物最多调用几次文本模型：首轮 + 带拒绝原因的返修轮；穷尽仍不合格就报错，
#: 不落盘半成品（与 AI 分镜的 ``_PLAN_ATTEMPTS`` 同一闸、同一口径，D-10）。
_PROMPT_AUTHORING_ATTEMPTS = 5


def _entry_asset_names(item: Mapping[str, Any]) -> set[str]:
    """分镜条目结构化字段里的出场资产名字（格式闸 token 硬闸的出场判定）。

    清单决定参考图列表（mention 只指认、不派生参考图），token 落在清单内才能被渲染成「图N」；
    清单归 AI 分镜管，编写路径只消费不修改。
    """
    names: set[str] = set()
    for field in ("characters_in_scene", "scenes", "props"):
        values = item.get(field)
        if isinstance(values, list):
            names.update(value for value in values if isinstance(value, str) and value.strip())
    return names


_VISUAL_IMAGE_RESPONSE_SCHEMA: dict[str, type[BaseModel]] = {"scenes": DramaImageVisualScript}


def _require_rewrite_fields(kind: str, rewrite: bool, fields: Iterable[str]) -> None:
    """显式重写必须点名本次写哪一层；参考生视频单元的视觉层只有正文，不在此列。

    分镜类省略 ``fields`` 时默认是 ``image_prompt``。一次「没说明白要改哪层」的显式重写会把代价最重的
    那一层整份替换：分镜提示词一变，该分镜已生成的分镜图与已确认的宫格参考素材同时过期，而丢失清单
    只说「图片提示词」。宁可回一句参数怎么传，也不替调用方猜。
    """

    if not rewrite:
        return
    if tuple(fields):
        return
    raise PromptAuthoringTargetError(
        '显式重写必须用 fields=["image_prompt"] 点名动态分镜提示词；'
        "省略 fields 按分镜提示词重写，会让该分镜已生成的分镜图与"
        "已确认的宫格参考素材一并过期"
    )


def _visual_response_schema(targets: "PromptAuthoringTargets") -> type[BaseModel]:
    return _VISUAL_IMAGE_RESPONSE_SCHEMA[targets.kind]


def _visual_field_instruction(targets: "PromptAuthoringTargets") -> str | None:
    fields = {field for entry in targets.selection.entries for field in entry.fields}
    if fields == {"image_prompt"}:
        return (
            "本次只编写 image_prompt：它是参考素材与视频生成共同消费的唯一动态分镜提示词。"
            "每个镜头块必须包含连续时间段，以及主体动态、空间调度、画面与光线、声音四层制作信息。"
            "提示词先于 K 帧生成，正文不得预写 K0、K1 等关键帧编号，也不要输出 video_prompt。"
        )
    return None


def _with_field_scope(prompt: str, targets: "PromptAuthoringTargets") -> str:
    extra = _visual_field_instruction(targets)
    if not extra:
        return prompt
    return f"# 本次编写字段\n{extra}\n\n{prompt}"


class PromptAuthoringTargetError(ValueError):
    """提示词编写的对象无效：该集尚无正式脚本，或 ``entry_ids`` 点名的条目不在正式脚本里。"""


@dataclass(frozen=True, slots=True)
class PromptAuthoringTargets:
    """一次提示词编写的对象：正式脚本快照，与本次要补视觉层的条目（按剧本顺序）。"""

    kind: str
    id_field: str
    script: dict[str, Any]
    entries: tuple[dict[str, Any], ...]
    selection: PromptAuthoringSelection

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(str(entry[self.id_field]) for entry in self.entries)

    def fields_of(self, entry_id: str) -> tuple[str, ...]:
        """该条目本次写回的视觉层字段。"""
        return next(entry.fields for entry in self.selection.entries if entry.entry_id == entry_id)

    @property
    def items(self) -> list[Any]:
        """正式脚本里该骨架的全部条目，供 prompt 渲染前后文。"""
        return cast(list[Any], self.script[self.kind])


def _require_overwrite_acknowledged(
    targets: PromptAuthoringTargets, baseline: str | None, overwrite_revision: str | None
) -> None:
    """显式重写会替换已有视觉层内容时，覆盖令牌须与读取目标时的正式剧本指纹一致。"""
    overwritten = targets.selection.overwritten
    if overwritten and (overwrite_revision is None or overwrite_revision != baseline):
        raise PromptOverwriteRequired(PromptOverwrite(fingerprint=baseline, entries=overwritten))


def _collapsed_visual_field(field: str, value: object) -> object:
    """视觉层写回前压掉镜头块之间的空行；其它字段原样返回。"""

    if field != "image_prompt":
        return value
    if isinstance(value, str):
        return collapse_blank_lines(value)
    if isinstance(value, dict) and isinstance(value.get("scene"), str):
        return {**value, "scene": collapse_blank_lines(value["scene"])}
    return value


class ScriptGenerator:
    """
    剧本生成器

    提示词编写只消费工厂 AI 分镜派生的正式脚本，编写唯一动态分镜提示词
    """

    # 类属性缺省，绕过 __init__ 构造的实例同样读到 None；解析时按需回退到生产 ConfigResolver。
    config_resolver: ConfigResolver | None = None

    def __init__(
        self,
        project_path: str | Path,
        generator: Optional["TextGenerator"] = None,
        *,
        config_resolver: ConfigResolver | None = None,
        video_prompt_spec: str | None = None,
    ):
        """
        初始化生成器

        Args:
            project_path: 项目目录路径，如 projects/test0205
            generator: TextGenerator 实例（可选）。若为 None 则仅支持 build_prompt() dry-run。
            config_resolver: 能力解析器；缺省时解析期构造连接生产数据库的 ConfigResolver。
            video_prompt_spec: 目标视频端点声明的提示词规范（如 ``minimax-h3-local``）；由调用方
                从执行期同一份 generation context 解析后传入，决定写作侧按哪套方言注入。
        """
        self.project_path = Path(project_path)
        self.generator = generator
        self.config_resolver = config_resolver
        self.video_prompt_spec = video_prompt_spec
        self._artifact_basis: ArtifactBasisDescriptor | None = None

        # 加载 project.json
        self.project_json = self._load_project_json()
        self.content_mode = self.project_json.get("content_mode", "drama")

    def _require_factory_episode(self, episode: int) -> None:
        if self.content_mode != "drama" or self.generation_mode != "storyboard":
            raise PromptAuthoringTargetError(translate("short_drama_factory_only"))
        pm = ProjectManager.for_project_dir(self.project_path)
        source = load_short_drama_source(pm, self.project_path.name, episode)
        if source is None or not source.markdown.strip():
            raise PromptAuthoringTargetError(translate("short_drama_source_missing"))
        if source.path != short_drama_source_relative_path(episode):
            raise PromptAuthoringTargetError(translate("short_drama_factory_only"))

    def _require_assets_ready(self, episode: int, fields: Iterable[str] = ()) -> None:
        """编写入口的资产完整性前置（D-10 第 0 步）：与 AI 分镜入口同一判定。

        流程上该集资产完整是分镜的前置，而资产在分镜之后仍可能被改动；写分镜提示词前重新检查，
        缺登记或缺描述时拒绝并引导去资产页，不在编写弹窗里补资产。
        """
        source = load_short_drama_source(
            ProjectManager.for_project_dir(self.project_path), self.project_path.name, episode
        )
        if source is None:
            return
        missing = assets_missing_description(self.project_json, source.markdown)
        if missing:
            raise PromptAuthoringTargetError(
                "请先完成资产步骤，以下资产尚未登记或缺少描述：" + "、".join(item["name"] for item in missing)
            )

    @property
    def generation_mode(self) -> str | None:
        """项目生成模式（project.json 顶层字段）：创建即定、之后不可变，不随集号变化。"""
        return self.project_json.get("generation_mode")

    @classmethod
    async def create(
        cls,
        project_path: str | Path,
        *,
        config_resolver: ConfigResolver | None = None,
        video_prompt_spec: str | None = None,
    ) -> "ScriptGenerator":
        """异步工厂方法，自动从 DB 加载供应商配置创建 TextGenerator。"""
        project_name = Path(project_path).name
        generator = await TextGenerator.create(TextTaskType.SCRIPT, project_name, purpose=CallPurpose.SCRIPT_GENERATION)
        return await asyncio.to_thread(
            cls,
            project_path,
            generator,
            config_resolver=config_resolver,
            video_prompt_spec=video_prompt_spec,
        )

    def _load_prompt_authoring_targets(
        self,
        episode: int,
        filename: str,
        entry_ids: Iterable[str] | None,
        *,
        rewrite: bool = False,
        fields: Iterable[str] = (),
    ) -> PromptAuthoringTargets | None:
        """读正式脚本并定出本次编写的条目；正式脚本不存在时返回 None。

        ``entry_ids`` 为空时范围是全部带待编写标记的条目，非空时是这些条目，其中任一 id 不在正式脚本里
        即抛 ``PromptAuthoringTargetError``。范围内的条目按补缺或显式重写选出（见
        ``select_prompt_authoring``）。不读脚本规划。
        """
        self._require_factory_episode(episode)
        fields = tuple(fields)
        resolver = active_artifact_currency_resolver(self.project_path, self.project_json)
        if (
            resolver.resolve_usable_entry(ArtifactKey.episode_script(episode), artifact_path=f"scripts/{filename}")
            is None
        ):
            return None
        pm = ProjectManager.for_project_dir(self.project_path)
        try:
            script = pm.load_script_readonly(self.project_path.name, filename)
        except FileNotFoundError:
            return None
        meta = script.get("short_drama_meta")
        if not isinstance(meta, Mapping):
            raise PromptAuthoringTargetError(translate("short_drama_factory_only"))
        source = load_short_drama_source(pm, self.project_path.name, episode)
        if source is None or meta.get("source_revision") != source.revision:
            raise PromptAuthoringTargetError(translate("short_drama_source_conflict"))
        kind = resolve_declared_kind(self.content_mode, self.generation_mode)
        _require_rewrite_fields(kind, rewrite, fields)
        raw_items, id_field, _kind = resolve_kind_items(script, kind=kind)
        if not isinstance(raw_items, list):
            raise ValueError(f"集（id={episode}）正式脚本的 {kind} 不是条目数组，无法编写提示词")
        items = [item for item in cast(list[Any], raw_items) if isinstance(item, dict) and id_field in item]
        requested = tuple(dict.fromkeys(entry_ids or ()))
        known = {str(item[id_field]) for item in items}
        unknown = [entry_id for entry_id in requested if entry_id not in known]
        if unknown:
            raise PromptAuthoringTargetError(f"entry_ids 不在集（id={episode}）正式脚本内: {unknown}")
        selection = select_prompt_authoring(
            items,
            kind=kind,
            id_field=id_field,
            entry_ids=requested,
            rewrite=rewrite,
            fields=fields,
        )
        selected = {entry.entry_id for entry in selection.entries}
        entries = tuple(item for item in items if str(item[id_field]) in selected)
        return PromptAuthoringTargets(kind=kind, id_field=id_field, script=script, entries=entries, selection=selection)

    async def generate(
        self,
        episode: int,
        output_filename: str | None = None,
        *,
        instructions: str | None = None,
        entry_ids: Iterable[str] | None = None,
        rewrite: bool = False,
        fields: Iterable[str] = (),
        overwrite_revision: str | None = None,
        rewritten_entry_ids: list[str] | None = None,
        skipped_entry_ids: list[str] | None = None,
    ) -> Path:
        """为工厂 AI 分镜的正式脚本补写或重写本次提示词字段，保留正文与其它制作状态。"""
        if self.generator is None:
            raise RuntimeError("TextGenerator 未初始化，请使用 ScriptGenerator.create() 工厂方法")

        # 兑现 docstring 的「只决定文件名、不接受目录」契约:写盘咽喉 _safe_subpath 能挡绝对
        # 路径与 path traversal,但不会挡子目录(`subdir/x.json` 拼出的 realpath 仍在 scripts/
        # 内,会让剧本写到 scripts/subdir/x.json,偏离扁平布局)。在公开 API 入口 fail-fast 拒,
        # 既兑现契约也避免跑完整套生成流程才撞到错。
        # 显式拒 `\\`:POSIX 上 Path 不当其为分隔符,但 Windows 上是;按跨平台兼容做防御。
        # 空字符串 "" 也显式拒:Path("").name == "" 等于 output_filename 会过前两条,
        # 带空 filename 流到 save_script 在写盘阶段才崩;入口 fail-fast 才不撕裂时机。
        if output_filename is not None and (
            not output_filename or Path(output_filename).name != output_filename or "\\" in output_filename
        ):
            raise ValueError(f"output_filename 只接受纯文件名，不允许目录或路径分隔符: {output_filename!r}")

        self._artifact_basis = None
        self._require_factory_episode(episode)
        filename = output_filename or formal_script_filename(self.project_path, self.project_json, episode)

        # 基线先于读入正式剧本：编写用的快照与 expected_fingerprint 出自同一时刻之前，两者之间
        # 落下的并发保存在写入时按冲突拒绝，而不是被本次写回覆盖。
        formal_baseline = await asyncio.to_thread(content_fingerprint, self.project_path / "scripts" / filename)
        targets = await asyncio.to_thread(
            functools.partial(
                self._load_prompt_authoring_targets,
                episode,
                filename,
                entry_ids,
                rewrite=rewrite,
                fields=fields,
            )
        )
        if targets is None:
            raise PromptAuthoringTargetError(translate("prompt_authoring_storyboard_missing"))
        self._require_assets_ready(episode, fields)
        _require_overwrite_acknowledged(targets, formal_baseline, overwrite_revision)
        if rewritten_entry_ids is not None:
            rewritten_entry_ids[:] = targets.ids
        if skipped_entry_ids is not None:
            skipped_entry_ids[:] = targets.selection.skipped
        output_path = self.project_path / "scripts" / filename
        if not targets.entries:
            logger.info("第 %d 集没有待编写条目，未调用文本模型", episode)
            return output_path

        if targets.kind == "scenes":
            for scene in targets.entries:
                require_script_unit_admitted(
                    "scenes", scene, ignore_marker=True, policy=speech_policy_for_project(self.project_json)
                )
        logger.info(
            "正在为第 %d 集编写提示词（%s，%d/%d 个条目）...",
            episode,
            targets.kind,
            len(targets.entries),
            len(targets.items),
        )
        source = load_short_drama_source(
            ProjectManager.for_project_dir(self.project_path), self.project_path.name, episode
        )

        base_prompt = await self._build_visual_prompt(episode, targets, instructions)
        registered = self._registered_asset_names()
        rejection: str | None = None
        visual_by_id: dict[str, dict] = {}

        async def semantic_failures(visual_items: list[dict]) -> list[tuple[str, str]]:
            originals = {str(item[targets.id_field]): item for item in targets.entries}
            scene_items = [
                item for item in visual_items if "image_prompt" in targets.fields_of(str(item[targets.id_field]))
            ]
            scenes = [
                {
                    "image_prompt": shot_scene_text(item),
                    "duration_seconds": originals[str(item[targets.id_field])].get("duration_seconds"),
                    "scene_description": originals[str(item[targets.id_field])].get("scene_description", ""),
                }
                for item in scene_items
            ]
            if not scenes:
                return []
            if self.generator is None:
                raise ValueError("文本生成器未初始化，无法审查分镜连续性")
            try:
                issues = await review_storyboard_semantics(
                    self.generator,
                    project_name=self.project_path.name,
                    scenes=scenes,
                )
            except ValueError as exc:
                return [(str(item[targets.id_field]), f"语义连续性审查响应无效：{exc}") for item in scene_items]
            failures: list[tuple[str, str]] = []
            for index, issue in issues:
                if index < 1 or index > len(scene_items):
                    return [
                        (str(item[targets.id_field]), f"语义连续性审查返回越界镜头索引：{index}")
                        for item in scene_items
                    ]
                entry_id = str(scene_items[index - 1][targets.id_field])
                failures.append((entry_id, f"分镜 {entry_id} 的语义连续性问题：{issue}"))
            return failures

        pending_entry: str | None = None
        for _attempt in range(_PROMPT_AUTHORING_ATTEMPTS):
            if pending_entry is None:
                prompt = (
                    base_prompt
                    if rejection is None
                    else (f"{base_prompt}\n\n上一轮输出未通过校验：{rejection}。请修正后重新输出完整 {targets.kind}。")
                )
                result = await self._generate_text(
                    TextGenerationRequest(
                        prompt=prompt,
                        response_schema=_visual_response_schema(targets),
                        max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
                    )
                )
                try:
                    parsed = self._parse_visual_layer(result.text, targets)
                    visual_by_id = self._index_visual_layer(parsed, targets)
                except ValueError as exc:
                    rejection = str(exc)
                    continue
                visual = [visual_by_id[entry_id] for entry_id in targets.ids]
                failures = self._authored_visual_failures(
                    visual,
                    targets,
                    registered,
                    episode=episode,
                )
                if failures:
                    pending_entry, rejection = failures[0]
                    continue
                failures = await semantic_failures(visual)
                if failures:
                    pending_entry, rejection = failures[0]
                    continue
                break

            # 局部修复：只重发失败条目；成功条目原样保留在 visual_by_id。
            prompt = self._build_visual_repair_prompt(
                episode=episode,
                targets=targets,
                entry_id=pending_entry,
                old_visual=visual_by_id[pending_entry],
                rejection=rejection or "未通过校验",
                instructions=instructions,
            )
            result = await self._generate_text(
                TextGenerationRequest(
                    prompt=prompt,
                    response_schema=_visual_response_schema(targets),
                    max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
                )
            )
            try:
                parsed = self._parse_visual_layer(result.text, targets)
            except ValueError as exc:
                rejection = f"分镜 {pending_entry} 局部修复结构不合格：{exc}"
                continue
            if len(parsed) != 1 or str(parsed[0].get(targets.id_field, "")) != pending_entry:
                rejection = f"分镜 {pending_entry} 局部修复只允许返回该条目一项"
                continue
            replacement = parsed[0]
            failures = self._authored_visual_failures([replacement], targets, registered, episode=episode)
            if failures:
                rejection = f"分镜 {pending_entry} 局部修复不合格：{failures[0][1]}"
                continue
            visual_by_id[pending_entry] = replacement
            visual = [visual_by_id[entry_id] for entry_id in targets.ids]
            failures = self._authored_visual_failures(visual, targets, registered, episode=episode)
            if failures:
                pending_entry, rejection = failures[0]
                continue
            failures = await semantic_failures(visual)
            if failures:
                pending_entry, rejection = failures[0]
                continue
            break
        else:
            raise ValueError(f"AI 返回的分镜提示词不可用：{rejection}")
        authored = self._merge_visual_layer(targets, visual, episode)
        script_data = self._authored_script(episode, targets, authored)
        pm = ProjectManager.for_project_dir(self.project_path)

        def commit() -> Path:
            def check_source(_project: dict[str, Any]) -> None:
                if load_short_drama_source(pm, self.project_path.name, episode) != source:
                    raise PromptAuthoringTargetError(translate("short_drama_source_conflict"))

            return pm.save_script(
                self.project_path.name,
                script_data,
                filename,
                validate=True,
                artifact_basis=self._artifact_basis,
                expected_fingerprint=formal_baseline,
                project_update=check_source,
            )

        saved_path = await run_sync_transaction(commit)
        logger.info("剧本已保存至 %s", saved_path)
        return saved_path

    async def _build_visual_prompt(
        self,
        episode: int,
        targets: PromptAuthoringTargets,
        instructions: str | None,
    ) -> str:
        """分镜类条目的视觉层 prompt：输入是机械分镜产物里的结构字段。"""
        if targets.kind != "scenes":
            raise PromptAuthoringTargetError(translate("short_drama_factory_only"))
        prompt = self._build_drama_prompt_authoring_prompt(
            list(targets.entries),
            episode,
            instructions,
            previous_prompts=self._previous_prompt_map(targets),
        )
        return _with_field_scope(prompt, targets)

    @staticmethod
    def _previous_prompt_map(targets: PromptAuthoringTargets) -> dict[str, str]:
        """每条待编写分镜的「上一条分镜末镜」：只取已编写、且紧邻的前一条。"""
        script_scenes = targets.script.get("scenes")
        if not isinstance(script_scenes, list):
            return {}
        previous_by_id: dict[str, str] = {}
        previous = ""
        for scene in script_scenes:
            if not isinstance(scene, Mapping):
                continue
            scene_id = str(scene.get(targets.id_field) or "")
            if scene_id and previous:
                previous_by_id[scene_id] = previous
            prompt = shot_scene_text(scene)
            previous = prompt if prompt.strip() else ""
        return previous_by_id

    def _build_visual_repair_prompt(
        self,
        *,
        episode: int,
        targets: PromptAuthoringTargets,
        entry_id: str,
        old_visual: Mapping[str, Any],
        rejection: str,
        instructions: str | None,
    ) -> str:
        """单 entry 局部修复 prompt：只渲染该条目内容 + 旧视觉层 + 失败原因。"""
        entry = next(item for item in targets.entries if str(item[targets.id_field]) == entry_id)
        fields = targets.fields_of(entry_id)
        repair_lines = [
            "# 本次为局部修复",
            f"只重写条目 {entry_id}；其他条目上一轮已通过校验，原样保留，不要输出。",
            f"输出 {targets.kind} 数组且只包含 {entry_id} 一项，不要输出其他 {targets.id_field}。",
            f"该条目上一轮未通过校验：{rejection}",
        ]
        if "image_prompt" in fields:
            repair_lines.append(f"旧 image_prompt.scene：\n{shot_scene_text(old_visual)}")
        repair_instructions = "\n".join(repair_lines)
        if instructions:
            repair_instructions = f"{instructions}\n\n{repair_instructions}"
        entry_for_prompt = dict(entry)
        prompt = self._build_drama_prompt_authoring_prompt(
            [entry_for_prompt],
            episode,
            repair_instructions,
            previous_prompts=self._previous_prompt_map(targets),
        )
        return _with_field_scope(prompt, targets)

    def _project_bucket(self, key: str) -> dict:
        bucket = self.project_json.get(key)
        return bucket if isinstance(bucket, dict) else {}

    def _parse_visual_layer(
        self,
        response_text: str,
        targets: PromptAuthoringTargets,
    ) -> list[dict]:
        """按骨架种类严格校验视觉层响应，返回逐条目动态分镜提示词。"""
        text = strip_json_code_fences(response_text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"提示词编写视觉层 JSON 解析失败: {e}") from e
        try:
            validated = _visual_response_schema(targets).model_validate(data)
        except ValidationError as e:
            raise ValueError(f"提示词编写视觉层结构校验失败: {e}") from e
        return cast(list[dict], validated.model_dump()[targets.kind])

    def _registered_asset_names(self) -> set[str]:
        """项目资产表已登记的名字集合（格式闸 token 硬闸的登记判定）。"""
        names: set[str] = set()
        for bucket_key in ("characters", "scenes", "props"):
            bucket = self._project_bucket(bucket_key)
            names.update(name for name in bucket if isinstance(name, str) and name.strip())
        return names

    def _index_visual_layer(self, visual_items: list[dict], targets: PromptAuthoringTargets) -> dict[str, dict]:
        """把整批视觉层按 id 建索引；缺、多、重直接判整批结构失败（可整批重试）。"""
        id_field = targets.id_field
        visual_by_id: dict[str, dict] = {}
        for item in visual_items:
            entry_id = str(item.get(id_field, ""))
            if not entry_id:
                raise ValueError(f"视觉层条目缺少 {id_field}")
            if entry_id in visual_by_id:
                raise ValueError(f"视觉层 {id_field} 重复: {entry_id}")
            visual_by_id[entry_id] = item
        missing = [entry_id for entry_id in targets.ids if entry_id not in visual_by_id]
        if missing:
            raise ValueError(f"视觉层缺少本次编写的条目: {missing}")
        extra = sorted(set(visual_by_id) - set(targets.ids))
        if extra:
            raise ValueError(f"视觉层含本次编写范围之外的 {id_field}: {extra}")
        return visual_by_id

    def _fragment_gap(self, entry_id: str, prompt: str, targets: PromptAuthoringTargets, episode: int) -> str | None:
        """本集第一镜必须写出碎片空间：没有冷光/叠影/画外声，后面视频就没有这个画面。"""

        fragments = self._fragment_scenes(episode)
        if not fragments:
            return None
        scenes = targets.script.get("scenes")
        if not isinstance(scenes, list) or not scenes or not isinstance(scenes[0], Mapping):
            return None
        if str(scenes[0].get(targets.id_field)) != entry_id:
            return None
        if any(scene_place_name(name) in prompt for name in fragments):
            return None
        if any(marker in prompt for marker in ("冷光", "叠影", "闪回", "画外", "虚影", "残影")):
            return None
        places = "、".join(scene_place_name(name) for name in fragments)
        return f"没有写出本集的碎片空间（{places}）：首镜必须有冷光 / 叠影 / 画外声，不能省略。"

    def _authored_visual_failures(
        self,
        visual: list[dict],
        targets: PromptAuthoringTargets,
        registered: set[str],
        *,
        episode: int | None = None,
    ) -> list[tuple[str, str]]:
        """逐 entry 校验动态分镜提示词，返回 ``(entry_id, 可回喂原因)``。"""
        original_by_id = {str(item[targets.id_field]): item for item in targets.entries}
        authored_by_id = {str(item[targets.id_field]): item for item in visual}
        characters = {str(name) for name in (self.project_json.get("characters") or {})}
        props = {str(name) for name in (self.project_json.get("props") or {})}
        script_scenes = targets.script.get("scenes")
        order: list[str] = []
        stored: dict[str, Mapping[str, Any]] = {}
        if isinstance(script_scenes, list):
            for scene in script_scenes:
                if isinstance(scene, Mapping):
                    entry_id = str(scene.get(targets.id_field))
                    order.append(entry_id)
                    stored[entry_id] = scene
        failures: list[tuple[str, str]] = []
        for item in visual:
            entry_id = str(item[targets.id_field])
            fields = targets.fields_of(entry_id)
            original = original_by_id[entry_id]
            if "image_prompt" in fields:
                scene_text = shot_scene_text(item)
                if not scene_text.strip():
                    failures.append((entry_id, f"分镜 {entry_id} 的 image_prompt.scene 为空"))
                    continue
                if problem := lint_shot_prompt(
                    scene_text,
                    registered_names=registered,
                    allowed_names=_entry_asset_names(original),
                    character_names=characters,
                ):
                    failures.append((entry_id, f"分镜 {entry_id} 的 image_prompt 未通过标准分镜提示词校验：{problem}"))
                    continue
                if problem := storyboard_motion_prompt_problem(scene_text, original.get("duration_seconds")):
                    failures.append((entry_id, f"分镜 {entry_id} 的 image_prompt 未通过动态分镜提示词校验：{problem}"))
                    continue
                if entry_id in order and (position := order.index(entry_id)) > 0:
                    previous_id = order[position - 1]
                    previous_item = authored_by_id.get(previous_id) or stored.get(previous_id)
                    previous_prompt = shot_scene_text(previous_item) if isinstance(previous_item, Mapping) else ""
                    if not previous_prompt.strip():
                        failures.append(
                            (
                                entry_id,
                                f"分镜 {entry_id} 的前一条分镜（{previous_id}）还没有 image_prompt："
                                f"{translate('prompt_authoring_previous_missing')}",
                            )
                        )
                        continue
                    if problem := handoff_problem(previous_prompt, scene_text, character_names=characters):
                        failures.append((entry_id, f"分镜 {entry_id} 的 image_prompt 未接住上一分镜：{problem}"))
                        continue
                    if problem := seam_prop_problem(previous_prompt, scene_text, registered_props=props):
                        failures.append((entry_id, f"分镜 {entry_id} 的 image_prompt 持物未接上：{problem}"))
                        continue
                if episode is not None and (gap := self._fragment_gap(entry_id, scene_text, targets, episode)):
                    failures.append((entry_id, f"分镜 {entry_id} 的 image_prompt {gap}"))
                    continue
        return failures

    def _lint_authored_visual_layer(
        self, visual: list[dict], targets: PromptAuthoringTargets, registered: set[str]
    ) -> None:
        """兼容入口：只做 image_prompt 逐 entry 校验，首个失败抛 ``ValueError``。"""
        failures = self._authored_visual_failures(visual, targets, registered)
        if failures:
            raise ValueError(failures[0][1])

    @staticmethod
    def _merge_visual_layer(targets: PromptAuthoringTargets, visual_items: list[dict], episode: int) -> list[dict]:
        """把视觉层按条目 id 写回正式剧本条目：只写本次选出的视觉层字段，其余字段原样保留。

        视觉层须与本次编写的条目一一对应：缺、多、重都 fail-loud，杜绝错配与漏写。
        """
        id_field = targets.id_field
        visual_by_id: dict[str, dict] = {}
        for item in visual_items:
            entry_id = str(item[id_field])
            if entry_id in visual_by_id:
                raise ValueError(f"集（id={episode}）视觉层 {id_field} 重复: {entry_id}")
            visual_by_id[entry_id] = item
        missing = [entry_id for entry_id in targets.ids if entry_id not in visual_by_id]
        if missing:
            raise ValueError(f"集（id={episode}）视觉层缺少本次编写的条目: {missing}")
        extra = sorted(set(visual_by_id) - set(targets.ids))
        if extra:
            raise ValueError(f"集（id={episode}）视觉层含本次编写范围之外的 {id_field}: {extra}")
        return [
            {
                **entry,
                **{
                    field: _collapsed_visual_field(field, visual_by_id[str(entry[id_field])][field])
                    for field in targets.fields_of(str(entry[id_field]))
                },
            }
            for entry in targets.entries
        ]

    def _authored_script(
        self,
        episode: int,
        targets: PromptAuthoringTargets,
        authored: list[dict],
    ) -> dict[str, Any]:
        """把本次编写的条目按 id 放回正式剧本快照，返回待落盘的整份剧本。

        条目级元数据（清除待编写标记、needs_replan 重判、参考单元取档校验）只作用于本次编写的
        条目；剧本级字段沿用快照，metadata 只刷新 ``updated_at`` 与 ``generator``。
        """
        finished = copy.deepcopy(authored)
        for item in finished:
            item.pop(PENDING_AUTHORING_FIELD, None)
            if not visual_layer_complete(targets.kind, item):
                item[PENDING_AUTHORING_FIELD] = True
        # 正式剧本的 id 是写回定位锚，不随提示词编写改变。
        replacements: dict[str, dict] = {}
        for entry_id, item in zip(targets.ids, finished, strict=True):
            item[targets.id_field] = entry_id
            replacements[entry_id] = item
        script = dict(targets.script)
        script[targets.kind] = [
            replacements.get(str(item.get(targets.id_field)), item) if isinstance(item, dict) else item
            for item in targets.items
        ]
        raw_metadata = script.get("metadata")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
        metadata["updated_at"] = datetime.now(UTC).isoformat()
        metadata["generator"] = self.generator.model if self.generator else "unknown"
        script["metadata"] = metadata
        return script

    def _video_backend_ref(self) -> tuple[str, str]:
        """项目当前的视频模型 ``(provider_id, model_id)``；未配置时返回两个空串。

        顺序与视频生成弹窗落盘的位置一致：参考生视频桶（``video_provider_r2v``，提示词编写
        弹窗里选的模型写这里）→ 图生视频桶 → 项目默认。
        """

        for key in ("video_provider_r2v", "video_provider_i2v", "video_backend"):
            provider, model = parse_backend_ref(self.project_json.get(key))
            if provider and model:
                return provider, model
        return "", ""

    def _video_model_brief(self) -> str | None:
        """写作侧 prompt 的「目标视频模型」块；未配置模型时返回 None（不注入）。"""

        provider_id, model_id = self._video_backend_ref()
        if not provider_id or not model_id:
            return None
        spec = resolve_video_prompt_spec(provider_id, model_id, declared=self.video_prompt_spec)
        return render_video_model_brief(
            spec,
            provider_id=provider_id,
            model_id=model_id,
            durations=self._video_supported_durations(provider_id, model_id),
        )

    @staticmethod
    def _video_supported_durations(provider_id: str, model_id: str) -> list[int]:
        meta = PROVIDER_REGISTRY.get(provider_id)
        info = meta.models.get(model_id) if meta is not None else None
        if info is None:
            return []
        return list(info.supported_durations or [])

    def _build_drama_prompt_authoring_prompt(
        self,
        content_scenes: list,
        episode: int,
        instructions: str | None = None,
        *,
        previous_prompts: Mapping[str, str] | None = None,
    ) -> str:
        """构建 drama prompt_authoring（视觉层）prompt：把分镜产物渲染为输入，仅求本次字段。"""
        scenes_content = render_drama_content_for_prompt_authoring(content_scenes, previous_prompts)
        # 碎片空间必须写在分镜内容之前：放在结尾的事实块里，模型会当作可选背景直接省掉。
        if fragments := self._fragment_scenes(episode):
            places = "、".join(scene_place_name(name) for name in fragments)
            scenes_content = (
                f"【本集碎片空间】{places}：只作压在画面里的冷光 / 叠影 / 画外声，"
                "必须出现在首镜；它不是本幕空间，也不占场景名额。\n\n" + scenes_content
            )
        prompt = build_drama_prompt(
            style=self.project_json.get("style", ""),
            style_description=self.project_json.get("style_description", ""),
            scenes_content=scenes_content,
            episode=episode,
            aspect_ratio=self._resolve_aspect_ratio(),
            # 输出语言与 script_plan（normalize）同取项目 source_language，避免非中文项目 script_plan 内容与 prompt_authoring 视觉割裂
            target_language=self.project_json.get("source_language") or "中文",
            instructions=instructions,
            # 分镜提示词同时是视频生成消费的唯一提示词：配了视频模型就按该模型的方言写。
            model_facts=self._video_model_brief(),
        )
        context = factory_authoring_context(self.project_path, episode)
        self._artifact_basis = ArtifactBasisDescriptor.from_basis(
            build_episode_script_basis(project=self.project_json, story_context=context)
        )
        extras = [context, self._prop_scene_facts(content_scenes, episode)]
        return prompt + "".join(f"\n\n{part}" for part in extras if part)

    def _fragment_scenes(self, episode: int) -> list[str]:
        """本集出现、却未写进 ``[场景]`` 的地点（碎片空间），按登记名返回。"""

        path = self.project_path / short_drama_source_relative_path(episode)
        if not path.is_file():
            return []
        scenes = self.project_json.get("scenes")
        if not isinstance(scenes, Mapping):
            return []
        return fragment_scene_names(scenes.keys(), path.read_text(encoding="utf-8"))

    def _prop_scene_facts(self, content_scenes: list, episode: int) -> str:
        """本集角色 / 场景 / 道具事实 + 碎片空间，只作画面依据、不供抄写。

        编写路径此前只拿得到资产名字，模型只能按常识补道具形状——「白绫盘成同心结」就是
        这么来的（登记写的是同心结系在末端）。碎片空间（冷宫这类只作声画碎片的地点）单独
        列一行：它们不在本幕 ``scenes`` 里，不列出来模型就当作不存在，直接省掉。
        """
        wanted: dict[str, list[str]] = {"characters": [], "scenes": [], "props": []}
        field_by_key = {"characters": "characters_in_scene", "scenes": "scenes", "props": "props"}
        for entry in content_scenes:
            if not isinstance(entry, Mapping):
                continue
            for key, field in field_by_key.items():
                values = entry.get(field)
                if not isinstance(values, list):
                    continue
                for name in values:
                    if isinstance(name, str) and name.strip() and name not in wanted[key]:
                        wanted[key].append(name)
        lines: list[str] = []
        is_scene = False
        for key, label in (("characters", "角色"), ("scenes", "场景"), ("props", "道具")):
            bucket = self.project_json.get(key) or {}
            if not isinstance(bucket, Mapping):
                continue
            for name in wanted[key]:
                record = bucket.get(name)
                description = record.get("description") if isinstance(record, Mapping) else None
                if isinstance(description, str) and description.strip():
                    lines.append(f"- {label}・{name}：{description.strip()}")
                    is_scene = is_scene or key == "scenes"
        if fragments := self._fragment_scenes(episode):
            lines.append("碎片空间（只作压在画面里的光/声/叠影，不是本幕空间）：" + "、".join(fragments))
            is_scene = True
        if not lines:
            return ""
        note = "道具的形状与来路、场景的陈设、人物的穿戴与持物以此为准；这里没写的细节不得写成既成事实。"
        if is_scene:
            note += "碎片空间必须在首镜出现（冷光、叠影、画外声），不得省略，也不得把发生在那里的动作搬进本幕空间。"
        return "【本集角色、场景与道具事实（只作画面依据，不要抄写外观）】\n" + "\n".join(lines) + "\n" + note

    def prompt_overwrite(
        self,
        episode: int,
        *,
        entry_ids: Iterable[str] | None = None,
        rewrite: bool = False,
        fields: Iterable[str] = (),
        overwrite_revision: str | None = None,
    ) -> None:
        """提交前的覆盖预检：与 ``generate`` 同一套对象选择，未认可的覆盖抛 ``PromptOverwriteRequired``。

        点名的条目不在正式剧本里同样抛 ``PromptAuthoringTargetError``；尚无正式剧本时不检查。
        """
        filename = formal_script_filename(self.project_path, self.project_json, episode)
        baseline = content_fingerprint(self.project_path / "scripts" / filename)
        targets = self._load_prompt_authoring_targets(episode, filename, entry_ids, rewrite=rewrite, fields=fields)
        if targets is not None:
            self._require_assets_ready(episode, fields)
            self._require_previous_prompt(targets)
            _require_overwrite_acknowledged(targets, baseline, overwrite_revision)
        else:
            raise PromptAuthoringTargetError(translate("prompt_authoring_storyboard_missing"))

    def _require_previous_prompt(self, targets: PromptAuthoringTargets) -> None:
        """写 image_prompt 时必须等上一条先有提示词：没有邻居可接就拒绝，不静默放行。

        批量重写内部按顺序产出，前一条在同批里已经有了；这里挡的是「跳过上一段、直接写下一段」
        ——那样首镜没有可承接的对象，接不上也查不出来。
        """
        scenes = targets.script.get("scenes")
        if not isinstance(scenes, list):
            return
        order: list[str] = []
        stored: dict[str, Mapping[str, Any]] = {}
        for scene in scenes:
            if isinstance(scene, Mapping):
                entry_id = str(scene.get(targets.id_field))
                order.append(entry_id)
                stored[entry_id] = scene
        for item in targets.entries:
            entry_id = str(item[targets.id_field])
            if "image_prompt" not in targets.fields_of(entry_id) or entry_id not in order:
                continue
            position = order.index(entry_id)
            if position == 0:
                continue
            previous_id = order[position - 1]
            previous = stored.get(previous_id)
            previous_prompt = shot_scene_text(previous) if isinstance(previous, Mapping) else ""
            if not previous_prompt.strip():
                raise PromptAuthoringTargetError(
                    f"{translate('prompt_authoring_previous_missing')}（{previous_id} → {entry_id}）"
                )

    async def build_prompt(
        self,
        episode: int,
        *,
        instructions: str | None = None,
        entry_ids: Iterable[str] | None = None,
        rewrite: bool = False,
        fields: Iterable[str] = (),
    ) -> str:
        """
        构建 Prompt（用于 dry-run 模式）

        与 `generate()` 使用同一套对象选择，只渲染本次
        要编写的条目（``entry_ids`` 或全部待编写条目）；没有要编写的条目时回答那句事实，而不是
        渲染一份不会被发出的空 prompt。``instructions`` 的注入口径与 `generate()` 一致。
        dry-run 恒以该集绑定的正式剧本为准：它不落盘，也就没有 ``output_filename`` 可言。
        """
        targets = self._load_prompt_authoring_targets(
            episode,
            formal_script_filename(self.project_path, self.project_json, episode),
            entry_ids,
            rewrite=rewrite,
            fields=fields,
        )
        if targets is None:
            raise PromptAuthoringTargetError(translate("prompt_authoring_storyboard_missing"))
        if not targets.entries:
            return _NO_ENTRY_TO_AUTHOR_NOTE
        return await self._build_visual_prompt(episode, targets, instructions)

    def _resolve_aspect_ratio(self) -> str:
        """读取项目画面比例，缺省采用竖屏。"""
        if "aspect_ratio" in self.project_json and isinstance(self.project_json["aspect_ratio"], str):
            return self.project_json["aspect_ratio"]
        return "9:16"

    def _load_project_json(self) -> dict:
        """加载 project.json"""
        path = self.project_path / "project.json"
        if not path.exists():
            raise FileNotFoundError(f"未找到 project.json: {path}")

        with open(path, encoding="utf-8") as f:
            return json.load(f)

    async def _generate_text(self, request: TextGenerationRequest) -> Any:
        """Call the paid text provider after one shared formal-input recheck."""

        assert self.generator is not None  # generate() 入口已检查
        return await self.generator.generate(request, project_name=self.project_path.name)
