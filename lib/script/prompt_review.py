"""分镜提示词评分：把标准分镜 lint 拆成可解释的维度，不改变校验口径。

分数是评测，不是新的硬闸：真正的阻断仍由 ``lint_shot_prompt`` / ``handoff_problem`` 决定。
同一份失败原因在这里同时生成 finding、建议与 defect，供只读面板展示。
"""

from __future__ import annotations

import re
from collections.abc import Collection

from lib.script.shot_prompt_standard import (
    BLOCK_MIN_CHARS,
    block_diagnostics,
    dialogue_line_problem,
    handoff_problem,
    lint_shot_prompt,
    seam_prop_problem,
    shot_blocks,
)

_SOUND = re.compile("唱报|宣读|喊声|画外音|听见|唱礼|对白|台词|旁白")


def _dimension(
    dimension_id: str,
    score: int,
    maximum: int,
    findings: list[str],
    advice: list[str],
) -> dict[str, object]:
    return {"id": dimension_id, "score": score, "max": maximum, "findings": findings, "advice": advice}


def review_storyboard_prompt(
    scene_text: str,
    *,
    duration_seconds: int | None,
    registered_names: Collection[str],
    character_names: Collection[str],
    registered_props: Collection[str],
    previous_text: str | None = None,
) -> dict[str, object]:
    """给一条分镜提示词打分；返回总评分、六维明细、缺陷与建议。"""

    blocks = shot_blocks(scene_text)
    if not blocks:
        return {
            "score": 0,
            "max": 100,
            "coverage": 1.0,
            "verdict": "fail",
            "dimensions": [
                _dimension("format", 0, 20, ["没有任何【镜头N｜景别】块"], ["先写至少一个镜头块"]),
                _dimension("richness", 0, 20, ["没有可评的镜头正文"], ["补主体动作、神态与环境"]),
                _dimension("continuity", 0, 20, ["没有镜头，无法承接"], ["先建立第一个镜头"]),
                _dimension("assets", 0, 20, ["没有资产引用"], ["用登记名 + 场景 token"]),
                _dimension("boundary", 0, 10, ["没有正文"], ["只写画面，不写台词/声音"]),
                _dimension("duration", 0, 10, ["没有镜头，无法配时长"], ["按 2–6 秒/镜头拆"]),
            ],
            "defects": [{"id": "empty_storyboard", "severity": "blocker", "message": "分镜提示词为空"}],
            "advice": ["先写至少一个【镜头1｜景别】块，再补主体、环境与光线。"],
        }

    lint = lint_shot_prompt(
        scene_text,
        registered_names=registered_names,
        allowed_names=registered_names,
        character_names=character_names,
    )
    diagnostics = block_diagnostics(scene_text, character_names=character_names)
    defects: list[dict[str, object]] = []
    advice: list[str] = []

    format_findings = [lint] if lint else []
    format_score = 20 if not lint else 10
    if lint:
        defects.append({"id": "storyboard_lint", "severity": "high", "message": lint})
        advice.append("先把分镜 lint 报出的格式问题改掉，再谈画面丰满度。")

    richness_score = 20
    richness_findings: list[str] = []
    for diagnostic in diagnostics:
        number = diagnostic["number"]
        chars = diagnostic["chars"] if isinstance(diagnostic["chars"], int) else 0
        if chars < BLOCK_MIN_CHARS:
            richness_score -= 4
            message = f"镜头{number} 只有 {chars} 字，低于 {BLOCK_MIN_CHARS} 字下限"
            richness_findings.append(message)
            defects.append({"id": f"short_block_{number}", "severity": "medium", "message": message})
        if not diagnostic["has_action"]:
            richness_score -= 4
            message = f"镜头{number} 没有具体动作"
            richness_findings.append(message)
            defects.append({"id": f"no_action_{number}", "severity": "medium", "message": message})
        if not diagnostic["has_emotion"]:
            richness_score -= 4
            message = f"镜头{number} 缺具体神态/情绪（空镜需写光影或环境在动）"
            richness_findings.append(message)
            defects.append({"id": f"no_emotion_{number}", "severity": "medium", "message": message})
        if diagnostic["placeholder"]:
            richness_score -= 4
            message = f"镜头{number} 含空泛占位描写"
            richness_findings.append(message)
            defects.append({"id": f"placeholder_{number}", "severity": "medium", "message": message})
    richness_score = max(0, richness_score)
    if richness_findings:
        advice.append("每个镜头块补到一个主动作 + 一个具体神态，并用环境/光线收尾。")

    continuity_findings: list[str] = []
    continuity_score = 20
    if lint and "未写承接" in lint:
        continuity_score -= 4
        continuity_findings.append("镜头之间缺承接词或位移动作")
    if previous_text:
        if problem := handoff_problem(previous_text, scene_text, character_names=character_names):
            continuity_score -= 8
            continuity_findings.append(problem)
            defects.append({"id": "handoff", "severity": "high", "message": problem})
        if problem := seam_prop_problem(previous_text, scene_text, registered_props=registered_props):
            continuity_score -= 8
            continuity_findings.append(problem)
            defects.append({"id": "prop_seam", "severity": "high", "message": problem})
    continuity_score = max(0, continuity_score)
    if continuity_findings:
        advice.append("下一镜首镜先复述上一镜末镜的人物、手、持物；新道具写出托入/接过/放下。")

    assets_findings: list[str] = []
    assets_score = 20
    if lint and any(word in lint for word in ("token", "场景", "未登记", "清单")):
        assets_score = 8
        assets_findings.append(lint)
        advice.append("token 只能写登记名；场景用「场景名@[登记场景名]」指认。")

    boundary_findings: list[str] = []
    boundary_score = 10
    if problem := dialogue_line_problem(scene_text):
        boundary_score = 0
        boundary_findings.append(problem)
        defects.append({"id": "dialogue_in_image", "severity": "high", "message": problem})
    if _SOUND.search(scene_text):
        boundary_score = max(0, boundary_score - 5)
        boundary_findings.append("画面提示词里出现了声音词；声音应由 utterances / ambiance_audio 承载")
        defects.append({"id": "sound_in_image", "severity": "medium", "message": boundary_findings[-1]})
    if boundary_findings:
        advice.append("画面只写看得见的内容；台词和音效移到 utterances / ambiance_audio。")

    duration_findings: list[str] = []
    duration_score = 10
    seconds = int(duration_seconds or 0)
    shot_count = len(blocks)
    if seconds <= 0 or not (shot_count * 2 <= seconds <= shot_count * 6):
        duration_score = 5
        duration_findings.append(f"{shot_count} 个镜头配 {seconds or '未知'} 秒，不满足每镜 2–6 秒")
        defects.append({"id": "duration_fit", "severity": "low", "message": duration_findings[-1]})

    dimensions = [
        _dimension("format", format_score, 20, format_findings, []),
        _dimension("richness", richness_score, 20, richness_findings, []),
        _dimension("continuity", continuity_score, 20, continuity_findings, []),
        _dimension("assets", assets_score, 20, assets_findings, []),
        _dimension("boundary", boundary_score, 10, boundary_findings, []),
        _dimension("duration", duration_score, 10, duration_findings, []),
    ]
    score = sum(dimension["score"] for dimension in dimensions if isinstance(dimension["score"], int))
    has_high = any(defect["severity"] in {"blocker", "high"} for defect in defects)
    verdict = "fail" if has_high or score < 60 else "warn" if score < 85 else "pass"
    return {
        "score": score,
        "max": 100,
        "coverage": 1.0,
        "verdict": verdict,
        "dimensions": dimensions,
        "defects": defects,
        "advice": advice,
    }
