"""模型发现协议（``discovery_format``）名录，以及它与端点定义 ``kind`` 的挂接配对规则。

``discovery_format`` 声明的是「模型发现」与「连通性检查」按哪套接口进行，不决定任何模型的调用
协议——调用形态写在模型行挂接的那个调用端点上。两者因此通常互不相干：同一个 OpenAI 协议供应商
的模型行可以挂任意一个声明式端点。

``comfyui`` 与 ``runninghub`` 是唯一的例外，且是双向的：它们都是 workflow 传输协议，只能挂
``comfyui`` 端点定义（一份 workflow 加节点绑定）。把两者配错在保存期毫无征兆，要等到发起生成
才在传输层露出来，故配对规则落在写入侧（``docs/adr/0081``）。

本模块只有常量与纯谓词，不引任何会拉起运行时的模块——写入校验、容量装载与前端目录三处共读它。
"""

from __future__ import annotations

from arcreel_market_core.endpoint_definition import COMFYUI_KIND

#: ComfyUI 协议：连通性检查裸打 ``/system_stats``，模型发现不适用。
COMFYUI_DISCOVERY_FORMAT = "comfyui"

#: RunningHub 协议：同一份 workflow 定义，改由 RunningHub 的 task OpenAPI 提交与查询。
RUNNINGHUB_DISCOVERY_FORMAT = "runninghub"


def is_comfyui_protocol(discovery_format: str) -> bool:
    """该供应商是否声明了原生 ComfyUI 协议。"""
    return discovery_format == COMFYUI_DISCOVERY_FORMAT


def is_runninghub_protocol(discovery_format: str) -> bool:
    """该供应商是否声明了 RunningHub 协议。"""
    return discovery_format == RUNNINGHUB_DISCOVERY_FORMAT


def is_workflow_protocol(discovery_format: str) -> bool:
    """该供应商是否以 workflow 为调用形态（原生 ComfyUI 或 RunningHub）。"""
    return discovery_format in {COMFYUI_DISCOVERY_FORMAT, RUNNINGHUB_DISCOVERY_FORMAT}


def endpoint_attachment_holds(*, endpoint_kind: str, discovery_format: str) -> bool:
    """一条模型行「端点 × 供应商协议」的挂接是否成立。

    workflow 端点（``comfyui`` kind）只挂 workflow 协议供应商，其余端点只挂非 workflow 协议；
    两个「是不是 workflow」的答案必须一致，写成一个等式而非两条 if，两个方向就不会各自漂移。
    """
    return (endpoint_kind == COMFYUI_KIND) == is_workflow_protocol(discovery_format)
