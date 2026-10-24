"""OpenRouterImageBackend 单元测试（respx 捕获出站请求，单步同步 /api/v1/images 端点）。"""

from __future__ import annotations

import base64
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import respx

from lib.backends.image_backends.base import (
    ImageCapability,
    ImageCapabilityError,
    ImageGenerationRequest,
    ReferenceImage,
)
from lib.backends.image_backends.openrouter import (
    MAX_REFERENCE_IMAGES,
    OpenRouterImageBackend,
    openrouter_base_url,
)
from lib.backends.providers import PROVIDER_OPENROUTER
from lib.custom_provider.endpoints import ENDPOINT_REGISTRY
from tests.http_capture import capture_http, only_request, request_json

_ENDPOINT = "https://openrouter.ai/api/v1/images"
_PNG = b"\x89PNG\r\n\x1a\n"


def _b64_response(raw: bytes) -> httpx.Response:
    return httpx.Response(
        200,
        json={"created": 1, "data": [{"b64_json": base64.b64encode(raw).decode("ascii")}]},
    )


def _make_ref(tmp_path: Path, name: str) -> ReferenceImage:
    p = tmp_path / name
    p.write_bytes(_PNG + b"ref")
    return ReferenceImage(path=str(p))


@contextmanager
def _post(response: httpx.Response) -> Generator[respx.Route]:
    """拦截出图 POST，产出该路由供断言真实请求。"""
    with capture_http() as router:
        yield router.post(_ENDPOINT).mock(return_value=response)


class TestBaseUrl:
    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            (None, "https://openrouter.ai/api/v1"),
            ("", "https://openrouter.ai/api/v1"),
            ("https://openrouter.ai", "https://openrouter.ai/api/v1"),
            ("openrouter.ai", "https://openrouter.ai/api/v1"),
            ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1"),
            ("https://openrouter.ai/api/v1/", "https://openrouter.ai/api/v1"),
        ],
    )
    def test_api_suffix_normalized(self, configured, expected):
        assert openrouter_base_url(configured) == expected

    def test_openai_style_v1_suffix_is_not_trusted(self):
        # /v1 是 OpenAI 的挂载点，OpenRouter 的图片路由在 /api/v1 下——不能把它当成已带前缀。
        assert openrouter_base_url("https://openrouter.ai/v1") == "https://openrouter.ai/v1/api/v1"


class TestCapabilities:
    def test_t2i_and_i2i(self):
        backend = OpenRouterImageBackend(api_key="sk", model="openai/gpt-image-2.5-sunburst")
        assert backend.name == PROVIDER_OPENROUTER
        assert backend.model == "openai/gpt-image-2.5-sunburst"
        assert backend.capabilities == {ImageCapability.TEXT_TO_IMAGE, ImageCapability.IMAGE_TO_IMAGE}

    def test_reference_limit_matches_api_max_items(self):
        backend = OpenRouterImageBackend(api_key="sk", model="m")
        assert backend.max_reference_images == MAX_REFERENCE_IMAGES == 16

    def test_registered_as_builtin_endpoint(self):
        spec = ENDPOINT_REGISTRY["openrouter-images"]
        assert spec.media_type == "image"
        assert spec.request_path_template == "/api/v1/images"
        assert spec.image_capabilities == frozenset({ImageCapability.TEXT_TO_IMAGE, ImageCapability.IMAGE_TO_IMAGE})

    def test_endpoint_builder_normalizes_base_url(self):
        provider = type(
            "Provider",
            (),
            {"provider_id": "custom-openrouter", "api_key": "sk", "base_url": "https://openrouter.ai"},
        )()
        backend = ENDPOINT_REGISTRY["openrouter-images"].build_backend(provider, "openai/gpt-image-2.5")
        # 包装层把记账身份换成 provider_id；路径归一位于下文的真实请求断言。
        assert backend.name == "custom-openrouter"
        assert backend.capabilities == {ImageCapability.TEXT_TO_IMAGE, ImageCapability.IMAGE_TO_IMAGE}


class TestTextToImage:
    async def test_request_shape(self, tmp_path: Path):
        with _post(_b64_response(_PNG + b"img")) as route:
            backend = OpenRouterImageBackend(api_key="sk", model="openai/gpt-image-2.5-sunburst")
            result = await backend.generate(
                ImageGenerationRequest(
                    prompt="a red panda",
                    output_path=tmp_path / "o.png",
                    aspect_ratio="16:9",
                    image_size="2K",
                )
            )

        request = only_request(route)
        assert str(request.url) == _ENDPOINT
        assert request.headers["Authorization"] == "Bearer sk"
        assert request_json(request) == {
            "model": "openai/gpt-image-2.5-sunburst",
            "prompt": "a red panda",
            "n": 1,
            "output_format": "png",
            "aspect_ratio": "16:9",
            "resolution": "2K",
        }
        assert result.image_path == tmp_path / "o.png"
        assert result.provider == PROVIDER_OPENROUTER
        # 单步同步响应只有内联 base64，没有可回指的远端地址
        assert result.image_uri is None

    async def test_custom_pixel_size_uses_size_field(self, tmp_path: Path):
        with _post(_b64_response(_PNG)) as route:
            backend = OpenRouterImageBackend(api_key="sk", model="m")
            await backend.generate(
                ImageGenerationRequest(
                    prompt="x",
                    output_path=tmp_path / "o.png",
                    aspect_ratio="9:16",
                    image_size="1920*1080",
                )
            )

        body = request_json(only_request(route))
        assert body["size"] == "1920x1080"
        assert "resolution" not in body

    async def test_no_resolution_when_unset(self, tmp_path: Path):
        with _post(_b64_response(_PNG)) as route:
            backend = OpenRouterImageBackend(api_key="sk", model="m")
            await backend.generate(ImageGenerationRequest(prompt="x", output_path=tmp_path / "o.png"))

        body = request_json(only_request(route))
        assert "resolution" not in body
        assert "size" not in body
        assert body["aspect_ratio"] == "9:16"


class TestImageToImage:
    async def test_reference_images_as_input_references(self, tmp_path: Path):
        refs = [_make_ref(tmp_path, f"r{i}.png") for i in range(2)]
        with _post(_b64_response(_PNG)) as route:
            backend = OpenRouterImageBackend(api_key="sk", model="m")
            await backend.generate(
                ImageGenerationRequest(prompt="hero", output_path=tmp_path / "o.png", reference_images=refs)
            )

        body = request_json(only_request(route))
        items = body["input_references"]
        assert len(items) == 2
        assert all(item["type"] == "image_url" for item in items)
        assert all(item["image_url"]["url"].startswith("data:image/png;base64,") for item in items)

    async def test_references_clipped_to_api_limit(self, tmp_path: Path):
        refs = [_make_ref(tmp_path, f"r{i}.png") for i in range(MAX_REFERENCE_IMAGES + 2)]
        with _post(_b64_response(_PNG)) as route:
            backend = OpenRouterImageBackend(api_key="sk", model="m")
            await backend.generate(
                ImageGenerationRequest(prompt="p", output_path=tmp_path / "o.png", reference_images=refs)
            )

        assert len(request_json(only_request(route))["input_references"]) == MAX_REFERENCE_IMAGES

    async def test_missing_reference_fails_loud(self, tmp_path: Path):
        backend = OpenRouterImageBackend(api_key="sk", model="m")
        with pytest.raises(ImageCapabilityError) as ei:
            await backend.generate(
                ImageGenerationRequest(
                    prompt="p",
                    output_path=tmp_path / "o.png",
                    reference_images=[ReferenceImage(path=str(tmp_path / "nope.png"))],
                )
            )
        assert ei.value.code == "image_reference_images_unreadable"


class TestResponseHandling:
    async def test_base64_written_to_disk(self, tmp_path: Path):
        out = tmp_path / "o.png"
        with _post(_b64_response(_PNG + b"hello")):
            backend = OpenRouterImageBackend(api_key="sk", model="m")
            await backend.generate(ImageGenerationRequest(prompt="x", output_path=out))

        assert out.read_bytes() == _PNG + b"hello"

    async def test_data_uri_prefix_tolerated(self, tmp_path: Path):
        encoded = "data:image/png;base64," + base64.b64encode(_PNG + b"x").decode("ascii")
        out = tmp_path / "o.png"
        with _post(httpx.Response(200, json={"created": 1, "data": [{"b64_json": encoded}]})):
            backend = OpenRouterImageBackend(api_key="sk", model="m")
            await backend.generate(ImageGenerationRequest(prompt="x", output_path=out))

        assert out.read_bytes() == _PNG + b"x"

    async def test_missing_b64_raises_without_echoing_body(self, tmp_path: Path, caplog):
        response = httpx.Response(
            200,
            json={"created": 1, "data": [{"media_type": "image/png"}], "signed_url": "https://x/secret?sig=abc"},
        )
        with _post(response), caplog.at_level("ERROR"):
            backend = OpenRouterImageBackend(api_key="sk", model="m")
            with pytest.raises(RuntimeError):
                await backend.generate(ImageGenerationRequest(prompt="x", output_path=tmp_path / "o.png"))

        assert "sig=abc" not in caplog.text
        assert "signed_url" in caplog.text

    async def test_non_image_bytes_rejected(self, tmp_path: Path):
        out = tmp_path / "o.png"
        with _post(_b64_response(b"<html>not an image</html>")):
            backend = OpenRouterImageBackend(api_key="sk", model="m")
            with pytest.raises(ValueError, match="PNG, JPEG or WebP"):
                await backend.generate(ImageGenerationRequest(prompt="x", output_path=out))

        assert not out.exists()


class TestHttpErrors:
    async def test_400_surfaces_single_call(self, tmp_path: Path):
        with _post(httpx.Response(400, text="boom")) as route:
            backend = OpenRouterImageBackend(api_key="sk", model="m")
            with pytest.raises(httpx.HTTPStatusError) as ei:
                await backend.generate(ImageGenerationRequest(prompt="p", output_path=tmp_path / "o.png"))

        assert ei.value.response.status_code == 400
        assert route.call_count == 1
