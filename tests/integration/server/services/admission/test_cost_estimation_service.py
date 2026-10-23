"""Tests for CostEstimationService."""

import pytest

from lib.backends.providers import PROVIDER_GEMINI
from lib.billing.cost_calculator import cost_calculator
from lib.config.resolver import ConfigResolver
from lib.db.repositories.usage_repo import SettlementInput, UsageRepository
from lib.generation.video_request_facts import VideoRequestCostFacts, VideoRequestFactsFailure
from server.services.admission.cost_estimation import CostEstimationService, quote_video_request
from tests.factories import make_video_request_facts


async def _seed_call(
    db_factory,
    project_name: str,
    call_type: str,
    model: str,
    *,
    provider: str = PROVIDER_GEMINI,
    resolution: str | None = None,
    segment_id: str | None = None,
    output_path: str | None = None,
    usage_tokens: int | None = None,
    cost_amount: float | None = None,
    currency: str | None = None,
) -> None:
    """直连 UsageRepository 写入一条已完成调用记录（等价于旧 UsageTracker 的种子写法）。

    ``cost_amount`` 非空时绕过按 model 定价表的自动计算，直接结算为该值——用于不依赖
    具体供应商定价配置、只关心分摊/聚合逻辑的用例（如 unit 费用按镜头摊回）。
    """
    async with db_factory() as session:
        repo = UsageRepository(session)
        cid = await repo.start_call(
            project_name=project_name,
            call_type=call_type,
            model=model,
            provider=provider,
            resolution=resolution,
            segment_id=segment_id,
        )
        await repo.finish_call(
            cid,
            status="success",
            settlement=SettlementInput(usage_tokens=usage_tokens, cost_amount=cost_amount, currency=currency),
            output_path=output_path,
        )


def _make_script(
    episode: int,
    segment_ids: list[str],
    durations: list[int],
    generated_assets_overrides: list[dict] | None = None,
) -> dict:
    """Create drama storyboard items for current image, video, and audio estimates."""
    default_assets = {"storyboard_image": None, "video_clip": None, "status": "pending"}
    segments = []
    for i, (sid, dur) in enumerate(zip(segment_ids, durations, strict=True)):
        assets = {**default_assets}
        if generated_assets_overrides and i < len(generated_assets_overrides):
            assets.update(generated_assets_overrides[i])
        segments.append(
            {
                "scene_id": sid,
                "episode": episode,
                "duration_seconds": dur,
                "segment_break": False,
                "utterances": [],
                "characters_in_scene": [],
                "scenes": [],
                "props": [],
                "image_prompt": {
                    "scene": "s",
                    "composition": {"shot_type": "medium", "lighting": "l", "ambiance": "a"},
                },
                "video_prompt": {"action": "a", "camera_motion": "Static", "ambiance_audio": "aa"},
                "generated_assets": assets,
            }
        )
    return {
        "episode": episode,
        "title": f"Episode {episode}",
        "content_mode": "drama",
        "duration_seconds": sum(durations),
        "summary": "test",
        "novel": {"title": "t", "chapter": "c"},
        "scenes": segments,
    }


def _make_reference_video_script(episode: int, content_mode: str, unit_specs: list[tuple[str, int]]) -> dict:
    """Helper to create a reference_video episode script dict (video_units[])."""
    units = []
    for unit_id, duration in unit_specs:
        units.append(
            {
                "unit_id": unit_id,
                "text": "t",
                "duration_seconds": duration,
                "generated_assets": {"video_clip": None, "status": "pending"},
            }
        )
    return {
        "episode": episode,
        "title": f"Episode {episode}",
        "content_mode": content_mode,
        "generation_mode": "reference_video",
        "duration_seconds": sum(d for _, d in unit_specs),
        "novel": {"title": "t", "chapter": "c"},
        "video_units": units,
    }


class TestCostEstimationService:
    async def test_storyboard_facts_failure_reports_each_segment_without_video_quote(
        self, db_factory, set_video_request_facts
    ):
        failure = VideoRequestFactsFailure(
            "video_supported_durations_incompatible",
            (("provider", "gemini-aistudio"), ("model", "veo-3.1-generate-preview"), ("resolution", "1080p")),
        )
        set_video_request_facts(failure)
        project = {
            "content_mode": "drama",
            "generation_mode": "storyboard",
            "video_provider_i2v": "gemini-aistudio/veo-3.1-generate-preview",
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        result = await CostEstimationService(ConfigResolver(db_factory), db_factory).compute(
            project, {"ep1.json": _make_script(1, ["E1S001", "E1S002"], [8, 8])}, project_name="failed-facts"
        )
        for segment in result["episodes"][0]["segments"]:
            assert segment["estimate"]["video"] == {}
            assert segment["request_projection"]["allowed"] is False
            problem = segment["request_projection"]["problems"][0]
            assert (problem["code"], problem["action"], problem["params"]) == (
                failure.code,
                failure.action,
                failure.parameters(),
            )

    @pytest.mark.parametrize("explicit_resolution", [False, True])
    @pytest.mark.parametrize(
        ("provider", "model", "selected_resolution", "duration", "usage_tokens"),
        [
            ("gemini-aistudio", "veo-3.1-generate-preview", "1080p", 8, None),
            ("openai", "sora-2-pro", "1080p", 8, None),
            ("dashscope", "happyhorse-1.1-i2v", "1080p", 8, None),
            ("grok", "grok-imagine-video", "720p", 8, None),
            ("minimax", "MiniMax-H3", "2k", 6, None),
            ("minimax", "MiniMax-Hailuo-2.3", "1080p", 6, None),
            ("kling", "kling-v3", "4k", 8, None),
            ("vidu", "viduq3-turbo", "1080p", 8, None),
            # token 计价仅在实际 usage 等于预估 token 数时金额相等。
            ("ark", "doubao-seedance-1-5-pro-251215", "1080p", 8, 480_000),
        ],
    )
    async def test_video_estimate_uses_request_resolution_and_settlement_pricing(
        self,
        db_factory,
        explicit_resolution,
        provider,
        model,
        selected_resolution,
        duration,
        usage_tokens,
    ):
        resolution = selected_resolution if explicit_resolution else None
        project = {
            "title": "Test",
            "content_mode": "drama",
            "generation_mode": "storyboard",
            "video_provider_i2v": f"{provider}/{model}",
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        if resolution is not None:
            project["model_settings"] = {f"{provider}/{model}": {"resolution": resolution}}
        script = _make_script(1, ["E1S001"], [duration])

        result = await CostEstimationService(ConfigResolver(db_factory), db_factory).compute(
            project, {"ep1.json": script}, project_name="test-pricing-source"
        )

        estimate = result["episodes"][0]["segments"][0]["estimate"]["video"]
        assert estimate
        assert all(amount > 0 for amount in estimate.values())

        async with db_factory() as session:
            usage = UsageRepository(session)
            call_id = await usage.start_call(
                project_name="test-pricing-source",
                call_type="video",
                provider=provider,
                model=model,
                resolution=resolution,
                duration_seconds=duration,
                generate_audio=True,
                segment_id="billed",
            )
            await usage.finish_call(call_id, status="success", settlement=SettlementInput(usage_tokens=usage_tokens))
            actual = await usage.get_actual_costs_by_segment("test-pricing-source")
        assert estimate == actual["billed"]["video"]

    async def test_shared_video_quote_exposes_exact_amount_currency_and_request_coordinates(self, db_factory):
        quote = await quote_video_request(
            VideoRequestCostFacts(
                request_facts=make_video_request_facts(
                    provider_id="openai", model_id="sora-2", resolution="720p", generate_audio=True
                ),
                duration_seconds=8,
            ),
            db_factory,
        )

        assert quote is not None
        assert quote.to_payload() == {
            "amount": pytest.approx(0.8),
            "currency": "USD",
            "provider_id": "openai",
            "model_id": "sora-2",
            "request_duration_seconds": 8,
        }

    async def test_estimate_single_episode(self, db_factory):
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        scripts = {"ep1.json": _make_script(1, ["E1S001", "E1S002"], [6, 8])}

        result = await service.compute(project_data, scripts, project_name="test")

        assert len(result["episodes"]) == 1
        ep = result["episodes"][0]
        assert len(ep["segments"]) == 2
        for seg in ep["segments"]:
            assert "image" in seg["estimate"]
            assert "video" in seg["estimate"]
            for cost in seg["estimate"].values():
                assert isinstance(cost, dict)
                assert all(isinstance(v, (int, float)) for v in cost.values())

    async def test_actual_costs_included(self, db_factory):
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        await _seed_call(
            db_factory,
            "proj",
            "image",
            "gemini-3.1-flash-image-preview",
            resolution="1K",
            segment_id="E1S001",
            output_path="a.png",
        )

        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        scripts = {"ep1.json": _make_script(1, ["E1S001"], [6])}

        result = await service.compute(project_data, scripts, project_name="proj")

        seg = result["episodes"][0]["segments"][0]
        assert seg["actual"]["image"]["USD"] == pytest.approx(0.067)
        assert "unassigned" not in result["episodes"][0]["totals"]["actual"]
        assert "unassigned" not in result["project_totals"]["actual"]

    async def test_duplicate_unit_id_does_not_double_count_actual_cost(self, db_factory):
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)
        await _seed_call(
            db_factory,
            "duplicate-unit",
            "video",
            "veo-3.1",
            provider="veo",
            segment_id="E1U1",
            cost_amount=1.25,
            currency="USD",
        )
        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "generation_mode": "reference_video",
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        scripts = {
            "ep1.json": _make_reference_video_script(
                1,
                "narration",
                [("E1U1", 5), ("E1U1", 5)],
            )
        }

        result = await service.compute(project_data, scripts, project_name="duplicate-unit")

        assert result["episodes"][0]["totals"]["actual"]["video"]["USD"] == pytest.approx(1.25)
        assert result["project_totals"]["actual"]["video"]["USD"] == pytest.approx(1.25)

    async def test_deleted_unit_actual_cost_is_reconciled_as_unassigned(self, db_factory):
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)
        await _seed_call(
            db_factory,
            "deleted-unit",
            "video",
            "veo-3.1",
            provider="veo",
            segment_id="E1U1",
            cost_amount=1.25,
            currency="USD",
        )
        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "generation_mode": "reference_video",
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        scripts = {"ep1.json": _make_reference_video_script(1, "narration", [("E1U2", 5)])}

        result = await service.compute(project_data, scripts, project_name="deleted-unit")

        episode = result["episodes"][0]
        assert episode["totals"]["actual"]["unassigned"]["USD"] == pytest.approx(1.25)
        assert result["project_totals"]["actual"]["unassigned"]["USD"] == pytest.approx(1.25)

    async def test_replaced_script_reconciles_all_historical_actual_costs(self, db_factory):
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)
        for unit_id, amount, call_type in (
            ("E1U1", 0.4, "image"),
            ("E1U2", 1.6, "video"),
            ("E1U3", 0.2, "audio"),
        ):
            await _seed_call(
                db_factory,
                "replaced-script",
                call_type,
                "historical-model",
                segment_id=unit_id,
                cost_amount=amount,
                currency="USD",
            )
        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "generation_mode": "reference_video",
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        scripts = {"ep1.json": _make_reference_video_script(1, "narration", [("E1U9", 5)])}

        result = await service.compute(project_data, scripts, project_name="replaced-script")

        episode = result["episodes"][0]
        assert episode["totals"]["actual"]["unassigned"]["USD"] == pytest.approx(2.2)
        assert result["project_totals"]["actual"]["unassigned"]["USD"] == pytest.approx(2.2)

    async def test_missing_script_episode_still_gets_history_row(self, db_factory):
        """剧本文件缺失的集不进入估算，但它的历史支出仍要落在该集行上。"""
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)
        await _seed_call(
            db_factory,
            "missing-script",
            "video",
            "historical-model",
            segment_id="E2U1",
            cost_amount=1.5,
            currency="USD",
        )
        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "generation_mode": "reference_video",
            "episodes": [
                {"episode": 1, "title": "Ep1", "script_file": "ep1.json"},
                {"episode": 2, "title": "Ep2", "script_file": "ep2.json"},
            ],
        }
        # ep2.json 不在 scripts 里：模拟剧本文件已丢失、集元数据仍在 project.json 的状态
        scripts = {"ep1.json": _make_reference_video_script(1, "narration", [("E1U1", 5)])}

        result = await service.compute(project_data, scripts, project_name="missing-script")

        episodes = result["episodes"]
        assert [ep["episode"] for ep in episodes] == [1, 2]
        assert episodes[1]["title"] == "Ep2"
        assert episodes[1]["segments"] == []
        assert episodes[1]["totals"]["actual"]["unassigned"]["USD"] == pytest.approx(1.5)
        assert result["project_totals"]["actual"]["unassigned"]["USD"] == pytest.approx(1.5)

    async def test_null_segment_history_counts_as_unassigned(self, db_factory):
        """segment_id 为空的历史 video/audio 与无法归类的图，同样是真实支出。"""
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)
        await _seed_call(
            db_factory,
            "null-segment",
            "video",
            "historical-model",
            segment_id=None,
            cost_amount=2.0,
            currency="USD",
        )
        await _seed_call(
            db_factory,
            "null-segment",
            "audio",
            "historical-model",
            segment_id=None,
            cost_amount=0.5,
            currency="USD",
        )
        await _seed_call(
            db_factory,
            "null-segment",
            "image",
            "historical-model",
            segment_id=None,
            output_path="misc/legacy.png",
            cost_amount=0.3,
            currency="USD",
        )
        await _seed_call(
            db_factory,
            "null-segment",
            "image",
            "historical-model",
            segment_id=None,
            output_path="characters/hero.png",
            cost_amount=0.7,
            currency="USD",
        )
        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "generation_mode": "reference_video",
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        scripts = {"ep1.json": _make_reference_video_script(1, "narration", [("E1U1", 5)])}

        result = await service.compute(project_data, scripts, project_name="null-segment")

        project_actual = result["project_totals"]["actual"]
        # 资产图按类型单列，其余（other 图 + video + audio）归入未归属
        assert project_actual["characters"]["USD"] == pytest.approx(0.7)
        assert project_actual["unassigned"]["USD"] == pytest.approx(2.8)
        assert "unassigned" not in result["episodes"][0]["totals"]["actual"]

    async def test_unit_id_named_like_project_sentinel_is_not_double_counted(self, db_factory):
        """剧本用哨兵键字面量作 unit_id 时，它的支出只算一次（哨兵键不与真实 ID 共用取值）。"""
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)
        await _seed_call(
            db_factory,
            "sentinel-unit",
            "video",
            "historical-model",
            segment_id="__project__",
            cost_amount=3.0,
            currency="USD",
        )
        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "generation_mode": "reference_video",
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        scripts = {"ep1.json": _make_reference_video_script(1, "narration", [("__project__", 5)])}

        result = await service.compute(project_data, scripts, project_name="sentinel-unit")

        project_actual = result["project_totals"]["actual"]
        assert project_actual["video"]["USD"] == pytest.approx(3.0)
        assert "unassigned" not in project_actual

    async def test_grid_actual_costs_apportioned_to_scenes(self, db_factory):
        """Grid actual cost should be split evenly among scenes sharing the grid_id."""
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        grid_id = "grid_abc123"
        seg_ids = [f"E1S{i:03d}" for i in range(1, 10)]  # 9 scenes

        # Record grid image API call
        await _seed_call(
            db_factory,
            "proj",
            "image",
            "gemini-3.1-flash-image-preview",
            resolution="2K",
            segment_id=grid_id,
            output_path="g.png",
        )

        # All 9 scenes reference the same grid_id
        overrides = [{"grid_id": grid_id, "grid_cell_index": i} for i in range(9)]
        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "generation_mode": "storyboard",
            "grid_storyboard": True,
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        scripts = {"ep1.json": _make_script(1, seg_ids, [6] * 9, generated_assets_overrides=overrides)}

        result = await service.compute(project_data, scripts, project_name="proj")

        # Each scene should get 1/9 of the grid cost
        expected_per_scene = round(0.101 / 9, 6)
        for seg in result["episodes"][0]["segments"]:
            assert seg["actual"]["image"]["USD"] == pytest.approx(expected_per_scene, abs=1e-5)

        # Episode total should equal the full grid cost
        ep_total_image = result["episodes"][0]["totals"]["actual"].get("image", {})
        assert ep_total_image.get("USD", 0) == pytest.approx(0.101, abs=1e-4)

        # Project totals should NOT have a separate "grid" bucket
        assert "grid" not in result["project_totals"]["actual"]
        # But should have the cost under "image"
        assert result["project_totals"]["actual"]["image"]["USD"] == pytest.approx(0.101, abs=1e-4)

    async def test_grid_duplicate_ids_each_claim_own_share(self, db_factory):
        """一张宫格覆盖两个共用同一 ID 的条目（ADR 0053 明确接受的受支持状态）：

        两条目应各拿自己那一份均摊份额，而不是把宫格实付重复计入合计。"""
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        grid_id = "grid_dup"
        await _seed_call(
            db_factory,
            "proj-dup",
            "image",
            "historical-model",
            segment_id=grid_id,
            cost_amount=1.0,
            currency="USD",
        )

        overrides = [
            {"grid_id": grid_id, "grid_cell_index": 0},
            {"grid_id": grid_id, "grid_cell_index": 1},
        ]
        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "generation_mode": "storyboard",
            "grid_storyboard": True,
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        scripts = {"ep1.json": _make_script(1, ["E1S001", "E1S001"], [6, 6], generated_assets_overrides=overrides)}

        result = await service.compute(project_data, scripts, project_name="proj-dup")

        segments = result["episodes"][0]["segments"]
        assert len(segments) == 2
        for seg in segments:
            assert seg["segment_id"] == "E1S001"
            assert seg["actual"]["image"]["USD"] == pytest.approx(0.5)

        ep_total_image = result["episodes"][0]["totals"]["actual"].get("image", {})
        assert ep_total_image.get("USD", 0) == pytest.approx(1.0)
        assert result["project_totals"]["actual"]["image"]["USD"] == pytest.approx(1.0)

    async def test_grid_duplicate_ids_across_multiple_grids(self, db_factory):
        """同一 ID 既在一张宫格内出现 3 次、又跨到另一张宫格：每个条目只拿所属宫格的那一份。"""
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        for grid_id, amount in (("grid_a", 0.9), ("grid_b", 1.0)):
            await _seed_call(
                db_factory,
                "proj-multi",
                "image",
                "historical-model",
                segment_id=grid_id,
                cost_amount=amount,
                currency="USD",
            )

        overrides = [
            {"grid_id": "grid_a", "grid_cell_index": 0},
            {"grid_id": "grid_a", "grid_cell_index": 1},
            {"grid_id": "grid_a", "grid_cell_index": 2},
            {"grid_id": "grid_b", "grid_cell_index": 0},
            {"grid_id": "grid_b", "grid_cell_index": 1},
        ]
        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "generation_mode": "storyboard",
            "grid_storyboard": True,
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        seg_ids = ["E1S001", "E1S001", "E1S001", "E1S001", "E1S002"]
        scripts = {"ep1.json": _make_script(1, seg_ids, [6] * 5, generated_assets_overrides=overrides)}

        result = await service.compute(project_data, scripts, project_name="proj-multi")

        per_scene_costs = [seg["actual"]["image"]["USD"] for seg in result["episodes"][0]["segments"]]
        assert per_scene_costs == pytest.approx([0.3, 0.3, 0.3, 0.5, 0.5])
        assert result["project_totals"]["actual"]["image"]["USD"] == pytest.approx(1.9)

    async def test_grid_actual_split_remainder_sums_exactly(self, db_factory):
        """除不尽的宫格实付（USD 0.101 均摊 9 份）分摊后，各份之和须与冻结实付分文不差。"""
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        grid_id = "grid_remainder"
        seg_ids = [f"E1S{i:03d}" for i in range(1, 10)]  # 9 scenes

        await _seed_call(
            db_factory,
            "proj-rem",
            "image",
            "historical-model",
            segment_id=grid_id,
            cost_amount=0.101,
            currency="USD",
        )

        overrides = [{"grid_id": grid_id, "grid_cell_index": i} for i in range(9)]
        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "generation_mode": "storyboard",
            "grid_storyboard": True,
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        scripts = {"ep1.json": _make_script(1, seg_ids, [6] * 9, generated_assets_overrides=overrides)}

        result = await service.compute(project_data, scripts, project_name="proj-rem")

        per_scene_costs = [seg["actual"]["image"]["USD"] for seg in result["episodes"][0]["segments"]]
        assert sum(per_scene_costs) == pytest.approx(0.101, abs=1e-9)

    async def test_claimed_key_keeps_unconsumed_cost_types_as_unassigned(self, db_factory):
        """认领粒度到 (记账 key, 类型)：宫格 key 上只消费 image，同 key 的 video 仍须计入未归属。"""
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        grid_id = "grid_mixed"
        seg_ids = ["E1S001", "E1S002"]
        await _seed_call(
            db_factory,
            "mixed-key",
            "image",
            "historical-model",
            segment_id=grid_id,
            cost_amount=0.6,
            currency="USD",
        )
        await _seed_call(
            db_factory,
            "mixed-key",
            "video",
            "historical-model",
            segment_id=grid_id,
            cost_amount=1.4,
            currency="USD",
        )

        overrides = [{"grid_id": grid_id, "grid_cell_index": i} for i in range(2)]
        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "generation_mode": "storyboard",
            "grid_storyboard": True,
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        scripts = {"ep1.json": _make_script(1, seg_ids, [6, 6], generated_assets_overrides=overrides)}

        result = await service.compute(project_data, scripts, project_name="mixed-key")

        project_actual = result["project_totals"]["actual"]
        assert project_actual["image"]["USD"] == pytest.approx(0.6)
        assert project_actual["unassigned"]["USD"] == pytest.approx(1.4)

    async def test_grid_partial_generation_some_without_grid_id(self, db_factory):
        """Scenes without grid_id should have empty actual image cost."""
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        grid_id = "grid_partial"
        seg_ids = [f"E1S{i:03d}" for i in range(1, 6)]  # 5 scenes

        await _seed_call(
            db_factory,
            "proj",
            "image",
            "gemini-3.1-flash-image-preview",
            resolution="2K",
            segment_id=grid_id,
            output_path="g.png",
        )

        # Only first 3 scenes have grid_id
        overrides = [
            {"grid_id": grid_id, "grid_cell_index": 0},
            {"grid_id": grid_id, "grid_cell_index": 1},
            {"grid_id": grid_id, "grid_cell_index": 2},
            {},  # no grid_id
            {},  # no grid_id
        ]
        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "generation_mode": "storyboard",
            "grid_storyboard": True,
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        scripts = {"ep1.json": _make_script(1, seg_ids, [6] * 5, generated_assets_overrides=overrides)}

        result = await service.compute(project_data, scripts, project_name="proj")

        segments = result["episodes"][0]["segments"]
        expected = round(0.101 / 3, 6)
        for seg in segments[:3]:
            assert seg["actual"]["image"]["USD"] == pytest.approx(expected, abs=1e-5)
        for seg in segments[3:]:
            assert seg["actual"]["image"] == {}

    async def test_single_mode_unaffected_by_grid_logic(self, db_factory):
        """Single generation mode should be completely unaffected by grid apportionment."""
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        await _seed_call(
            db_factory,
            "proj",
            "image",
            "gemini-3.1-flash-image-preview",
            resolution="1K",
            segment_id="E1S001",
            output_path="a.png",
        )

        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "generation_mode": "single",
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        scripts = {"ep1.json": _make_script(1, ["E1S001", "E1S002"], [6, 8])}

        result = await service.compute(project_data, scripts, project_name="proj")

        seg1 = result["episodes"][0]["segments"][0]
        assert seg1["actual"]["image"]["USD"] == pytest.approx(0.067)
        seg2 = result["episodes"][0]["segments"][1]
        assert seg2["actual"]["image"] == {}

    async def test_plain_storyboard_estimate_prices_at_resolved_resolution(self, db_factory, monkeypatch):
        """普通（非宫格）分镜图同样按执行期生效的分辨率档计价，未配置时按保底档。"""
        from server.services.admission import cost_estimation as ce
        from server.services.admission.cost_estimation import _IMAGE_PRICING_FALLBACK_RESOLUTION

        seg_ids = [f"E1S{i:03d}" for i in range(1, 4)]
        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "generation_mode": "storyboard",
            # 按分辨率分档定价的型号，档位差异才可观测
            "image_provider_t2i": "gemini-aistudio/gemini-3.1-flash-image-preview",
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        scripts = {"ep1.json": _make_script(1, seg_ids, [6] * 3)}

        async def _estimated_image_total(resolution: str | None) -> float:
            async def _resolution(_r, _project):
                return resolution

            monkeypatch.setattr(ce, "resolve_image_resolution", _resolution)
            service = CostEstimationService(ConfigResolver(db_factory), db_factory)
            result = await service.compute(project_data, scripts, project_name="proj")
            return sum(seg["estimate"]["image"]["USD"] for seg in result["episodes"][0]["segments"])

        total_fallback = await _estimated_image_total(_IMAGE_PRICING_FALLBACK_RESOLUTION)
        assert total_fallback > 0
        # 该型号 4K 单价高于保底档，估算须随之上浮而非恒按保底档
        assert await _estimated_image_total("4K") > total_fallback
        # 解析失败（未配置图像供应商）按保底档计价，不抛错
        assert await _estimated_image_total(None) == pytest.approx(total_fallback)

    async def test_project_level_actual_split_by_asset_type(self, db_factory):
        """project-level image 成本应按 output_path 前缀拆分为 characters/scenes/props 三项。"""
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        # 3 条 project-level image 调用，分别落在 characters / scenes / props
        for sub in ("characters", "scenes", "props"):
            await _seed_call(
                db_factory,
                "proj",
                "image",
                "gemini-3.1-flash-image-preview",
                resolution="1K",
                output_path=f"projects/proj/{sub}/a.png",
            )

        result = await service.compute(
            {"title": "T", "content_mode": "drama", "episodes": []},
            {},
            project_name="proj",
        )
        actual = result["project_totals"]["actual"]

        assert "characters" in actual
        assert actual["characters"].get("USD", 0) > 0
        assert "scenes" in actual
        assert actual["scenes"].get("USD", 0) > 0
        assert "props" in actual
        assert actual["props"].get("USD", 0) > 0
        # 旧 key 不应出现
        assert "character_and_clue" not in actual

    async def test_dirty_script_skipped_with_warning(self, db_factory, caplog):
        """单集脏脚本(segments=null)不应让整个项目费用估算 5xx;脏集降级为 0 segments
        + warning,其他正常集仍参与估算。"""
        import logging

        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "episodes": [
                {"episode": 1, "title": "Ep1", "script_file": "ep1.json"},
                {"episode": 2, "title": "Ep2-dirty", "script_file": "ep2.json"},
                {"episode": 3, "title": "Ep3", "script_file": "ep3.json"},
            ],
        }
        # ep2 segments 是 null(脏数据)→ get_storyboard_items 抛 ScriptEditError
        dirty_script = {
            "episode": 2,
            "title": "Dirty",
            "content_mode": "drama",
            "summary": "t",
            "novel": {"title": "t", "chapter": "c"},
            "scenes": None,  # 脏数据
        }
        scripts = {
            "ep1.json": _make_script(1, ["E1S001"], [6]),
            "ep2.json": dirty_script,
            "ep3.json": _make_script(3, ["E3S001"], [8]),
        }

        with caplog.at_level(logging.WARNING, logger="server.services.admission.cost_estimation"):
            result = await service.compute(project_data, scripts, project_name="test")

        # 正常集 ep1 / ep3 都参与估算,脏集 ep2 仍出现但 segments 为空
        assert len(result["episodes"]) == 3
        eps_by_episode = {ep["episode"]: ep for ep in result["episodes"]}
        assert len(eps_by_episode[1]["segments"]) == 1
        assert len(eps_by_episode[2]["segments"]) == 0
        assert len(eps_by_episode[3]["segments"]) == 1

        # warning 显式标出哪一集被跳过
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("ep2.json" in m for m in warnings), warnings

    async def test_audio_estimate_per_segment_by_characters(self, db_factory):
        """旁白配音预估 = novel_text 字符数 × 项目 TTS 快照模型的按字符费率；models 含 audio 条目。"""
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "narration_delivery": "use_tts",
            "audio_backend": "dashscope/qwen3-tts-flash",
            "narration_voice": "Cherry",
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        script = _make_script(1, ["E1S001", "E1S002"], [6, 8])
        script["scenes"][0]["utterances"] = [{"kind": "voiceover", "speaker": None, "text": "字" * 100}]
        script["scenes"][1]["utterances"] = []
        scripts = {"ep1.json": script}

        result = await service.compute(project_data, scripts, project_name="test")

        assert result["models"]["audio"] == {"provider": "dashscope", "model": "qwen3-tts-flash"}
        segments = result["episodes"][0]["segments"]
        # qwen3-tts-flash 按 ¥0.8/万字符：100 字 → 0.008 CNY
        assert segments[0]["estimate"]["audio"]["CNY"] == pytest.approx(0.008)
        # 无原文的段不产生旁白预估
        assert segments[1]["estimate"]["audio"] == {}
        # 集/项目两级合计纳入 audio
        assert result["episodes"][0]["totals"]["estimate"]["audio"]["CNY"] == pytest.approx(0.008)
        assert result["project_totals"]["estimate"]["audio"]["CNY"] == pytest.approx(0.008)

    async def test_post_production_project_has_no_audio_estimate(self, db_factory):
        """后期配音项目即使保留着 TTS 快照也不产生旁白配音预估，全局默认同样不参与。"""
        from lib.config.service import ConfigService

        async with db_factory() as session:
            await ConfigService(session).set_setting("default_audio_backend", "dashscope/qwen3-tts-flash")
            await session.commit()

        service = CostEstimationService(ConfigResolver(db_factory), db_factory)
        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "narration_delivery": "post_production",
            "audio_backend": "dashscope/qwen3-tts-flash",
            "narration_voice": "Cherry",
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        script = _make_script(1, ["E1S001"], [6])
        script["scenes"][0]["utterances"] = [{"kind": "voiceover", "speaker": None, "text": "字" * 100}]

        result = await service.compute(project_data, {"ep1.json": script}, project_name="test")

        assert result["episodes"][0]["segments"][0]["estimate"]["audio"] == {}

    async def test_audio_actual_costs_included(self, db_factory):
        """旁白实际费用按 segment 聚合进 actual.audio。"""
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        await _seed_call(
            db_factory,
            "proj",
            "audio",
            "qwen3-tts-flash",
            provider="dashscope",
            segment_id="E1S001",
            output_path="a.wav",
            usage_tokens=100,
        )

        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        scripts = {"ep1.json": _make_script(1, ["E1S001"], [6])}

        result = await service.compute(project_data, scripts, project_name="proj")

        seg = result["episodes"][0]["segments"][0]
        assert seg["actual"]["audio"]["CNY"] == pytest.approx(0.008)
        assert result["project_totals"]["actual"]["audio"]["CNY"] == pytest.approx(0.008)

    async def test_drama_reference_video_actual_cost_matches_unit_id(self, db_factory):
        """actual 侧直接按 unit_id 匹配，不需要摊分——reference_videos 的记账 resource_id
        即 unit_id，与本路径输出的 segment_id 同一 identity。
        """
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        await _seed_call(
            db_factory,
            "drama-ref-actual",
            "video",
            "veo-3.1",
            provider="veo",
            segment_id="E1U1",
            cost_amount=0.8,
            currency="USD",
        )

        project_data = {
            "title": "Drama",
            "content_mode": "drama",
            "generation_mode": "reference_video",
            "target_duration": 30,
            "episodes": [{"episode": 1, "title": "", "script_file": "ep1.json"}],
        }
        scripts = {"ep1.json": _make_reference_video_script(1, "drama", [("E1U1", 8)])}

        result = await service.compute(project_data, scripts, project_name="drama-ref-actual")

        segments = result["episodes"][0]["segments"]
        assert segments[0]["segment_id"] == "E1U1"
        assert segments[0]["actual"]["video"]["USD"] == pytest.approx(0.8)
        assert result["episodes"][0]["totals"]["actual"]["video"]["USD"] == pytest.approx(0.8)
        assert result["project_totals"]["actual"]["video"]["USD"] == pytest.approx(0.8)

    async def test_storyboard_route_gives_no_estimate_for_mismatched_unit_script(self, db_factory):
        """分镜图生视频项目下的失配剧本（video_units 骨架）不产生预估：估算只认项目生成模式。"""
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        project_data = {
            "title": "Narration",
            "content_mode": "drama",
            "generation_mode": "storyboard",
            "target_duration": 30,
            "episodes": [{"episode": 1, "title": "", "script_file": "ep1.json"}],
        }
        scripts = {"ep1.json": _make_reference_video_script(1, "narration", [("E1U1", 6)])}

        result = await service.compute(project_data, scripts, project_name="narration-mismatched-units")

        assert result["episodes"][0]["segments"] == []
        assert not result["project_totals"]["estimate"]["video"]

    async def test_empty_episodes(self, db_factory):
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        result = await service.compute({"title": "T", "content_mode": "drama", "episodes": []}, {}, project_name="p")

        assert result["episodes"] == []
        assert result["project_totals"]["estimate"] == {}

    async def test_cost_estimation_uses_t2i_default_when_split_fields_present(self, db_factory):
        """project 仅有 image_provider_t2i 时，cost estimation 用此值估算（T2I 是 cost estimation 锚点）。"""
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "image_provider_t2i": "openai/gpt-image-1",
            "image_provider_i2i": "openai/gpt-image-1-edit",
            "episodes": [],
        }

        result = await service.compute(project_data, {}, project_name="test_split")

        # T2I field should be the canonical image cost estimation anchor
        assert result["models"]["image"]["provider"] == "openai"
        assert result["models"]["image"]["model"] == "gpt-image-1"

    async def test_cost_estimation_no_image_provider_falls_back_to_resolver(self, db_factory):
        """project 没有 image_provider_t2i 时，cost_estimation 不再自行 fallback I2I 或 legacy
        （legacy 由 ProjectManager.load_project 的 lazy upgrade 处理；I2I 和 T2I 是正交能力槽，
        互替会算到错误价目）。无 T2I 字段则使用 resolver 默认值。"""
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        project_data = {
            "title": "Test",
            "content_mode": "drama",
            # 仅有 i2i 与 legacy 字段：cost_estimation 应忽略，落到 resolver 默认值
            "image_provider_i2i": "openai/gpt-image-1-edit",
            "image_backend": "gemini/gemini-2.0-flash-preview-image-generation",
            "episodes": [],
        }

        result = await service.compute(project_data, {}, project_name="test_no_t2i")

        # 正向锁定：项目无 T2I 字段时走 resolver；空 DB 没有任何 image provider，
        # cost_estimation 走 except 分支返回 ("unknown", "unknown")。
        # 这个契约同时排除掉 i2i 槽（gpt-image-1-edit）和 legacy（gemini-2.0-...）。
        assert result["models"]["image"]["provider"] == "unknown"
        assert result["models"]["image"]["model"] == "unknown"

    async def test_cost_estimation_does_not_hide_unexpected_resolution_error(self, db_factory, monkeypatch):
        """意外的程序错误应暴露，而不是作为能力不可用生成伪报价。"""
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        async def _raise(self, project, provider_id, model_id):
            raise RuntimeError("boom")

        monkeypatch.setattr(ConfigResolver, "resolve_resolution", _raise)

        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "episodes": [],
        }

        with pytest.raises(RuntimeError, match="boom"):
            await service.compute(project_data, {}, project_name="test_resolution_exc")

    @pytest.mark.parametrize(
        ("video_backend", "configured_generate_audio", "expected_usd"),
        [
            # 未设分辨率时 Veo 按 720p 出片。AI Studio 无 audio-off 档，供应商恒按含音价出账：
            # 无论开关状态，预估都须落在 veo-3.1-lite-generate-preview 720p 的含音价 0.05 USD/s。
            ("gemini-aistudio", True, 0.05),
            ("gemini-aistudio", False, 0.05),
            # Vertex 有独立的 audio-off 档，不受 AI Studio 修正影响，预估随开关走
            # （veo-3.1-fast-generate-001 720p：含音 0.10 / 无音 0.08 USD/s）。
            ("gemini-vertex", True, 0.10),
            ("gemini-vertex", False, 0.08),
        ],
    )
    async def test_video_estimate_generate_audio_by_provider(
        self, db_factory, video_backend, configured_generate_audio, expected_usd
    ):
        """费用预估在所有 (provider, audio 开关) 组合下与供应商实际出账口径一致。

        直接在已加载 project dict 中给出项目开关，验证能力解析不二次依赖磁盘项目文件。
        """

        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "video_backend": video_backend,
            "video_generate_audio": configured_generate_audio,
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        scripts = {"ep1.json": _make_script(1, ["E1S001"], [1])}

        result = await service.compute(project_data, scripts, project_name="test")

        seg = result["episodes"][0]["segments"][0]
        assert seg["estimate"]["video"]["USD"] == pytest.approx(expected_usd)

    async def test_disabled_custom_video_model_estimate_reports_unknown(self, db_factory):
        """估算模型身份与执行同口径：项目模型被禁用后按任务类型桶解析闸算悬空引用，不改按该供应商
        默认启用模型出价——那个模型用户没选过，执行期也不会用它（``docs/adr/0054``）。"""
        from lib.db.repositories.custom_provider_repo import CustomProviderRepository

        async with db_factory() as session:
            await CustomProviderRepository(session).create_provider(
                display_name="Custom",
                discovery_format="openai",
                base_url="https://api.example.com",
                api_key="k",
                models=[
                    {
                        "model_id": "disabled",
                        "display_name": "Disabled",
                        "endpoint": "openai-video",
                        "is_enabled": False,
                        "price_unit": "second",
                        "price_input": 9.0,
                        "currency": "USD",
                    },
                    {
                        "model_id": "runtime",
                        "display_name": "Runtime",
                        "endpoint": "openai-video",
                        "is_enabled": True,
                        "is_default": True,
                        "price_unit": "second",
                        "price_input": 0.1,
                        "currency": "USD",
                    },
                ],
            )
            await session.commit()

        service = CostEstimationService(ConfigResolver(db_factory), db_factory)
        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "video_backend": "custom-1/disabled",
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }

        result = await service.compute(
            project_data, {"ep1.json": _make_script(1, ["E1S001"], [6])}, project_name="test-custom-fallback"
        )

        assert result["models"]["video"] == {"provider": "unknown", "model": "unknown"}
        # 身份解析不出时退到通用目录价，既不按被禁用模型（9.0/s → 54.0）也不按该供应商默认
        # 启用模型（0.1/s → 0.6）的 DB 单价出数
        video_estimate = result["episodes"][0]["segments"][0]["estimate"]["video"]
        assert video_estimate.get("USD") != pytest.approx(0.6)
        assert video_estimate.get("USD") != pytest.approx(54.0)

    async def test_all_episodes_priced_by_the_project_route_bucket(self, db_factory, monkeypatch):
        """全项目同一种生成模式、同一个桶：算价不逐集分歧，也不被某集的剧本形状带偏。

        项目生成模式是 storyboard，ep2 是失配的 video_units 骨架——它不产生预估（生成侧会拒绝
        并要求重拆），更不会把估算拽去 r2v 桶。
        """
        priced_models: list[str | None] = []
        original = cost_calculator.calculate_cost

        def _spy(provider, params, **kwargs):
            if params.call_type == "video":
                priced_models.append(params.model)
            return original(provider, params, **kwargs)

        monkeypatch.setattr(cost_calculator, "calculate_cost", _spy)

        service = CostEstimationService(ConfigResolver(db_factory), db_factory)
        project_data = {
            "title": "Narration",
            "content_mode": "drama",
            "generation_mode": "storyboard",
            "target_duration": 30,
            "video_provider_i2v": "kling/kling-v3",
            "video_provider_r2v": "kling/kling-v3-omni",
            "episodes": [
                {"episode": 1, "title": "", "script_file": "ep1.json"},
                {"episode": 2, "title": "", "script_file": "ep2.json"},
            ],
        }
        scripts = {
            "ep1.json": _make_script(1, ["E1S001"], [6]),
            "ep2.json": _make_reference_video_script(2, "narration", [("E2U1", 6)]),
        }

        result = await service.compute(project_data, scripts, project_name="per-episode-bucket")

        # 只有骨架与生成模式相符的 ep1 产生预估，且按项目生成模式的 i2v 桶算价。
        assert priced_models == ["kling-v3"]
        assert result["episodes"][1]["segments"] == []
        assert result["models"]["video"] == {"provider": "kling", "model": "kling-v3"}

    async def test_custom_provider_estimates_use_db_prices(self, db_factory):
        """自定义供应商预估：image/video/audio 单价来自 DB（与实际记账同源），估值按配置价格非零。"""
        from lib.db.repositories.custom_provider_repo import CustomProviderRepository

        async with db_factory() as session:
            await CustomProviderRepository(session).create_provider(
                display_name="Custom",
                discovery_format="openai",
                base_url="https://api.example.com",
                api_key="k",
                models=[
                    {
                        "model_id": "img",
                        "display_name": "Img",
                        "endpoint": "openai-images",
                        "price_unit": "image",
                        "price_input": 0.05,
                        "currency": "USD",
                    },
                    {
                        "model_id": "vid",
                        "display_name": "Vid",
                        "endpoint": "openai-video",
                        "supported_durations": "[6]",
                        "price_unit": "second",
                        "price_input": 0.10,
                        "currency": "USD",
                    },
                    {
                        "model_id": "aud",
                        "display_name": "Aud",
                        "endpoint": "openai-tts",
                        "price_unit": "character",
                        "price_input": 2.0,
                        "currency": "CNY",
                    },
                ],
            )
            await session.commit()

        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "image_provider_t2i": "custom-1/img",
            "video_backend": "custom-1/vid",
            "narration_delivery": "use_tts",
            "audio_backend": "custom-1/aud",
            "narration_voice": "alloy",
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        script = _make_script(1, ["E1S001"], [6])
        script["scenes"][0]["utterances"] = [{"kind": "voiceover", "speaker": None, "text": "字" * 10000}]  # 1 万字符
        scripts = {"ep1.json": script}

        result = await service.compute(project_data, scripts, project_name="test-custom")

        assert result["models"]["image"] == {"provider": "custom-1", "model": "img"}
        seg = result["episodes"][0]["segments"][0]
        # image：自定义供应商按张计费，flat 0.05 USD（不随 1K/2K 变化）
        assert seg["estimate"]["image"]["USD"] == pytest.approx(0.05)
        # video：时长 6s × 0.10 = 0.60 USD
        assert seg["estimate"]["video"]["USD"] == pytest.approx(0.60)
        # audio：10000 字符 / 10000 × 2.0 = 2.0 CNY
        assert seg["estimate"]["audio"]["CNY"] == pytest.approx(2.0)
        # 集/项目两级合计同步纳入
        assert result["project_totals"]["estimate"]["video"]["USD"] == pytest.approx(0.60)

    async def test_custom_provider_without_price_degrades_to_zero(self, db_factory):
        """自定义供应商查无价格模型：预估降级为 0（记 debug 日志、不抛错），与现状降级口径一致。"""
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "image_provider_t2i": "custom-99/ghost",  # DB 无此供应商/模型
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        scripts = {"ep1.json": _make_script(1, ["E1S001"], [6])}

        result = await service.compute(project_data, scripts, project_name="test-noprice")

        # 断言解析到的仍是该自定义供应商/模型，排除 resolver 回落 unknown 导致的同结果假阳性
        assert result["models"]["image"] == {"provider": "custom-99", "model": "ghost"}
        # 缺价 → calculate_cost 返回 0，_add_cost 过滤，image 估值为空且未抛错
        seg = result["episodes"][0]["segments"][0]
        assert seg["estimate"]["image"] == {}

    async def test_custom_provider_malformed_id_degrades_to_zero(self, db_factory):
        """畸形 custom- provider id（非数字后缀）：parse_provider_id 的 ValueError 需降级为 0，不抛错。"""
        resolver = ConfigResolver(db_factory)
        service = CostEstimationService(resolver, db_factory)

        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "image_provider_t2i": "custom-abc/ghost",  # 写入侧校验只查前缀，后缀非数字仍可能入库
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        scripts = {"ep1.json": _make_script(1, ["E1S001"], [6])}

        result = await service.compute(project_data, scripts, project_name="test-malformed-id")

        # 断言解析到的仍是该畸形 provider/model，排除 resolver 回落 unknown 导致的同结果假阳性
        assert result["models"]["image"] == {"provider": "custom-abc", "model": "ghost"}
        seg = result["episodes"][0]["segments"][0]
        assert seg["estimate"]["image"] == {}


class TestUnpricedCosts:
    """预估与实际两侧标出没有价格的部分，让界面区分真实的 0 与未知。"""

    async def test_custom_model_without_price_is_reported_on_both_sides(self, db_factory):
        from lib.db.repositories.custom_provider_repo import CustomProviderRepository

        async with db_factory() as session:
            await CustomProviderRepository(session).create_provider(
                display_name="Relay",
                discovery_format="openai",
                base_url="https://api.example.com",
                api_key="k",
                models=[
                    {"model_id": "img", "display_name": "Img", "endpoint": "openai-images"},
                    {
                        "model_id": "vid",
                        "display_name": "Vid",
                        "endpoint": "openai-video",
                        "supported_durations": "[6]",
                        "price_unit": "second",
                        "price_input": 0.0,
                        "currency": "USD",
                    },
                ],
            )
            await session.commit()
        await _seed_call(db_factory, "test-unpriced", "image", "img", provider="custom-1", segment_id="E1S001")
        await _seed_call(db_factory, "test-unpriced", "video", "vid", provider="custom-1", segment_id="E1S001")

        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "image_provider_t2i": "custom-1/img",
            "video_provider_i2v": "custom-1/vid",
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }
        scripts = {"ep1.json": _make_script(1, ["E1S001", "E1S002"], [6, 6])}

        result = await CostEstimationService(ConfigResolver(db_factory), db_factory).compute(
            project_data, scripts, project_name="test-unpriced"
        )

        unpriced_img = {"call_type": "image", "provider": "custom-1", "provider_name": "Relay", "model": "img"}
        # 价格设为 0 的视频模型是真实的 0，不算未计价
        assert result["unpriced"]["estimate"] == [{**unpriced_img, "count": 2}]
        assert result["unpriced"]["actual"] == [{**unpriced_img, "count": 1}]
        assert result["missing_local_calls"] is False

    async def test_zero_cost_call_is_priced_once_the_model_has_a_price(self, db_factory):
        """调用时还没有价格、之后设了价格的模型不再算未计价：设置价格后提示随之消失。"""
        from lib.db.repositories.custom_provider_repo import CustomProviderRepository

        await _seed_call(db_factory, "test-repriced", "image", "img", provider="custom-1", segment_id="E1S001")
        async with db_factory() as session:
            await CustomProviderRepository(session).create_provider(
                display_name="Relay",
                discovery_format="openai",
                base_url="https://api.example.com",
                api_key="k",
                models=[
                    {
                        "model_id": "img",
                        "display_name": "Img",
                        "endpoint": "openai-images",
                        "price_unit": "image",
                        "price_input": 0.05,
                        "currency": "USD",
                    }
                ],
            )
            await session.commit()

        result = await CostEstimationService(ConfigResolver(db_factory), db_factory).compute(
            {"title": "Test", "content_mode": "drama", "episodes": []}, {}, project_name="test-repriced"
        )

        assert result["unpriced"]["actual"] == []

    @pytest.mark.parametrize(
        ("generated", "call", "expected"),
        [
            ("asset", None, True),
            ("storyboard", None, True),
            ("asset", ("image", "success"), False),
            # 文本调用与失败的媒体调用都算不出产物的费用，不能让未知的已花变成真实的 0
            ("asset", ("text", "success"), True),
            ("asset", ("image", "failed"), True),
            (None, None, False),
        ],
    )
    async def test_missing_local_calls_flags_generated_media_without_call_records(
        self, db_factory, generated, call, expected
    ):
        """没有本机媒体调用记录（如导入的项目）时实际费用无从统计；什么都还没生成的新项目则是真实的 0。"""
        project_name = "test-imported"
        if call is not None:
            call_type, status = call
            async with db_factory() as session:
                repo = UsageRepository(session)
                call_id = await repo.start_call(
                    project_name=project_name, call_type=call_type, model="m", provider=PROVIDER_GEMINI
                )
                await repo.finish_call(call_id, status=status, settlement=SettlementInput())
        script = _make_script(
            1,
            ["E1S001"],
            [6],
            [{"storyboard_image": "storyboards/E1S001.png"}] if generated == "storyboard" else None,
        )
        project_data = {
            "title": "Test",
            "content_mode": "drama",
            "characters": {"A": {"character_sheet": "characters/A.png" if generated == "asset" else ""}},
            "episodes": [{"episode": 1, "title": "Ep1", "script_file": "ep1.json"}],
        }

        result = await CostEstimationService(ConfigResolver(db_factory), db_factory).compute(
            project_data, {"ep1.json": script}, project_name=project_name
        )

        assert result["missing_local_calls"] is expected
