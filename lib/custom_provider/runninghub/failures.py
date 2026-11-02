"""RunningHub 通道的失败载体与失败码。

与 ComfyUI 通道同形：一个 ``code`` + 可序列化 ``params`` 贯穿提交、轮询与取件，落库后由
``lib.generation.task_failure`` 按语言渲染。code 必须登记进 ``FAILURE_CODE_KEYS`` 与
``_TASK_FAILURE_ACTIONS``，否则 worker 编码失败原因时会降级成裸文本。
"""

from __future__ import annotations

from typing import Any

#: APIKEY 无效 / 过期 / 无权限。
AUTH_FAILED = "runninghub_auth_failed"

#: workflowId 不存在，或当前 APIKEY 无权访问。
WORKFLOW_NOT_FOUND = "runninghub_workflow_not_found"

#: RH 币不足，任务未提交。
INSUFFICIENT_COINS = "runninghub_insufficient_coins"

#: 上游明确拒绝（HTTP 4xx 或业务码非 0），且不属于上面几类。
TASK_FAILED = "runninghub_task_failed"

#: 素材上传失败：地址、凭据或磁盘的瞬态问题，重发同一请求可能就好了。
UPLOAD_FAILED = "runninghub_upload_failed"

#: 提交成功后任务在 RunningHub 上找不着了。
JOB_LOST = "runninghub_job_lost"

#: 执行到了终态，但结果里没有这个媒体类型的产物。
OUTPUT_MISSING = "runninghub_output_missing"

#: 产物扩展名不是这个端点应产的那一类。
OUTPUT_TYPE_MISMATCH = "runninghub_output_type_mismatch"

#: 产物的扩展名对得上，但落盘字节的文件头不是这个媒体类型的容器。
OUTPUT_CONTAINER_MISMATCH = "runninghub_output_container_mismatch"

#: 提交时 workflow 节点校验失败（``promptTips.node_errors``）。
NODE_ERRORS = "runninghub_node_errors"


class RunningHubError(RuntimeError):
    """RunningHub 通道失败，携带可持久化、可本地化的稳定失败码。"""

    def __init__(self, code: str, **params: Any) -> None:
        self.code = code
        self.params: dict[str, Any] = params
        super().__init__(code)
