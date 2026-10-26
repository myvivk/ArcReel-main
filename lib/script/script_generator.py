"""
script_generator.py - 剧本生成器

读取工厂 AI 分镜派生状态，独立编写分镜提示词与视频提示词
"""

import asyncio
import copy
import functools
import json
import logging
import re
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
from lib.config.resolver import ConfigResolver
from lib.i18n import _ as translate
from lib.infra.async_thread import run_sync_transaction
from lib.infra.content_digest import sha256_file
from lib.infra.path_safety import safe_join
from lib.infra.text_utils import strip_json_code_fences
from lib.project.project_manager import ProjectManager
from lib.project.story_plan_docs import factory_authoring_context
from lib.prompts.prompt_builders_script import (
    build_drama_prompt,
    render_drama_content_for_prompt_authoring,
)
from lib.script.grid.keyframe_parser import format_action_segments, keyframe_reference_problems, shot_scene_text
from lib.script.grid.shot_grid_manager import ShotGridManager, select_shot_grids
from lib.script.grid.shot_models import ShotGrid
from lib.script.grid.video_prompt_targets import (
    video_prompt_basis_for,
    video_prompt_body_text,
    video_prompt_text,
)
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
    DramaVideoVisualScript,
    DramaVisualScript,
)
from lib.script.script_revision import content_fingerprint, formal_script_filename
from lib.script.script_skeleton import resolve_declared_kind, resolve_kind_items
from lib.script.short_drama_import import assets_missing_description, fragment_scene_names, scene_place_name
from lib.script.short_drama_source import (
    ShortDramaSource,
    load_short_drama_source,
    short_drama_source_relative_path,
)
from lib.script.shot_prompt_standard import handoff_problem, lint_shot_prompt
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


_VISUAL_RESPONSE_SCHEMA: dict[str, type[BaseModel]] = {"scenes": DramaVisualScript}
_VISUAL_IMAGE_RESPONSE_SCHEMA: dict[str, type[BaseModel]] = {"scenes": DramaImageVisualScript}
_VISUAL_VIDEO_RESPONSE_SCHEMA: dict[str, type[BaseModel]] = {"scenes": DramaVideoVisualScript}

_VIDEO_PROMPT_WRAPPER = re.compile(
    r"(?:(?:Avoid|Voice_Profiles|Dialogue|aspect_ratio|duration_seconds|negative_prompt|model)"
    r"\s*[:：=]|</?(?:Avoid|Voice_Profiles|Dialogue)>|画面比例\s*[:：=])",
    re.IGNORECASE,
)


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
        '显式重写必须用 fields 点名本次编写的字段（分镜类传 fields=["image_prompt"] 或 '
        'fields=["video_prompt"]）；省略 fields 按分镜提示词重写，会让该分镜已生成的分镜图与'
        "已确认的宫格参考素材一并过期"
    )


def _visual_response_schema(targets: "PromptAuthoringTargets") -> type[BaseModel]:
    fields = {field for entry in targets.selection.entries for field in entry.fields}
    if fields == {"image_prompt"}:
        return _VISUAL_IMAGE_RESPONSE_SCHEMA[targets.kind]
    if fields == {"video_prompt"}:
        return _VISUAL_VIDEO_RESPONSE_SCHEMA[targets.kind]
    return _VISUAL_RESPONSE_SCHEMA[targets.kind]


def _visual_field_instruction(targets: "PromptAuthoringTargets") -> str | None:
    fields = {field for entry in targets.selection.entries for field in entry.fields}
    if fields == {"image_prompt"}:
        return (
            "本次只编写 image_prompt（分镜提示词）。不要输出 video_prompt；"
            "视频提示词必须等参考素材生成并确认后再单独编写。"
        )
    if fields == {"video_prompt"}:
        return (
            "本次只编写 video_prompt（视频提示词）。输入里有分镜提示词与该分镜已确认宫格的逐格文本"
            "（K0→Kn）：按 K0→Kn 的时序写「怎么动」，不复述每格已经写死的静帧外观、姿态与服装；"
            "每个时间段单独一行（如「0-3秒：…」换行「3-6秒：…」），不要连写成一段；"
            "只引用真实存在的 K（正文出现的 K 编号必须小于格数），台词不进 action；"
            "不写 BGM、画面比例、时长与模型参数，也不要输出 image_prompt 或改写分镜提示词。"
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


class ScriptGenerator:
    """
    剧本生成器

    提示词编写只消费工厂 AI 分镜派生的正式脚本，独立编写分镜提示词与视频提示词
    """

    # 类属性缺省，绕过 __init__ 构造的实例同样读到 None；解析时按需回退到生产 ConfigResolver。
    config_resolver: ConfigResolver | None = None

    def __init__(
        self,
        project_path: str | Path,
        generator: Optional["TextGenerator"] = None,
        *,
        config_resolver: ConfigResolver | None = None,
    ):
        """
        初始化生成器

        Args:
            project_path: 项目目录路径，如 projects/test0205
            generator: TextGenerator 实例（可选）。若为 None 则仅支持 build_prompt() dry-run。
            config_resolver: 能力解析器；缺省时解析期构造连接生产数据库的 ConfigResolver。
        """
        self.project_path = Path(project_path)
        self.generator = generator
        self.config_resolver = config_resolver
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
        缺登记或缺描述时拒绝并引导去资产页，不在编写弹窗里补资产。视频提示词等参考素材确认后才
        编写，届时资产已就绪，不重复此闸。
        """
        if set(fields) == {"video_prompt"}:
            return
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
    ) -> "ScriptGenerator":
        """异步工厂方法，自动从 DB 加载供应商配置创建 TextGenerator。"""
        project_name = Path(project_path).name
        generator = await TextGenerator.create(TextTaskType.SCRIPT, project_name, purpose=CallPurpose.SCRIPT_GENERATION)
        return await asyncio.to_thread(
            cls,
            project_path,
            generator,
            config_resolver=config_resolver,
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
        if set(fields) == {"video_prompt"}:
            self._video_prompt_source({}, episode)
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
        if set(fields) == {"video_prompt"}:
            source = self._video_prompt_source(script, episode)
            if source is not None and not items:
                raise PromptAuthoringTargetError(translate("video_prompt_storyboard_missing"))
            grids = select_shot_grids(
                (
                    grid
                    for grid in ShotGridManager(self.project_path).list_all()
                    if grid.script_file == filename and grid.status == "confirmed"
                ),
                items=items,
                id_field=id_field,
            )
            if requested:
                for item in items:
                    if str(item[id_field]) in requested:
                        self._require_video_prompt_grid(item, grids.get(str(item[id_field])))
            else:
                requested = tuple(
                    str(item[id_field])
                    for item in items
                    if shot_scene_text(item).strip() and str(item[id_field]) in grids
                )
                if not requested:
                    return PromptAuthoringTargets(
                        kind=kind,
                        id_field=id_field,
                        script=script,
                        entries=(),
                        selection=PromptAuthoringSelection(entries=(), skipped=()),
                    )
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
            raise PromptAuthoringTargetError(translate("video_prompt_storyboard_missing"))
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
        video_inputs = await asyncio.to_thread(self._video_prompt_inputs, targets, episode)
        source = load_short_drama_source(
            ProjectManager.for_project_dir(self.project_path), self.project_path.name, episode
        )

        def image_digests() -> dict[str, str]:
            return {
                path: sha256_file(safe_join(self.project_path, path))
                for grid in (video_inputs or {}).values()
                for path in grid.cell_paths
            }

        frozen_images = await asyncio.to_thread(image_digests)
        base_prompt = await self._build_visual_prompt(episode, targets, instructions, video_inputs=video_inputs)
        registered = self._registered_asset_names()
        rejection: str | None = None
        visual_by_id: dict[str, dict] = {}
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
                    visual, targets, registered, video_inputs=video_inputs, episode=episode
                )
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
                video_inputs=video_inputs,
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
            failures = self._authored_visual_failures(
                [replacement], targets, registered, video_inputs=video_inputs, episode=episode
            )
            if failures:
                rejection = f"分镜 {pending_entry} 局部修复不合格：{failures[0][1]}"
                continue
            visual_by_id[pending_entry] = replacement
            visual = [visual_by_id[entry_id] for entry_id in targets.ids]
            failures = self._authored_visual_failures(
                visual, targets, registered, video_inputs=video_inputs, episode=episode
            )
            if failures:
                pending_entry, rejection = failures[0]
                continue
            break
        else:
            raise ValueError(f"AI 返回的分镜提示词不可用：{rejection}")
        authored = self._merge_visual_layer(targets, visual, episode)
        script_data = self._authored_script(episode, targets, authored, video_inputs=video_inputs)
        pm = ProjectManager.for_project_dir(self.project_path)

        def commit() -> Path:
            manager = ShotGridManager(self.project_path)
            frozen = video_inputs or {}
            with manager.locked_records(grid.id for grid in frozen.values()) as current:
                for grid in frozen.values():
                    present = current[grid.id]
                    if present is None or present.to_dict() != grid.to_dict():
                        raise PromptAuthoringTargetError(translate("video_prompt_inputs_changed"))
                    self._require_video_prompt_grid({}, present, require_scene=False)
                if image_digests() != frozen_images:
                    raise PromptAuthoringTargetError(translate("video_prompt_inputs_changed"))

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
        *,
        video_inputs: Mapping[str, ShotGrid] | None = None,
    ) -> str:
        """分镜类条目（segments / scenes / shots）的视觉层 prompt：输入是正式剧本里这些条目的内容字段。"""
        if targets.kind != "scenes":
            raise PromptAuthoringTargetError(translate("short_drama_factory_only"))
        prompt = self._build_drama_prompt_authoring_prompt(
            list(targets.entries),
            episode,
            instructions,
            video_inputs=video_inputs,
        )
        return _with_field_scope(prompt, targets)

    def _build_visual_repair_prompt(
        self,
        *,
        episode: int,
        targets: PromptAuthoringTargets,
        entry_id: str,
        old_visual: Mapping[str, Any],
        rejection: str,
        instructions: str | None,
        video_inputs: Mapping[str, ShotGrid] | None,
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
        if "video_prompt" in fields:
            repair_lines.append(f"旧 video_prompt：\n{video_prompt_body_text(old_visual)}")
        repair_instructions = "\n".join(repair_lines)
        if instructions:
            repair_instructions = f"{instructions}\n\n{repair_instructions}"
        repair_video_inputs = (
            {entry_id: video_inputs[entry_id]} if video_inputs is not None and entry_id in video_inputs else None
        )
        entry_for_prompt = dict(entry)
        if "video_prompt" in fields:
            # 视频局部修复的 K 帧上下文必须基于本轮已生成的 image_prompt；原条目可能还是 None。
            current_image = old_visual.get("image_prompt")
            if current_image is not None:
                entry_for_prompt["image_prompt"] = current_image
        prompt = self._build_drama_prompt_authoring_prompt(
            [entry_for_prompt],
            episode,
            repair_instructions,
            video_inputs=repair_video_inputs,
        )
        return _with_field_scope(prompt, targets)

    def _video_prompt_inputs(self, targets: PromptAuthoringTargets, episode: int) -> Mapping[str, ShotGrid] | None:
        """视频提示词编写才带的输入：本集已确认宫格，按分镜 id 索引（D-15）。

        ``None`` 表示本次不是写视频提示词，调用方据此不渲染分镜提示词 / 关键帧块——
        写 image_prompt 时模型看不到宫格，也就不会把逐格文本抄回去。
        """

        fields = {field for entry in targets.selection.entries for field in entry.fields}
        if fields != {"video_prompt"}:
            return None
        source = self._video_prompt_source(targets.script, episode)
        grids = select_shot_grids(
            (
                grid
                for grid in ShotGridManager(self.project_path).list_all()
                if grid.episode == episode
                and grid.status == "confirmed"
                and grid.script_file == formal_script_filename(self.project_path, self.project_json, episode)
            ),
            items=targets.entries,
            id_field=targets.id_field,
        )
        selected: dict[str, ShotGrid] = {}
        for item in targets.entries:
            entry_id = str(item[targets.id_field])
            if source is not None:
                anchor = item.get("source_text")
                source_lines = iter(source.markdown.splitlines())
                if (
                    not isinstance(anchor, str)
                    or not anchor.strip()
                    or not all(
                        any(original == line for original in source_lines)
                        for line in anchor.splitlines()
                        if line.strip()
                    )
                ):
                    raise PromptAuthoringTargetError(translate("video_prompt_source_unaligned"))
            selected[entry_id] = self._require_video_prompt_grid(item, grids.get(entry_id))
        return selected

    def _require_video_prompt_grid(
        self, item: Mapping[str, Any], grid: ShotGrid | None, *, require_scene: bool = True
    ) -> ShotGrid:
        if require_scene and not shot_scene_text(item).strip():
            raise PromptAuthoringTargetError(translate("shot_grid_scene_missing"))
        if grid is None or grid.status != "confirmed":
            raise PromptAuthoringTargetError(translate("video_prompt_grid_unconfirmed"))
        count = grid.rows * grid.cols
        if (
            count <= 0
            or len(grid.cells) != count
            or [cell.index for cell in grid.cells] != list(range(count))
            or any(not cell.prompt.strip() or not cell.image_path for cell in grid.cells)
        ):
            raise PromptAuthoringTargetError(translate("shot_grid_cells_invalid", count=count))
        for path in grid.cell_paths:
            if not safe_join(self.project_path, path).is_file():
                raise PromptAuthoringTargetError(translate("video_prompt_grid_unconfirmed"))
        return grid

    def _video_prompt_source(self, script: Mapping[str, Any], episode: int) -> ShortDramaSource | None:
        pm = ProjectManager.for_project_dir(self.project_path)
        source = load_short_drama_source(pm, self.project_path.name, episode)
        factory = (
            source is not None
            or isinstance(script.get("short_drama_meta"), Mapping)
            or (self.project_path / "source" / "short_drama").is_dir()
        )
        if not factory:
            return None
        if source is None or not source.markdown.strip():
            raise PromptAuthoringTargetError(translate("short_drama_source_missing"))
        meta = script.get("short_drama_meta")
        if isinstance(meta, Mapping) and meta.get("source_revision") not in (None, source.revision):
            raise PromptAuthoringTargetError(translate("short_drama_source_conflict"))
        return source

    def _project_bucket(self, key: str) -> dict:
        bucket = self.project_json.get(key)
        return bucket if isinstance(bucket, dict) else {}

    def _parse_visual_layer(
        self,
        response_text: str,
        targets: PromptAuthoringTargets,
    ) -> list[dict]:
        """按骨架种类严格校验视觉层响应，返回逐条目视觉层 dict（id + image_prompt + video_prompt）。

        video_prompt 的正文/K 引用语义校验不在这里做：调用方要逐 entry 收集失败并做局部修复，
        不能因为一个 entry 的正文错误直接让整批失败。
        """
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
            if not isinstance(item, dict):
                raise ValueError("视觉层条目不是对象")
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
        scenes = targets.script.get("scenes") if isinstance(targets.script, Mapping) else None
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
        video_inputs: Mapping[str, ShotGrid] | None,
        episode: int | None = None,
    ) -> list[tuple[str, str]]:
        """逐 entry 校验 image_prompt/video_prompt，返回 ``(entry_id, 可回喂原因)``。

        image_prompt 走标准分镜提示词 lint；video_prompt 走 K 引用/包装字段/正文非空校验。
        失败只记录该 entry，调用方据此只对它做局部修复，成功条目原样保留。
        """
        original_by_id = {str(item[targets.id_field]): item for item in targets.entries}
        authored_by_id = {str(item[targets.id_field]): item for item in visual}
        characters = {str(name) for name in (self.project_json.get("characters") or {})}
        script_scenes = targets.script.get("scenes") if isinstance(targets.script, Mapping) else None
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
                if episode is not None and (gap := self._fragment_gap(entry_id, scene_text, targets, episode)):
                    failures.append((entry_id, f"分镜 {entry_id} 的 image_prompt {gap}"))
                    continue
            if "video_prompt" in fields:
                grid = video_inputs.get(entry_id) if video_inputs is not None else None
                if grid is None:
                    failures.append((entry_id, translate("video_prompt_grid_unconfirmed")))
                    continue
                try:
                    self._validate_video_prompt_body(item, grid)
                except ValueError as exc:
                    failures.append((entry_id, f"分镜 {entry_id} 的 video_prompt 未通过校验：{exc}"))
        return failures

    def _lint_authored_visual_layer(
        self, visual: list[dict], targets: PromptAuthoringTargets, registered: set[str]
    ) -> None:
        """兼容入口：只做 image_prompt 逐 entry 校验，首个失败抛 ``ValueError``。"""
        failures = self._authored_visual_failures(visual, targets, registered, video_inputs=None)
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
                    field: visual_by_id[str(entry[id_field])][field]
                    for field in targets.fields_of(str(entry[id_field]))
                },
            }
            for entry in targets.entries
        ]

    def _stamp_video_prompt_basis(
        self,
        authored_by_id: Mapping[str, dict],
        targets: PromptAuthoringTargets,
        video_inputs: Mapping[str, ShotGrid] | None,
    ) -> None:
        """视频提示词的写回守卫：越界 K 整条拒、有已确认宫格才记依据（D-17 / D-18）。

        模型产出在后台任务里落盘，拿不到请求期的 422；错误在这里抛出会把该任务判失败，
        不落半截文本。视频准入（``storyboard_video_prompt``）在计费前再拦一次同一条规则。
        """

        written = [entry_id for entry_id in targets.ids if "video_prompt" in targets.fields_of(entry_id)]
        if not written:
            return
        for entry_id in written:
            item = authored_by_id[entry_id]
            grid = video_inputs.get(entry_id) if video_inputs is not None else None
            if grid is None:
                raise PromptAuthoringTargetError(translate("video_prompt_grid_unconfirmed"))
            self._format_video_prompt_lines(item)
            self._validate_video_prompt_body(item, grid)
            assets = item.get("generated_assets")
            if not isinstance(assets, dict):
                assets = {}
                item["generated_assets"] = assets
            assets["video_prompt_basis"] = video_prompt_basis_for(grid)

    @staticmethod
    def _format_video_prompt_lines(item: dict[str, Any]) -> None:
        """视频提示词写回前把时间段拆成一行一段：编辑器与视频输入都按行识别时间段。"""

        value = item.get("video_prompt")
        if isinstance(value, dict):
            action = value.get("action")
            if isinstance(action, str):
                value["action"] = format_action_segments(action)
        elif isinstance(value, str):
            item["video_prompt"] = format_action_segments(value)

    @staticmethod
    def _validate_video_prompt_body(item: Mapping[str, Any], grid: ShotGrid) -> None:
        body = video_prompt_body_text(item)
        if _VIDEO_PROMPT_WRAPPER.search(body):
            raise ValueError(translate("video_prompt_body_wrapped"))
        problems = keyframe_reference_problems(body, len(grid.cells))
        if problems:
            raise ValueError(
                translate(
                    "shot_grid_keyframe_out_of_range",
                    ids=grid.shot_id,
                    refs="、".join(problems),
                    count=len(grid.cells),
                )
            )
        if not video_prompt_text(item):
            raise ValueError(translate("video_prompt_action_empty"))

    def _authored_script(
        self,
        episode: int,
        targets: PromptAuthoringTargets,
        authored: list[dict],
        *,
        video_inputs: Mapping[str, ShotGrid] | None = None,
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
        # 视频提示词写回前：越界 K 直接拒，有已确认宫格才记依据（D-17 / D-18）。
        self._stamp_video_prompt_basis(replacements, targets, video_inputs)
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

    def _build_drama_prompt_authoring_prompt(
        self,
        content_scenes: list,
        episode: int,
        instructions: str | None = None,
        *,
        video_inputs: Mapping[str, ShotGrid] | None = None,
    ) -> str:
        """构建 drama prompt_authoring（视觉层）prompt：把 script_plan 内容渲染为输入，仅求本次字段。"""
        scenes_content = render_drama_content_for_prompt_authoring(content_scenes, video_inputs=video_inputs)
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
        for entry in content_scenes:
            if not isinstance(entry, Mapping):
                continue
            for key in wanted:
                values = entry.get(key)
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
            self._video_prompt_inputs(targets, episode)
            _require_overwrite_acknowledged(targets, baseline, overwrite_revision)
        elif self._video_prompt_source({}, episode) is not None:
            raise PromptAuthoringTargetError(translate("video_prompt_storyboard_missing"))

    def _require_previous_prompt(self, targets: PromptAuthoringTargets) -> None:
        """写 image_prompt 时必须等上一条先有提示词：没有邻居可接就拒绝，不静默放行。

        批量重写内部按顺序产出，前一条在同批里已经有了；这里挡的是「跳过上一段、直接写下一段」
        ——那样首镜没有可承接的对象，接不上也查不出来。
        """
        scenes = targets.script.get("scenes") if isinstance(targets.script, Mapping) else None
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
            raise PromptAuthoringTargetError(translate("video_prompt_storyboard_missing"))
        if not targets.entries:
            return _NO_ENTRY_TO_AUTHOR_NOTE
        return await self._build_visual_prompt(
            episode, targets, instructions, video_inputs=self._video_prompt_inputs(targets, episode)
        )

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
