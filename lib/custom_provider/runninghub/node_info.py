"""把「实发 workflow」与底稿的差异压成 RunningHub 的 ``nodeInfoList``。

RunningHub 的 create 接口同时接受完整 ``workflow`` 与逐字段的 ``nodeInfoList``：前者保证节点
裁剪 / 旁路这类结构变化原样生效，后者是官方文档描述的主用法。实现两样都发——完整 workflow 让
远端按本地实发稿执行，nodeInfoList 兜住 RunningHub 会强制覆盖的字段（尤其是 seed）。

差异只收标量：链接（``[node_id, output_index]``）与对象不是可安全回写的字段值；数组字面量在
ComfyUI 里也可能被误读成链接，故一律跳过。这一层不解释语义，语义已经在 ``build_workflow``
里写完。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from arcreel_market_core.comfyui.bindings import targets_of
from lib.custom_provider.comfyui.request_builder import BuiltWorkflow


def build_node_info_list(
    definition: Mapping[str, Any],
    built: BuiltWorkflow,
) -> list[dict[str, Any]]:
    """返回 ``[{nodeId, fieldName, fieldValue}]``，类型保持原值。"""
    base: Mapping[str, Any] = definition.get("workflow") or {}
    final: Mapping[str, Any] = built.workflow
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for node_id, node in final.items():
        if not isinstance(node, Mapping):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, Mapping):
            continue
        base_node = base.get(node_id)
        base_inputs = base_node.get("inputs") if isinstance(base_node, Mapping) else None
        if not isinstance(base_inputs, Mapping):
            base_inputs = {}
        for field_name, value in inputs.items():
            if not isinstance(field_name, str) or isinstance(value, (list, dict)):
                continue
            if base_inputs.get(field_name) == value:
                continue
            entries.append(
                {
                    "nodeId": str(node_id),
                    "fieldName": field_name,
                    "fieldValue": value,
                }
            )
            seen.add((str(node_id), field_name))

    # RunningHub 会强制重置 seed；请求里带 seed 时即使与底稿相同也要显式写回。
    if built.seed is not None:
        bindings = definition.get("bindings") or {}
        for target in targets_of(bindings.get("seed")):
            node_id = str(target.get("node") or "")
            field_name = target.get("input")
            if not node_id or not isinstance(field_name, str):
                continue
            if (node_id, field_name) in seen:
                continue
            node = final.get(node_id)
            value = node.get("inputs", {}).get(field_name) if isinstance(node, Mapping) else None
            if value is None or isinstance(value, (list, dict)):
                continue
            entries.append({"nodeId": node_id, "fieldName": field_name, "fieldValue": value})
            seen.add((node_id, field_name))

    return entries
