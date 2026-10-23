"""Tests for projects_creation_fields."""

import asyncio

import pytest

from lib.config.service import ConfigService
from tests.integration.server.routers.projects_router_support import (
    _FakePM,
    build_projects_client,
)


class TestProjectsRouter:
    def test_create_project_with_style_template_id_expands_prompt(self, tmp_path, monkeypatch):
        fake_pm = _FakePM(tmp_path)
        client = build_projects_client(monkeypatch, fake_pm)

        with client:
            resp = client.post(
                "/api/v1/projects",
                json={
                    "generation_mode": "storyboard",
                    "title": "模版项目",
                    "name": "tpl-1",
                    "style_template_id": "live_premium_drama",
                    "content_mode": "drama",
                    "aspect_ratio": "9:16",
                },
            )
            assert resp.status_code == 200
            data = fake_pm.project_data["tpl-1"]
            assert data["style_template_id"] == "live_premium_drama"
            assert "真人电视剧" in data["style"] or "精品短剧" in data["style"]

    def test_create_project_with_unknown_template_id_returns_400(self, tmp_path, monkeypatch):
        fake_pm = _FakePM(tmp_path)
        client = build_projects_client(monkeypatch, fake_pm)

        with client:
            resp = client.post(
                "/api/v1/projects",
                json={
                    "generation_mode": "storyboard",
                    "title": "坏模版",
                    "name": "bad-1",
                    "style_template_id": "no_such",
                },
            )
            assert resp.status_code == 400

    def test_create_project_with_model_fields_persists(self, tmp_path, monkeypatch):
        fake_pm = _FakePM(tmp_path)
        client = build_projects_client(monkeypatch, fake_pm)

        with client:
            resp = client.post(
                "/api/v1/projects",
                json={
                    "generation_mode": "storyboard",
                    "title": "模型项目",
                    "name": "m-1",
                    "video_backend": "gemini-aistudio/veo-3",
                    "image_provider_t2i": "gemini-aistudio/nano-banana",
                    "text_backend_simple": "gemini-aistudio/gemini-2.5",
                    "text_backend_complex": "gemini-aistudio/gemini-2.5-pro",
                    "default_text_backend": "gemini-aistudio/gemini-2.5",
                    "default_duration": 8,
                },
            )
            assert resp.status_code == 200
            data = fake_pm.project_data["m-1"]
            assert data["video_backend"] == "gemini-aistudio/veo-3"
            assert data["image_provider_t2i"] == "gemini-aistudio/nano-banana"
            assert data["text_backend_simple"] == "gemini-aistudio/gemini-2.5"
            assert data["text_backend_complex"] == "gemini-aistudio/gemini-2.5-pro"
            assert data["default_text_backend"] == "gemini-aistudio/gemini-2.5"
            assert data["default_duration"] == 8

    def test_create_project_persists_episode_target_duration(self, tmp_path, monkeypatch):
        """单集目标时长在创建向导写入；越界值 422 且不留下半成品项目。"""
        fake_pm = _FakePM(tmp_path)
        client = build_projects_client(monkeypatch, fake_pm)

        with client:
            resp = client.post(
                "/api/v1/projects",
                json={
                    "generation_mode": "storyboard",
                    "name": "etd-ok",
                    "title": "目标时长",
                    "episode_target_duration": 120,
                },
            )
            assert resp.status_code == 200
            assert fake_pm.project_data["etd-ok"]["episode_target_duration"] == 120

            rejected = client.post(
                "/api/v1/projects",
                json={
                    "generation_mode": "storyboard",
                    "name": "etd-bad",
                    "title": "越界",
                    "episode_target_duration": 5,
                },
            )
            assert rejected.status_code == 422
            assert "etd-bad" not in fake_pm.project_data

    def test_create_project_with_image_default_layer(self, tmp_path, monkeypatch):
        """项目默认图片模型（default_image_backend）可在创建时写入，不必配桶。"""
        fake_pm = _FakePM(tmp_path)
        client = build_projects_client(monkeypatch, fake_pm)

        with client:
            resp = client.post(
                "/api/v1/projects",
                json={
                    "generation_mode": "storyboard",
                    "title": "只配默认",
                    "name": "img-default",
                    "default_image_backend": "gemini-aistudio/nano-banana",
                },
            )
            assert resp.status_code == 200
            data = fake_pm.project_data["img-default"]
            assert data["default_image_backend"] == "gemini-aistudio/nano-banana"
            assert "image_provider_t2i" not in data
            assert "image_provider_i2i" not in data

    def test_video_bucket_fields_create_patch_and_clear(self, tmp_path, monkeypatch):
        """项目级视频桶键（video_provider_i2v/r2v）可创建时写入、PATCH 设置；空值 = 清除、回退默认层。"""
        fake_pm = _FakePM(tmp_path)
        client = build_projects_client(monkeypatch, fake_pm)

        with client:
            created = client.post(
                "/api/v1/projects",
                json={
                    "generation_mode": "storyboard",
                    "title": "视频桶项目",
                    "name": "vb-1",
                    "video_provider_i2v": "minimax/MiniMax-Hailuo-2.3",
                    "video_provider_r2v": "minimax/S2V-01",
                },
            )
            assert created.status_code == 200
            data = fake_pm.project_data["vb-1"]
            assert data["video_provider_i2v"] == "minimax/MiniMax-Hailuo-2.3"
            assert data["video_provider_r2v"] == "minimax/S2V-01"

            updated = client.patch(
                "/api/v1/projects/ready",
                json={"video_provider_r2v": "openai/sora-2"},
            )
            assert updated.status_code == 200
            assert fake_pm.project_data["ready"]["video_provider_r2v"] == "openai/sora-2"

            cleared = client.patch(
                "/api/v1/projects/ready",
                json={"video_provider_r2v": ""},
            )
            assert cleared.status_code == 200
            assert "video_provider_r2v" not in fake_pm.project_data["ready"]

    def test_create_project_rejects_legacy_image_backend(self, tmp_path, monkeypatch):
        """退役的 image_backend 字段不能通过创建接口写入。"""
        fake_pm = _FakePM(tmp_path)
        client = build_projects_client(monkeypatch, fake_pm)

        with client:
            resp = client.post(
                "/api/v1/projects",
                json={
                    "generation_mode": "storyboard",
                    "title": "旧字段项目",
                    "name": "legacy-1",
                    "image_backend": "gemini-aistudio/nano-banana",
                },
            )
            assert resp.status_code == 422
            assert "legacy-1" not in fake_pm.project_data

    def test_create_project_empty_model_fields_not_written(self, tmp_path, monkeypatch):
        fake_pm = _FakePM(tmp_path)
        client = build_projects_client(monkeypatch, fake_pm)

        with client:
            resp = client.post(
                "/api/v1/projects",
                json={
                    "generation_mode": "storyboard",
                    "title": "空字段项目",
                    "name": "e-1",
                    "video_backend": "",
                },
            )
            assert resp.status_code == 200
            data = fake_pm.project_data["e-1"]
            assert "video_backend" not in data
            assert "image_backend" not in data

    def test_create_project_persists_speech_rate(self, tmp_path, monkeypatch):
        """创建时可选填口播语速估算：区间内落盘，未填不落盘（回退语言默认）。"""
        fake_pm = _FakePM(tmp_path)
        client = build_projects_client(monkeypatch, fake_pm)

        with client:
            resp = client.post(
                "/api/v1/projects",
                json={
                    "generation_mode": "storyboard",
                    "title": "语速项目",
                    "name": "sr-1",
                    "speech_rate_units_per_second": 6.5,
                },
            )
            assert resp.status_code == 200
            assert fake_pm.project_data["sr-1"]["speech_rate_units_per_second"] == 6.5

            resp = client.post(
                "/api/v1/projects",
                json={"generation_mode": "storyboard", "title": "默认语速", "name": "sr-2"},
            )
            assert resp.status_code == 200
            assert "speech_rate_units_per_second" not in fake_pm.project_data["sr-2"]

    @pytest.mark.parametrize("bad", [0, -1, 20.5])
    def test_create_project_rejects_out_of_range_speech_rate(self, tmp_path, monkeypatch, bad):
        fake_pm = _FakePM(tmp_path)
        client = build_projects_client(monkeypatch, fake_pm)

        with client:
            resp = client.post(
                "/api/v1/projects",
                json={
                    "generation_mode": "storyboard",
                    "title": "越界语速",
                    "name": "sr-bad",
                    "speech_rate_units_per_second": bad,
                },
            )
            assert resp.status_code == 422

    @pytest.mark.parametrize("value", [True, False])
    def test_create_project_rejects_boolean_speech_rate(self, tmp_path, monkeypatch, value):
        """JSON 布尔不得被 Pydantic 折成 1.0 / 0.0 混进语速覆盖，两个取值都应 422 且不建目录。"""
        fake_pm = _FakePM(tmp_path)
        client = build_projects_client(monkeypatch, fake_pm)

        with client:
            resp = client.post(
                "/api/v1/projects",
                json={
                    "generation_mode": "storyboard",
                    "title": "布尔语速",
                    "name": "sr-bool",
                    "speech_rate_units_per_second": value,
                },
            )
            assert resp.status_code == 422
            assert "sr-bool" not in fake_pm.project_data

    def test_create_project_with_invalid_backend_returns_400(self, tmp_path, monkeypatch):
        """非法 backend 字符串应被校验器拒绝。"""
        fake_pm = _FakePM(tmp_path)
        client = build_projects_client(monkeypatch, fake_pm)

        with client:
            resp = client.post(
                "/api/v1/projects",
                json={
                    "generation_mode": "storyboard",
                    "title": "Bad Backend",
                    "name": "bad-bk",
                    "video_backend": "garbage",  # 无 "/"，且不在 PROVIDER_REGISTRY
                },
            )
            assert resp.status_code == 400

    def test_create_defaults_to_factory_workflow(self, tmp_path, monkeypatch):
        fake_pm = _FakePM(tmp_path)
        with build_projects_client(monkeypatch, fake_pm) as client:
            response = client.post("/api/v1/projects", json={"name": "factory", "title": "短剧"})
        assert response.status_code == 200
        project = response.json()["project"]
        assert project["content_mode"] == "drama"
        assert project["generation_mode"] == "storyboard"
        assert "grid_storyboard" not in project

    @pytest.mark.parametrize(
        "fields",
        [
            {"content_mode": "narration"},
            {"content_mode": "ad"},
            {"generation_mode": "reference_video"},
            {"generation_mode": "grid"},
            {"grid_storyboard": True},
            {"brief": "广告诉求"},
            {"target_duration": 60},
        ],
    )
    def test_create_rejects_retired_workflow_fields(self, tmp_path, monkeypatch, fields):
        fake_pm = _FakePM(tmp_path)
        with build_projects_client(monkeypatch, fake_pm) as client:
            response = client.post("/api/v1/projects", json={"name": "retired", **fields})
        assert response.status_code == 422
        assert "retired" not in fake_pm.project_data


class TestProjectCreationNarrationDelivery:
    """旁白交付方式是必填的项目配置：缺省后期配音，TTS 配音以全局默认预填快照。"""

    def _create(self, client, **fields):
        return client.post(
            "/api/v1/projects",
            json={"generation_mode": "storyboard", "title": "旁白", "name": "n-1", **fields},
        )

    def test_defaults_to_post_production(self, tmp_path, monkeypatch):
        fake_pm = _FakePM(tmp_path)
        with build_projects_client(monkeypatch, fake_pm) as client:
            assert self._create(client).status_code == 200
        data = fake_pm.project_data["n-1"]
        assert data["narration_delivery"] == "post_production"
        assert "audio_backend" not in data

    def test_tts_prefills_snapshot_from_global_defaults(self, tmp_path, monkeypatch, db_factory):
        async def _seed():
            async with db_factory() as session:
                svc = ConfigService(session)
                await svc.set_setting("default_audio_backend", "dashscope/qwen3-tts-flash")
                await svc.set_setting("narration_voice", "Ethan")
                await svc.set_setting("narration_speed", "1.1")
                await session.commit()

        asyncio.run(_seed())
        fake_pm = _FakePM(tmp_path)
        with build_projects_client(monkeypatch, fake_pm, session_factory=db_factory) as client:
            resp = self._create(client, narration_delivery="use_tts", narration_voice="Cherry")
            assert resp.status_code == 200
        data = fake_pm.project_data["n-1"]
        assert data["narration_delivery"] == "use_tts"
        assert data["audio_backend"] == "dashscope/qwen3-tts-flash"
        assert data["narration_voice"] == "Cherry"
        assert data["narration_speed"] == 1.1

    def test_tts_without_model_is_rejected(self, tmp_path, monkeypatch):
        fake_pm = _FakePM(tmp_path)
        with build_projects_client(monkeypatch, fake_pm) as client:
            resp = self._create(
                client,
                narration_delivery="use_tts",
                audio_backend=None,
                narration_voice="Cherry",
                narration_speed=None,
            )
            assert resp.status_code == 422
        assert "n-1" not in fake_pm.project_data
