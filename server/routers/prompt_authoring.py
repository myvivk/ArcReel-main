"""提示词编写的 Web 入口。

「AI 编写 / AI 重写」与 Agent 的 ``generate_episode_script`` 调用同一个服务命令：补缺与显式重写的
对象选择、覆盖确认与草稿落盘都在服务内，路由只负责把结果映射成 HTTP。提交后立即返回生成批次，
任务进度经任务事件呈现。附加指令按集保存，下次打开弹窗时预填。
"""

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from lib.episode.episode_paths import episode_script_filename
from lib.infra.api_errors import ConflictError, UnprocessableError
from lib.project.project_manager import get_project_manager
from lib.script.grid.keyframe_parser import keyframe_reference_problems
from lib.script.grid.shot_grid_manager import ShotGridManager, select_shot_grids
from lib.script.grid.video_prompt_targets import video_prompt_body_text
from lib.script.prompt_authoring_scope import prompt_overwrite_with_text
from lib.script.storyboard_sequence import get_storyboard_items
from server.agent_toolset.envelope import json_value
from server.auth import CurrentUser
from server.i18n import Translator
from server.services.project.episode_instructions import EpisodeInstructionKind, save_episode_instructions
from server.text_generation import MAX_INSTRUCTIONS_LEN
from server.tool_runtime import (
    CallerContext,
    GenerateEpisodeScriptRequest,
    ProjectScope,
    Services,
    ToolProblem,
    ToolRequest,
    generate_episode_script,
)

router = APIRouter()

_Instructions = Annotated[str, Field(max_length=MAX_INSTRUCTIONS_LEN)]
_PromptField = Literal["image_prompt", "video_prompt"]


class PromptAuthoringInstructions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instructions: _Instructions | None = Field(default=None, description="附加指令原文；空白视同清除")


class AuthorPromptsRequest(PromptAuthoringInstructions):
    entry_ids: list[Annotated[str, Field(min_length=1)]] | None = Field(
        default=None, min_length=1, description="编写范围（分镜 / 单元 id）；省略时为全部待编写条目，给出时至少一条"
    )
    rewrite: bool = Field(
        default=False,
        description=(
            "显式重写范围内条目的本次提示词字段；省略时补缺。"
            "分镜类必须与 fields 同用：省略 fields 时本次写的是 image_prompt，单独给 rewrite 会被拒绝"
        ),
    )
    fields: list[_PromptField] | None = Field(
        default=None,
        min_length=1,
        description="仅编写指定提示词字段；省略时默认写分镜提示词，视频提示词须在参考素材确认后单独请求",
    )
    overwrite_revision: str | None = Field(
        default=None, description="认可覆盖的视觉制作状态版本，取自 prompt_overwrite.revision"
    )

    @model_validator(mode="after")
    def _single_prompt_field(self) -> "AuthorPromptsRequest":
        if self.fields is not None and {"image_prompt", "video_prompt"} <= set(self.fields):
            raise ValueError("image_prompt 和 video_prompt 不能在同一次提示词编写中同时生成")
        return self


def _raise_problem(problem: ToolProblem, episode: int, _t: Translator) -> None:
    params = problem.params or {}
    if problem.code == "prompt_overwrite_required":
        overwrite = {key: value for key, value in params["prompt_overwrite"].items() if key != "text"}
        raise ConflictError("prompt_overwrite_required").with_diagnostic(
            {"prompt_overwrite": prompt_overwrite_with_text(overwrite, _t)}
        )
    if problem.code == "generation_active_task_conflict":
        raise ConflictError("prompt_authoring_task_active").with_diagnostic(params)
    reason = params.get("reason")
    if problem.code == "operation_not_admitted" and isinstance(reason, str):
        text = _t(f"operation_{reason}", where=_t("operation_episode", episode=episode))
    else:
        text = problem.detail
    raise UnprocessableError("prompt_authoring_refused", reason=text).with_diagnostic(problem.detail)


def _reject_out_of_range_keyframes(project_name: str, episode: int, req: "AuthorPromptsRequest", t: Translator) -> None:
    """写视频提示词前先答「这次会不会失败」：已有正文里越界的 K 直接 422（D-17）。

    补缺模式不重写已有正文，但同一分镜的旧文本越界会让整条写回被拒、整集任务白跑一遍。
    重写模式会覆盖旧正文，不做这项前置检查。
    """

    if req.fields != ["video_prompt"] or req.rewrite:
        return
    pm = get_project_manager()
    try:
        script = pm.load_script(project_name, episode_script_filename(episode))
    except (FileNotFoundError, ValueError):
        # 缺少视觉制作状态时由常规准入拒绝。
        return
    items, id_field, _, _, _ = get_storyboard_items(script)
    wanted = set(req.entry_ids or [])
    grids = select_shot_grids(
        (
            grid
            for grid in ShotGridManager(pm.get_project_path(project_name)).list_all()
            if grid.episode == episode and grid.status == "confirmed"
        ),
        items=items,
        id_field=id_field,
    )
    for item in items:
        unit_id = str(item.get(id_field, ""))
        if wanted and unit_id not in wanted:
            continue
        grid = grids.get(unit_id)
        text = video_prompt_body_text(item)
        if grid is None or not text:
            continue
        problems = keyframe_reference_problems(text, len(grid.cells))
        if problems:
            raise UnprocessableError(
                "prompt_authoring_refused",
                reason=t(
                    "shot_grid_keyframe_out_of_range",
                    ids=unit_id,
                    refs="、".join(problems),
                    count=len(grid.cells),
                ),
            ).with_diagnostic({"entry_id": unit_id, "references": problems, "cell_count": len(grid.cells)})


@router.put("/projects/{project_name}/episodes/{episode}/prompt-authoring/instructions")
async def save_prompt_authoring_instructions(project_name: str, episode: int, req: PromptAuthoringInstructions):
    """只保存本集的附加指令（「交给 Agent」路径用），不提交生成。"""
    await asyncio.to_thread(
        save_episode_instructions,
        get_project_manager(),
        project_name,
        episode,
        EpisodeInstructionKind.PROMPT_AUTHORING,
        req.instructions,
    )
    return {"success": True}


@router.post("/projects/{project_name}/episodes/{episode}/prompt-authoring")
async def author_prompts(
    project_name: str,
    episode: int,
    req: AuthorPromptsRequest,
    user: CurrentUser,
    _t: Translator,
):
    """保存附加指令并提交提示词编写，返回生成批次。

    显式重写会替换已有视觉层内容而未带（或带了过期的）``overwrite_revision`` 时 409，诊断里的
    ``prompt_overwrite`` 列出将被覆盖的条目与字段、渲染好的 ``text`` 和认可令牌 ``revision``，不提交任务。
    """
    pm = get_project_manager()
    await asyncio.to_thread(
        save_episode_instructions,
        pm,
        project_name,
        episode,
        EpisodeInstructionKind.PROMPT_AUTHORING,
        req.instructions,
    )
    await asyncio.to_thread(_reject_out_of_range_keyframes, project_name, episode, req, _t)
    outcome = await generate_episode_script(
        ToolRequest(
            GenerateEpisodeScriptRequest(
                episode_id=episode,
                instructions=req.instructions,
                entry_ids=req.entry_ids,
                fields=req.fields,
                rewrite=req.rewrite,
                overwrite_revision=req.overwrite_revision,
            )
        ),
        ProjectScope(project_name=project_name, data_root=pm.data_root),
        CallerContext(user_id=user.id, source="webui"),
        Services.defaults(pm),
    )
    if outcome.problem is not None:
        _raise_problem(outcome.problem, episode, _t)
    return {"batch": json_value(outcome.value)}
