"""从 ComfyUI / RunningHub 工作流结构检出提示词规范。

自定义端点的模型名是用户随手起的别名（``参考图生视频``、``rh-video-1``），按名字匹配规范
必然落空，落空就退回 ``generic``——而 H3 这类本地权重模型的提示词格式与通用模板差得很远，
渲染错的直接后果是模型自己编台词。工作流本身带节点类型，它才是「这份提示词要喂给谁」的
可靠事实来源，所以规范从节点类型检出，写进执行 lane，由渲染出口使用。

只做字符串判定，不读文件、不碰网络。
"""

from __future__ import annotations

from collections.abc import Mapping

#: 官方 / T8 三方节点统一以 ``MiniMaxH3`` 开头（``MiniMaxH3ReferenceToVideo``、
#: ``MiniMaxH3AudioConditioningT8``、``MiniMaxH3ImageToVideo`` 等）。
_H3_NODE_PREFIX = "minimaxh3"

#: 节点类型前缀 → 规范键。
_SPEC_BY_NODE_PREFIX: tuple[tuple[str, str], ...] = ((_H3_NODE_PREFIX, "minimax-h3-local"),)


def prompt_spec_for_workflow(workflow: object) -> str | None:
    """按工作流节点类型返回规范键；检不出时 None（调用方按模型名继续匹配）。"""

    if not isinstance(workflow, Mapping):
        return None
    for node in workflow.values():
        if not isinstance(node, Mapping):
            continue
        class_type = node.get("class_type")
        if not isinstance(class_type, str):
            continue
        lowered = class_type.strip().casefold()
        for prefix, spec_key in _SPEC_BY_NODE_PREFIX:
            if lowered.startswith(prefix):
                return spec_key
    return None


def prompt_spec_for_definition(definition: object) -> str | None:
    """端点定义（``comfyui`` 形状）→ 规范键；定义缺失或媒体类型不是视频时 None。"""

    if not isinstance(definition, Mapping):
        return None
    if str(definition.get("media_type") or "").strip() != "video":
        return None
    return prompt_spec_for_workflow(definition.get("workflow"))


__all__ = ["prompt_spec_for_definition", "prompt_spec_for_workflow"]
