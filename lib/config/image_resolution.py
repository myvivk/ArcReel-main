"""T2I image resolution used for price estimates."""

from __future__ import annotations

import logging

from lib.config.resolver import ConfigResolver

logger = logging.getLogger(__name__)


async def resolve_image_resolution(r: ConfigResolver, project: dict) -> str | None:
    """项目 T2I 槽生效的图像分辨率档；``None`` 表示未配置（由调用方按各自保底档处理）。

    以 T2I 槽解析身份：``execute_grid_task`` 在有参考图时走 I2I 槽，两槽解析到不同型号时门控与
    实际渲染可能取到不同档位——门控须在入队时就定档，那时还没有参考图收集结果，取正交能力槽中
    代表宫格主路径的 T2I。这里取 registry 身份 ``model_id`` 而非构造后的 backend 身份，避免为
    一次门控判定构造 backend；执行侧按 backend 身份取档，两者在型号身份分叉的供应商上可能落到
    不同档位。

    解析失败（未配置图像供应商等）按未配置处理：门控是收紧方向，取不到档位即不放行大宫格。
    这条降级会让已配置 4K 的项目静默退回 3×3，故留一条 warning 便于排查。
    """
    try:
        resolved = await r.resolve_image_backend(project, None, generation_type="t2i")
        return await r.resolve_resolution(project, resolved.provider_id, resolved.model_id or "")
    except Exception as exc:
        logger.warning("取不到 T2I 图像分辨率档，宫格门控按未配置收紧、计价按保底档: %s", exc)
        return None


__all__ = ["resolve_image_resolution"]
