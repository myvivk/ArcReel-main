"""OpenRouterImageBackend — OpenRouter 统一图片接口（单步同步）。

走 ``POST {base}/api/v1/images``：一次请求直接返回 ``data[].b64_json``，没有任务 id，
因而既没有提交 / 轮询两段，也没有可续跑的任务。这与 OpenAI 的 ``/images/generations``
只差路径与几个字段名：比例 / 清晰度用 ``aspect_ratio`` + ``resolution``（显式像素用
``size``），参考图放在 ``input_references`` 里。

路径不能靠 ``ensure_openai_base_url`` 派生：那个函数补的是 ``/v1``，而 OpenRouter 的
图片路由挂在 ``/api/v1`` 下，且末段是 ``/images`` 而不是 ``/images/generations``。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

from lib.backends.artifact_download_guard import IMAGE_ARTIFACT_MAX_BYTES, artifact_http_client
from lib.backends.backend_runtime import should_retry_submit, submit_post
from lib.backends.container_sniff import CONTAINER_HEAD_BYTES, sniff_container
from lib.backends.image_backends.base import (
    ImageCapability,
    ImageCapabilityError,
    ImageGenerationRequest,
    ImageGenerationResult,
    image_to_base64_data_uri,
)
from lib.backends.providers import PROVIDER_OPENROUTER
from lib.infra.logging_utils import format_kwargs_for_log
from lib.infra.retry import with_retry_async

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_API_PATH = "/api/v1"
_IMAGE_PATH = "/images"

#: OpenRouter ``input_references`` 的 maxItems。
MAX_REFERENCE_IMAGES = 16

#: 落盘前认的容器。``output_format`` 已请求 png，但供应商不一定都听，jpeg / webp 一并认；
#: 认不出的字节（HTML 错误页之类）不当图片写盘。
_INLINE_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp"})

#: 分辨率档位 → OpenRouter ``resolution`` 枚举。OpenRouter 用 ``512`` 而不是 ``512px``。
_RESOLUTION_BY_TIER = {"512px": "512", "1k": "1K", "2k": "2K", "4k": "4K"}

#: 自定义 ``宽*高``。与 :mod:`arcreel_market_core.aspect_size` 认的分隔符一致（星号 / 叉号 / x），
#: 但这里要把它改写成 OpenRouter 的 ``WxH`` 形态。
_WH_RE = re.compile(r"^\s*(\d+)\s*[xX×*]\s*(\d+)\s*$")

_HTTP_TIMEOUT_SECONDS = 120.0


def openrouter_base_url(base_url: str | None) -> str:
    """补全 OpenRouter 的挂载路径 ``/api/v1``；已带则原样信任。

    纯域名（无 scheme）先补 ``https://``：自定义供应商的 ``base_url`` 是不受约束的字符串，
    httpx 拒收没有协议的地址。
    """
    stripped = (base_url or "").strip().rstrip("/")
    if not stripped:
        return DEFAULT_BASE_URL
    normalized = stripped if "://" in stripped else f"https://{stripped}"
    if urlsplit(normalized).path.rstrip("/").endswith(_API_PATH):
        return normalized
    return normalized + _API_PATH


def _resolution_for(image_size: str | None) -> str | None:
    """档位词 → ``resolution`` 枚举值；自定义像素或空值返回 ``None``（改走 ``size``）。"""
    if not image_size:
        return None
    return _RESOLUTION_BY_TIER.get(image_size.strip().lower())


def _pixel_size(image_size: str | None) -> str | None:
    """自定义 ``宽*高`` → ``WxH``；档位词与无法解析的值返回 ``None``。"""
    if not image_size:
        return None
    match = _WH_RE.match(image_size)
    return f"{int(match.group(1))}x{int(match.group(2))}" if match else None


def _extract_first_b64(payload: object) -> str | None:
    """取 ``data[].b64_json`` 的首个非空串。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                value = item.get("b64_json")
                if isinstance(value, str) and value:
                    return value
    return None


def _safe_body_for_log(body: dict) -> dict:
    """生成安全日志视图：标量字段照记，prompt 只记长度，素材只记条数。"""
    view: dict = {key: body[key] for key in ("model", "aspect_ratio", "resolution", "size", "n") if key in body}
    if isinstance(body.get("prompt"), str):
        view["prompt_len"] = len(body["prompt"])
    references = body.get("input_references")
    if isinstance(references, list) and references:
        view["input_references"] = f"<{len(references)} ref>"
    return view


class OpenRouterImageBackend:
    """OpenRouter 图像后端（单步同步 ``/api/v1/images`` 端点）。"""

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        base_url: str | None = None,
        http_timeout: float = _HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._api_key = api_key or ""
        self._base_url = openrouter_base_url(base_url)
        self._model = model
        self._http_timeout = http_timeout

    @property
    def name(self) -> str:
        return PROVIDER_OPENROUTER

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> set[ImageCapability]:
        return {ImageCapability.TEXT_TO_IMAGE, ImageCapability.IMAGE_TO_IMAGE}

    @property
    def max_reference_images(self) -> int:
        return MAX_REFERENCE_IMAGES

    async def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        # 生成是一次非幂等的「出图 + 计费」，重试范围严格限定在 _submit 内（见其 docstring）；
        # 解码与落盘本地失败不重跑 POST，避免重复计费。
        payload = self._build_payload(request)
        data = await self._submit(payload)
        encoded = _extract_first_b64(data)
        if not encoded:
            # 响应体可能含 prompt 与签名地址，只记键名与 data 条数，不落整 body。
            body = data if isinstance(data, dict) else {}
            data_items = body.get("data")
            logger.error(
                "OpenRouter 图像响应缺少 b64_json: keys=%s data_count=%s",
                sorted(str(key) for key in body),
                len(data_items) if isinstance(data_items, list) else None,
            )
            raise RuntimeError("OpenRouter 图像响应缺少 b64_json")
        await self._write_image(encoded, request.output_path)
        logger.info("OpenRouter 图片生成完成: %s", request.output_path)
        return ImageGenerationResult(
            image_path=request.output_path,
            provider=self.name,
            model=self._model,
            image_uri=None,
            seed=request.seed,
        )

    def _build_payload(self, request: ImageGenerationRequest) -> dict:
        """按 OpenRouter 的图片请求体组装：比例、清晰度、参考图各归自己的字段。"""
        payload: dict = {
            "model": self._model,
            "prompt": request.prompt,
            "n": 1,
            # 统一要 png：落盘扩展名由调用方定，jpeg 字节写进 .png 会让下游按错的类型去解。
            "output_format": "png",
        }
        if request.aspect_ratio:
            payload["aspect_ratio"] = request.aspect_ratio
        resolution = _resolution_for(request.image_size)
        if resolution is not None:
            payload["resolution"] = resolution
        else:
            size = _pixel_size(request.image_size)
            if size is not None:
                payload["size"] = size
        if request.seed is not None:
            payload["seed"] = request.seed
        references = request.reference_images[:MAX_REFERENCE_IMAGES]
        if references:
            payload["input_references"] = self._build_references(references)
        return payload

    def _build_references(self, references) -> list[dict]:
        """参考图按 data-URI 下发；任一张读不出来即 fail-loud，不静默降级成纯文生图。"""
        items: list[dict] = []
        for index, reference in enumerate(references, start=1):
            path = Path(reference.path) if reference.path else None
            if path is None or not path.is_file():
                raise ImageCapabilityError(
                    "image_reference_images_unreadable",
                    model=self._model,
                    names=path.name if path and path.name else f"#{index}",
                )
            try:
                uri = image_to_base64_data_uri(path)
            except OSError as exc:
                logger.warning("OpenRouter 参考图读取失败: %s (%s)", path, exc)
                raise ImageCapabilityError(
                    "image_reference_images_unreadable", model=self._model, names=path.name
                ) from exc
            items.append({"type": "image_url", "image_url": {"url": uri}})
        return items

    @with_retry_async(retry_if=should_retry_submit)
    async def _submit(self, payload: dict) -> object:
        """单步出图 POST，返回解析后的响应体。

        ``submit_post`` 把歧义传输错误转成终态失败，避免重复计费；>=400 落 body 日志后抛
        ``HTTPStatusError``，交 ``should_retry_submit`` 按状态码分流。
        """
        logger.info(
            "调用 %s 图片 API model=%s body=%s",
            self.name,
            self._model,
            format_kwargs_for_log(_safe_body_for_log(payload)),
        )
        async with artifact_http_client(timeout=self._http_timeout) as client:
            response = await submit_post(
                lambda: client.post(
                    f"{self._base_url}{_IMAGE_PATH}",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                ),
                provider=self.name,
            )
            return response.json()

    async def _write_image(self, encoded: str, output_path: Path) -> None:
        """解码 ``b64_json`` 并落盘；解码与写盘 offload 到线程，免得卡住事件循环。"""

        def decode_and_save() -> None:
            image = _decode_image(encoded)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(image)

        await asyncio.to_thread(decode_and_save)


def _decode_image(encoded: str) -> bytes:
    """解出图片字节：认裸 base64 与 ``data:image/...;base64,`` 两种形态。

    Raises:
        ValueError: 不是合法 base64、解出来为空、超出图片产物体积上限，或文件头不是
            PNG / JPEG / WebP。
    """
    payload = "".join(encoded.strip().split())
    if payload.startswith("data:"):
        _, _, payload = payload.partition(",")
    # 先按编码长度估算体积再解码：超限的串不值得花内存解出来。
    if len(payload) // 4 * 3 > IMAGE_ARTIFACT_MAX_BYTES:
        raise ValueError(f"inline image exceeds {IMAGE_ARTIFACT_MAX_BYTES} bytes")
    try:
        image = base64.b64decode(payload + "=" * (-len(payload) % 4), validate=True)
    except binascii.Error as exc:
        raise ValueError("b64_json is not valid base64") from exc
    if not image:
        raise ValueError("b64_json decoded to an empty image")
    if sniff_container(image[:CONTAINER_HEAD_BYTES]) not in _INLINE_IMAGE_MIMES:
        raise ValueError("b64_json decoded to bytes that are not a PNG, JPEG or WebP image")
    return image
