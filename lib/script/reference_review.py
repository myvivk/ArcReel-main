"""参考素材（K 帧/宫格）评测：只评可确定的结构、帧链、资产与跨镜接缝。

角色一致性、构图美感、面部是否崩坏需要视觉模型，当前没有可用视觉模型，因此不设
“画面一致性”维度；不展示未测项，也不伪造分数。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from difflib import SequenceMatcher
from itertools import pairwise

from lib.script.shot_prompt_standard import (
    handoff_problem,
    reference_tokens,
    seam_prop_problem,
)


def _dimension(dimension_id: str, score: int | None, maximum: int, findings: list[str]) -> dict[str, object]:
    return {"id": dimension_id, "score": score, "max": maximum, "findings": findings}


def review_reference_material(
    *,
    cell_prompts: Sequence[str],
    cell_files_exist: Sequence[bool],
    rows: int,
    cols: int,
    status: str,
    previous_last_cell: str | None,
    registered_names: Collection[str],
    character_names: Collection[str],
    prop_names: Collection[str],
) -> dict[str, object]:
    """返回宫格评测：布局、帧链、资产、接缝四个已测维度 + 视觉未测。"""

    defects: list[dict[str, object]] = []
    advice: list[str] = []
    expected = rows * cols
    has_cells = bool(cell_prompts)
    if not has_cells:
        return {
            "score": 0,
            "max": 100,
            "coverage": 1.0,
            "verdict": "fail",
            "dimensions": [
                _dimension("layout", 0, 20, ["还没有逐格关键帧"]),
                _dimension("chain", 0, 20, ["没有关键帧，无法判断帧链"]),
                _dimension("assets", 0, 20, ["没有关键帧，无法判断资产引用"]),
                _dimension("seam", 0, 20, ["没有关键帧，无法判断跨镜接缝"]),
            ],
            "defects": [{"id": "reference_missing", "severity": "blocker", "message": "还没有逐格关键帧"}],
            "advice": ["先生成并确认参考素材（K 帧/宫格），再做评测。"],
            "status": "reference_missing",
        }

    layout_findings: list[str] = []
    layout = 20
    if len(cell_prompts) != expected:
        layout -= 10
        layout_findings.append(f"{rows}×{cols} 应有 {expected} 格，实际 {len(cell_prompts)} 格")
    if any(not exists for exists in cell_files_exist):
        layout -= 8
        layout_findings.append("有格子没有生成图片文件")
    if status not in {"confirmed", "completed"}:
        layout -= 4
        layout_findings.append(f"宫格状态是 {status}，还不是 confirmed")
    layout = max(0, layout)
    if layout_findings:
        advice.append("先把宫格补齐到 rows×cols，并确认每格图片存在、状态已确认。")

    chain_findings: list[str] = []
    chain = 20
    for left, right in pairwise(cell_prompts):
        ratio = SequenceMatcher(None, left, right, autojunk=False).ratio()
        if ratio >= 0.9:
            chain -= 6
            chain_findings.append(f"相邻两格几乎同一画面（相似度 {ratio:.2f}）")
            defects.append(
                {"id": "cell_duplicate", "severity": "high", "message": f"相邻两格几乎同一画面（{ratio:.2f}）"}
            )
    chain = max(0, chain)
    if chain_findings:
        advice.append("每格必须是单一瞬间，相邻格要有可见姿态/位置变化，不能复制上一格。")

    asset_findings: list[str] = []
    assets = 20
    for index, prompt in enumerate(cell_prompts):
        for name in reference_tokens(prompt):
            if name not in registered_names:
                assets -= 6
                asset_findings.append(f"第{index + 1}格引用了未登记资产「{name}」")
                defects.append(
                    {"id": f"unregistered_{index + 1}", "severity": "high", "message": f"未登记资产：{name}"}
                )
    assets = max(0, assets)
    if asset_findings:
        advice.append("格内 token 只能写登记名；新道具先登记再引用。")

    seam_findings: list[str] = []
    seam = 20
    if previous_last_cell and cell_prompts:
        if problem := handoff_problem(previous_last_cell, cell_prompts[0], character_names=character_names):
            seam -= 8
            seam_findings.append(problem)
            defects.append({"id": "reference_handoff", "severity": "high", "message": problem})
        if problem := seam_prop_problem(previous_last_cell, cell_prompts[0], registered_props=prop_names):
            seam -= 8
            seam_findings.append(problem)
            defects.append({"id": "reference_prop_seam", "severity": "high", "message": problem})
    seam = max(0, seam)
    if seam_findings:
        advice.append("K0 要接住上一镜末格：上一镜末格的人物、手、持物要在 K0 里延续，或写出进入动作。")

    dimensions = [
        _dimension("layout", layout, 20, layout_findings),
        _dimension("chain", chain, 20, chain_findings),
        _dimension("assets", assets, 20, asset_findings),
        _dimension("seam", seam, 20, seam_findings),
    ]
    measured = layout + chain + assets + seam
    score = round(measured * 100 / 80)
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
        "status": "ok" if has_cells else "reference_missing",
    }
