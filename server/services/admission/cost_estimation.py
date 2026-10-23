"""费用估算服务 — 计算预估 + 汇总实际费用。"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lib.billing.cost_calculator import cost_calculator
from lib.billing.pricing.strategies import PricingParams
from lib.config.image_resolution import resolve_image_resolution
from lib.config.resolver import (
    ConfigResolver,
    VideoGenerationType,
    video_bucket_for_generation_mode,
)
from lib.custom_provider import is_custom_provider
from lib.db.repositories.custom_provider_repo import CustomProviderPrice, CustomProviderRepository
from lib.db.repositories.usage_repo import PROJECT_LEVEL_SEGMENT_KEY, UsageRepository
from lib.generation.video_request_facts import (
    CONFIGURED_VIDEO_IDENTITY,
    VideoRequestCostFacts,
    VideoRequestFacts,
    VideoRequestFactsFailure,
    evaluate_video_request_facts,
)
from lib.infra.schema_guards import is_int
from lib.project.asset_types import ASSET_SPECS
from lib.script.script_editor import ScriptEditError
from lib.script.script_models import get_generated_assets
from lib.script.storyboard_sequence import get_storyboard_items
from lib.speech.narration_config import USE_TTS, project_narration_delivery, project_tts_settings
from lib.speech.narration_delivery import canonical_narration_text
from lib.speech.speech_composition import admit_script_unit

logger = logging.getLogger(__name__)

CostBreakdown = dict[str, float]
ActualBySegment = dict[str, dict[str, CostBreakdown]]
#: 没有价格的预估项或调用，按 (call_type, provider, model) 计数。
UnpricedCounts = Counter[tuple[str, str, str]]
# 费用页展示的记账类型；text 类调用不写 segment_id，只会落在项目级汇总里。
ACTUAL_COST_TYPES = ("image", "video", "audio")


#: 读侧定桶要枚举的全部视频任务类型桶。分镜图生视频项目整体走 i2v 桶；参考生视频由公共
#: request projection 按每个 unit 当前实际可用资产分桶。两个桶都在这里预解析，省去按
#: 生成模式与分镜分支判断该解析哪个桶的复杂度——桶只有两个，代价有界。
_VIDEO_BUCKETS: tuple[VideoGenerationType, ...] = ("i2v", "r2v")

#: 普通分镜图取不到分辨率档时的计价档。执行侧此路径把 ``None`` 原样下发给 backend、由其自行定档
#: （不像宫格有 ``GRID_FALLBACK_RESOLUTION`` 这一确定的保底档），估价无从同源，只能取最低档保守
#: 计价——宁可低估未配置供应商的项目，也不拿高档单价虚报。取值与分档策略自身的缺省档一致（见
#: ``lib.billing.pricing.strategies``），显式写出是为了让估价侧的保底口径可读、不随策略层缺省漂移。
_IMAGE_PRICING_FALLBACK_RESOLUTION = "1K"

#: 剧本条目 ``generated_assets`` 里代表已生成媒体的字段，用于识别「有产物却没有本机调用记录」。
_GENERATED_MEDIA_FIELDS = ("storyboard_image", "video_clip", "narration_audio")
_SCRIPT_ITEM_KEYS = ("segments", "scenes", "shots", "video_units")


@dataclass(frozen=True)
class _VideoPricing:
    """一个任务类型桶下的视频计价参数——解析出的模型身份、分辨率、有效 generate_audio 与自定义单价。

    五项总是结伴传给三条估算路径，且必须同源于一次解析：分辨率与 generate_audio 都按模型身份
    求值，混用不同桶的分项会算出任何一个模型都不会产生的价。
    """

    provider: str
    model: str | None
    resolution: str | None
    generate_audio: bool
    price: Any


@dataclass(frozen=True, slots=True)
class VideoRequestQuote:
    """Exact current price for one projected provider video request."""

    amount: float
    currency: str
    provider_id: str
    model_id: str
    request_duration_seconds: int

    def to_payload(self) -> dict[str, object]:
        return {
            "amount": self.amount,
            "currency": self.currency,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "request_duration_seconds": self.request_duration_seconds,
        }


def quote_video_request_from_price(
    facts: VideoRequestCostFacts,
    price: Any,
) -> VideoRequestQuote:
    """Price one request using the same calculator and custom-price coordinates as cost estimation."""

    amount, currency = cost_calculator.calculate_cost(
        facts.provider_id,
        PricingParams(
            call_type="video",
            model=facts.model_id,
            resolution=facts.resolution,
            duration_seconds=facts.duration_seconds,
            generate_audio=facts.generate_audio,
        ),
        custom_price_input=price.price_input,
        custom_price_output=price.price_output,
        custom_currency=price.currency,
        estimate_only=True,
    )
    return VideoRequestQuote(
        amount=round(amount, 6),
        currency=currency,
        provider_id=facts.provider_id,
        model_id=facts.model_id,
        request_duration_seconds=facts.duration_seconds,
    )


async def quote_video_request(
    facts: VideoRequestCostFacts,
    session_factory: async_sessionmaker[AsyncSession],
) -> VideoRequestQuote | None:
    """Resolve current pricing and quote a projected video request."""

    try:
        async with session_factory() as session:
            price = await CustomProviderRepository(session).resolve_price(facts.provider_id, facts.model_id)
        return quote_video_request_from_price(facts, price)
    except (SQLAlchemyError, ValueError):
        logger.warning(
            "无法为 current video request 计算精确费用 provider=%s model=%s duration=%s",
            facts.provider_id,
            facts.model_id,
            facts.duration_seconds,
            exc_info=True,
        )
        return None


def _price_missing(provider: str, price: CustomProviderPrice | None) -> bool:
    """模型没有价格：自定义供应商的模型未设单价。内置供应商总能查到定价声明，不会缺价。

    价格设为 0 是用户声明的真实 0，不算缺价。
    """
    return is_custom_provider(provider) and (price is None or price.price_input is None)


def _has_generated_media(project: dict[str, Any], scripts: dict[str, dict[str, Any]]) -> bool:
    """项目里是否已有生成出的资产图、分镜图、视频或旁白配音。"""
    for spec in ASSET_SPECS.values():
        bucket = project.get(spec.bucket_key)
        if isinstance(bucket, dict) and any(
            isinstance(asset, dict) and asset.get(spec.sheet_field) for asset in bucket.values()
        ):
            return True
    for script in scripts.values():
        for key in _SCRIPT_ITEM_KEYS:
            items = script.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and any(
                    get_generated_assets(item).get(field) for field in _GENERATED_MEDIA_FIELDS
                ):
                    return True
    return False


def _unpriced_payload(counts: UnpricedCounts, provider_names: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "call_type": call_type,
            "provider": provider,
            "provider_name": provider_names.get(provider, provider),
            "model": model,
            "count": count,
        }
        for (call_type, provider, model), count in sorted(counts.items())
    ]


def _add_cost(target: CostBreakdown, amount: float, currency: str) -> None:
    if amount <= 0:
        return
    target[currency] = round(target.get(currency, 0) + amount, 6)


def _merge_breakdowns(a: CostBreakdown, b: CostBreakdown) -> CostBreakdown:
    merged = dict(a)
    for cur, amt in b.items():
        merged[cur] = round(merged.get(cur, 0) + amt, 6)
    return merged


def _claim_actual(
    actual_by_segment: ActualBySegment,
    claimed: set[tuple[str, str]],
    segment_id: str,
    cost_types: tuple[str, ...] = ACTUAL_COST_TYPES,
) -> dict[str, CostBreakdown]:
    """认领一份 segment 实付；同一 (记账 key, 类型) 在一次估算中最多返回一次。

    认领粒度到类型而非整条 key：调用方只消费其中一部分类型时，剩下的仍是未认领状态，
    由兜底聚合收进「未归属」，不会被整条认领吞掉。
    """
    if not segment_id:
        return {}
    actual = actual_by_segment.get(segment_id, {})
    claimed_now: dict[str, CostBreakdown] = {}
    for cost_type in cost_types:
        if (segment_id, cost_type) in claimed:
            continue
        claimed.add((segment_id, cost_type))
        amounts = actual.get(cost_type)
        if amounts:
            claimed_now[cost_type] = amounts
    return claimed_now


def _split_cost_across(cost: CostBreakdown, parts: int) -> list[CostBreakdown]:
    """把一笔按整体计费的费用均摊成 ``parts`` 份，除不尽的余数补给最后一份。

    补余数是为了让分摊结果的合计与原值分文不差：调用方按分摊后的份额累加集/项目合计，
    若每份都独立 round，误差会随分镜数放大到用户可见的总价上。
    """
    if parts <= 0:
        return []
    split: list[CostBreakdown] = [{} for _ in range(parts)]
    for currency, amount in cost.items():
        share = round(amount / parts, 6)
        for bucket in split[:-1]:
            bucket[currency] = share
        split[-1][currency] = round(amount - share * (parts - 1), 6)
    return split


class CostEstimationService:
    def __init__(
        self,
        resolver: ConfigResolver,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        project_path: Path | None = None,
    ) -> None:
        self._resolver = resolver
        self._session_factory = session_factory
        self._project_path = project_path

    async def compute(
        self,
        project_data: dict[str, Any],
        scripts: dict[str, dict[str, Any]],
        *,
        project_name: str,
    ) -> dict[str, Any]:
        episodes_meta = project_data.get("episodes", [])

        # Resolve current model config（共享单一 session）。估价以 T2I 为准（T2I/I2I 是正交能力槽，
        # T2I 缺失不应回落 I2I —— 那会拿错误能力的价目算费用）。
        # image/video 的项目覆盖优先级由 ConfigResolver 统一解析，与执行路径共用同一套
        # payload>project>全局默认 链路，此处 payload 传 None（预估无历史任务 payload 可排空）。
        async with self._resolver.session() as r:
            storyboard_facts = await evaluate_video_request_facts(
                project_data,
                route="storyboard",
                generation_type="i2v",
                identity=CONFIGURED_VIDEO_IDENTITY,
                resolver=r,
            )
            try:
                resolved_image = await r.resolve_image_backend(project_data, None, generation_type="t2i")
                image_provider, image_model = resolved_image.provider_id, resolved_image.model_id
            except (ValueError, SQLAlchemyError):
                image_provider, image_model = "unknown", "unknown"

            # T2I 槽分辨率档：与路由入队、Agent 工具共用 ``grid_resolution`` 的取档，估算的宫格
            # 张数才不会与实际入队张数漂移；同一档位又是两路分镜图的计价档——宫格图未配置时回落
            # ``GRID_FALLBACK_RESOLUTION``（与 ``execute_grid_task`` 下发的保底档同源），普通
            # 分镜图未配置时回落 ``_IMAGE_PRICING_FALLBACK_RESOLUTION``。解析在两路之前，宫格
            # 与非宫格项目共用这一次 IO。计价与执行取的是同一个 T2I 槽、同一套项目配置，但身份
            # 键不同（此处 registry ``model_id``，执行侧构造后的 backend 型号），两者分叉的供应
            # 商上估价档位可能与实际渲染档位不一致。
            image_resolution = await resolve_image_resolution(r, project_data)

            # 视频按任务类型桶解析（``docs/adr/0054``），与执行扣费同一个模型：图生视频 / 宫格算
            # i2v 桶的价；参考生视频逐 unit 水合当前资产后分桶。两个桶都在这里
            # 解析出来（见 ``_VIDEO_BUCKETS``），分辨率与
            # generate_audio 随各自的模型身份求值。
            video_identity: dict[VideoGenerationType, tuple[str, str, str | None, bool]] = {}
            for generation_type in _VIDEO_BUCKETS:
                bucket_facts = None
                if generation_type == "i2v" and isinstance(storyboard_facts, VideoRequestFacts):
                    bucket_facts = storyboard_facts
                if bucket_facts is not None:
                    bucket_provider = bucket_facts.provider_id
                    bucket_model = bucket_facts.model_id
                    bucket_resolution = bucket_facts.resolution
                    bucket_audio = bucket_facts.generate_audio
                else:
                    try:
                        resolved_video = await r.resolve_video_backend(
                            project_data, None, generation_type=generation_type
                        )
                        bucket_provider, bucket_model = resolved_video.provider_id, resolved_video.model_id
                    except (ValueError, SQLAlchemyError):
                        bucket_provider, bucket_model = "unknown", "unknown"
                    # 事实解析不出的桶只保留模型展示坐标；分镜按分镜返回阻断问题，参考生视频逐 unit
                    # 的阻断由 request projector 给出，两者都不产生视频报价。
                    bucket_audio = await r.video_pricing_generate_audio(bucket_provider, bucket_model, project_data)
                    try:
                        bucket_resolution = await r.resolve_resolution(
                            project_data,
                            bucket_provider,
                            bucket_model or "",
                        )
                    except (ValueError, SQLAlchemyError):
                        bucket_resolution = None
                video_identity[generation_type] = (
                    bucket_provider,
                    bucket_model,
                    bucket_resolution,
                    bucket_audio,
                )

            # 旁白配音（TTS）模型取项目快照，不读全局默认；后期配音项目或没有快照时回落 unknown，
            # 该维度预估为空
            tts_settings = (
                project_tts_settings(project_data) if project_narration_delivery(project_data) == USE_TTS else None
            )
            audio_provider, audio_model = (
                (tts_settings.provider_id, tts_settings.model_id)
                if tts_settings is not None
                else ("unknown", "unknown")
            )

        # Get actual costs + 自定义供应商价格（缺则预估恒为零，需与实际记账同源预查 DB 单价）
        async with self._session_factory() as session:
            actual_by_segment = await UsageRepository(session).get_actual_costs_by_segment(project_name)
            custom_repo = CustomProviderRepository(session)
            image_price = await custom_repo.resolve_price(image_provider, image_model)
            audio_price = await custom_repo.resolve_price(audio_provider, audio_model)
            # 两个桶常解析到同一个模型，按身份去重后再查单价，不重复打 DB。
            video_prices: dict[tuple[str, str], Any] = {}
            for bucket_provider, bucket_model, _, _ in video_identity.values():
                if (bucket_provider, bucket_model) not in video_prices:
                    video_prices[(bucket_provider, bucket_model)] = await custom_repo.resolve_price(
                        bucket_provider, bucket_model
                    )

        video_pricing: dict[VideoGenerationType, _VideoPricing] = {
            generation_type: _VideoPricing(
                provider=bucket_provider,
                model=bucket_model,
                resolution=bucket_resolution,
                generate_audio=bucket_audio,
                price=video_prices[(bucket_provider, bucket_model)],
            )
            for generation_type, (
                bucket_provider,
                bucket_model,
                bucket_resolution,
                bucket_audio,
            ) in video_identity.items()
        }
        # 项目层展示的视频模型按项目 generation_mode 定桶：``models`` 回答的是「当前项目配置」
        # 的生成模式主桶；参考生视频内无参考图视频单元的逐 unit 降级计价在集级估算路径内完成，不改变项目层
        # 展示身份。
        project_video = video_pricing[video_bucket_for_generation_mode(project_data.get("generation_mode"))]

        # 预计算图片单价
        image_unit_cost: tuple[float, str] | None = None
        try:
            image_unit_cost = cost_calculator.calculate_cost(
                image_provider,
                PricingParams(
                    call_type="image",
                    model=image_model,
                    resolution=image_resolution or _IMAGE_PRICING_FALLBACK_RESOLUTION,
                ),
                custom_price_input=image_price.price_input,
                custom_price_output=image_price.price_output,
                custom_currency=image_price.currency,
            )
        except ValueError:
            logger.debug("无法计算 image 预估单价", exc_info=True)

        episodes_result = []
        proj_est: dict[str, CostBreakdown] = {}
        # 应当有预估、却因模型没有价格而没算进去的项；与实际侧一起让前端区分「0」与「未知」
        estimate_unpriced: UnpricedCounts = Counter()
        image_unpriced = _price_missing(image_provider, image_price)
        # 不用 TTS 时旁白不产生配音调用，audio 维度的估算本就为空，不算缺价
        audio_unpriced = tts_settings is not None and _price_missing(audio_provider, audio_price)
        proj_act: dict[str, CostBreakdown] = {}
        claimed_actual: set[tuple[str, str]] = set()

        def _accumulate_episode(
            ep_meta: dict[str, Any],
            segments_result: list[dict[str, Any]],
            ep_est: dict[str, CostBreakdown],
            ep_act: dict[str, CostBreakdown],
        ) -> None:
            """收下一集的估算结果并并入项目级合计（两条估算路径共用的收尾）。"""
            episodes_result.append(
                {
                    "episode": ep_meta.get("episode"),
                    "title": ep_meta.get("title", ""),
                    "segments": segments_result,
                    "totals": {"estimate": ep_est, "actual": ep_act},
                }
            )
            for cost_type in ("image", "video", "audio"):
                proj_est[cost_type] = _merge_breakdowns(proj_est.get(cost_type, {}), ep_est.get(cost_type, {}))
                proj_act[cost_type] = _merge_breakdowns(proj_act.get(cost_type, {}), ep_act.get(cost_type, {}))

        # 参考生视频路径跳过分镜步骤，所有创作类型都按自包含 reference_unit 计费与展示。
        #
        # 生成路径以项目生成模式为唯一真相源，整个项目同一种生成模式、逐集不变（剧本不携带生成模式信息）；
        # 参考生视频内的定桶再由 request projection 按当前资产逐 unit 分流。
        for ep_meta in episodes_meta:
            script_file = ep_meta.get("script_file", "")
            script = scripts.get(script_file)
            if not script:
                continue

            episode_video = video_pricing["i2v"]

            try:
                raw_segments, id_key, _, _, _ = get_storyboard_items(script)
                historical_units = project_data.get("generation_mode") == "reference_video"
                if historical_units and isinstance(script.get("video_units"), list):
                    raw_segments, id_key = script["video_units"], "unit_id"
            except ScriptEditError as exc:
                # 单集脏脚本(segments/scenes 键损坏)不应让整个项目费用估算 5xx;降级把该集
                # 估算为 0(raw_segments=[]) + warning 让运维知道,UI 仍能展示其他正常集的估算。
                logger.warning("费用估算跳过脏脚本 %s: %s", script_file, exc)
                historical_units = project_data.get("generation_mode") == "reference_video"
                raw_segments, id_key = [], "segment_id"
                if historical_units and isinstance(script.get("video_units"), list):
                    raw_segments, id_key = script["video_units"], "unit_id"

            # --- Grid actual cost apportionment ---
            # 均摊份额以条目在 raw_segments 中的位置为身份，而非条目 ID：ADR 0053 接受一张
            # 宫格覆盖的多个条目共用同一 ID，位置唯一而 ID 不唯一，只有按位置组织才能让每个
            # 条目（含同 ID 条目）恰好消费一次自己的份额。
            grid_to_indices: dict[str, list[int]] = {}
            for idx, seg in enumerate(raw_segments):
                gid = get_generated_assets(seg).get("grid_id")
                if gid and seg.get(id_key, ""):
                    grid_to_indices.setdefault(gid, []).append(idx)

            # 逐宫格算出每个位置的份额；``_split_cost_across`` 的余数补偿保证各份之和与冻结
            # 实付分文不差。
            grid_actual_per_index: dict[int, CostBreakdown] = {}
            for gid, indices in grid_to_indices.items():
                grid_cost = _claim_actual(actual_by_segment, claimed_actual, gid, ("image",)).get("image", {})
                if grid_cost:
                    grid_actual_per_index.update(zip(indices, _split_cost_across(grid_cost, len(indices)), strict=True))

            segments_result = []
            ep_est: dict[str, CostBreakdown] = {}
            ep_act: dict[str, CostBreakdown] = {}

            for idx, seg in enumerate(raw_segments):
                seg_id = seg.get(id_key, "")
                duration = seg.get("duration_seconds", 8)

                est_image: CostBreakdown = {}
                est_video: CostBreakdown = {}
                est_audio: CostBreakdown = {}

                if image_unit_cost and not historical_units:
                    _add_cost(est_image, image_unit_cost[0], image_unit_cost[1])
                if image_unpriced and not historical_units:
                    estimate_unpriced["image", image_provider, image_model] += 1

                # 剧本上的秒数可能被外部编辑成非整数：单条脏数据只让该分镜没有视频报价，不进计价。
                if (
                    not historical_units
                    and isinstance(storyboard_facts, VideoRequestFacts)
                    and is_int(duration, minimum=1)
                ):
                    if _price_missing(storyboard_facts.provider_id, episode_video.price):
                        estimate_unpriced["video", storyboard_facts.provider_id, storyboard_facts.model_id] += 1
                    try:
                        video_quote = quote_video_request_from_price(
                            VideoRequestCostFacts(storyboard_facts, duration), episode_video.price
                        )
                        _add_cost(est_video, video_quote.amount, video_quote.currency)
                    except ValueError:
                        logger.debug("无法计算 video 预估 for %s", seg_id, exc_info=True)

                # 工厂旁白按规范发声准备取正文；历史旁白保留原字段的费用读取口径。
                if id_key == "scene_id":
                    narration_text = canonical_narration_text(admit_script_unit("scenes", seg).preparation)
                else:
                    narration_text = seg.get("novel_text")
                narration_chars = len(narration_text.strip()) if isinstance(narration_text, str) else 0
                if narration_chars:
                    if audio_unpriced:
                        estimate_unpriced["audio", audio_provider, audio_model] += 1
                    try:
                        audio_amount, audio_currency = cost_calculator.calculate_cost(
                            audio_provider,
                            PricingParams(call_type="audio", model=audio_model, usage_tokens=narration_chars),
                            custom_price_input=audio_price.price_input,
                            custom_price_output=audio_price.price_output,
                            custom_currency=audio_price.currency,
                        )
                        _add_cost(est_audio, audio_amount, audio_currency)
                    except ValueError:
                        logger.debug("无法计算 audio 预估 for %s", seg_id, exc_info=True)

                seg_actual = _claim_actual(actual_by_segment, claimed_actual, seg_id)
                act_image: CostBreakdown = seg_actual.get("image", {})
                if idx in grid_actual_per_index:
                    act_image = _merge_breakdowns(act_image, grid_actual_per_index[idx])
                act_video: CostBreakdown = seg_actual.get("video", {})
                act_audio: CostBreakdown = seg_actual.get("audio", {})

                segments_result.append(
                    {
                        "segment_id": seg_id,
                        "duration_seconds": duration,
                        **(
                            {
                                "request_projection": {
                                    "allowed": False,
                                    "problems": [
                                        {
                                            **storyboard_facts.problem_payload(),
                                            "blocking": True,
                                            "unit_id": seg_id,
                                            "locations": [{"path": ["video_provider_i2v"], "line": None}],
                                        }
                                    ],
                                }
                            }
                            if isinstance(storyboard_facts, VideoRequestFactsFailure)
                            else {}
                        ),
                        "estimate": {"image": est_image, "video": est_video, "audio": est_audio},
                        "actual": {"image": act_image, "video": act_video, "audio": act_audio},
                    }
                )

                seg_est_by_type = {"image": est_image, "video": est_video, "audio": est_audio}
                seg_act_by_type = {"image": act_image, "video": act_video, "audio": act_audio}
                for cost_type in ("image", "video", "audio"):
                    ep_est[cost_type] = _merge_breakdowns(
                        ep_est.get(cost_type, {}),
                        seg_est_by_type[cost_type],
                    )
                    ep_act[cost_type] = _merge_breakdowns(
                        ep_act.get(cost_type, {}),
                        seg_act_by_type[cost_type],
                    )

            _accumulate_episode(ep_meta, segments_result, ep_est, ep_act)

        # 当前剧本没有认领到的历史记账仍是真实支出。规范 segment/unit ID 自带 E{n}
        # 前缀，可回填对应集；无法识别或对应集已不存在的记录仍纳入项目合计。
        episodes_by_number = {ep["episode"]: ep for ep in episodes_result}
        # 剧本文件缺失或尚未生成的集不进入估算结果，但它的历史支出仍属于这一集。按需补一条
        # 只含实付的集结果，让这笔钱显示在集行上，而不是静默退到项目合计。
        meta_by_number = {ep_meta.get("episode"): ep_meta for ep_meta in episodes_meta}

        def _attribution_target(episode_number: int) -> dict[str, Any] | None:
            existing = episodes_by_number.get(episode_number)
            if existing is not None:
                return existing
            ep_meta = meta_by_number.get(episode_number)
            if ep_meta is None:
                return None
            created: dict[str, Any] = {
                "episode": episode_number,
                "title": ep_meta.get("title", ""),
                "segments": [],
                "totals": {"estimate": {}, "actual": {}},
            }
            episodes_result.append(created)
            episodes_by_number[episode_number] = created
            return created

        for segment_id, actual_by_type in actual_by_segment.items():
            if segment_id == PROJECT_LEVEL_SEGMENT_KEY:
                continue
            match = re.match(r"^E(\d+)(?:S|U)", segment_id)
            for cost_type in ACTUAL_COST_TYPES:
                amounts = actual_by_type.get(cost_type, {})
                if not amounts or (segment_id, cost_type) in claimed_actual:
                    continue
                proj_act["unassigned"] = _merge_breakdowns(proj_act.get("unassigned", {}), amounts)
                episode_result = _attribution_target(int(match.group(1))) if match else None
                if episode_result is not None:
                    episode_actual = episode_result["totals"]["actual"]
                    episode_actual["unassigned"] = _merge_breakdowns(
                        episode_actual.get("unassigned", {}),
                        amounts,
                    )
        # 补出来的集结果追加在末尾，重排回 project.json 的集顺序，让费用页集行不跳序。
        meta_order = {ep_meta.get("episode"): i for i, ep_meta in enumerate(episodes_meta)}
        episodes_result.sort(key=lambda ep: meta_order.get(ep["episode"], len(meta_order)))

        # Project-level actual costs (characters/scenes/props/products 资产图—— segment_id is null)
        async with self._session_factory() as session:
            usage_repo = UsageRepository(session)
            project_image_by_type = await usage_repo.get_project_image_costs_by_asset_type(project_name)
            # 零费用的成功调用里，模型至今没有价格的算未计价；之后设了价格（含 0）的不再提示——
            # 历史调用不会按新价格补记，提示的用途是引导设置价格。
            actual_unpriced: UnpricedCounts = Counter()
            price_repo = CustomProviderRepository(session)
            for call_type, provider, model, calls in await usage_repo.get_zero_cost_calls_by_model(project_name):
                if _price_missing(provider, await price_repo.resolve_price(provider, model)):
                    actual_unpriced[call_type, provider, model] += calls
            provider_names = await usage_repo.provider_display_names(
                {provider for _, provider, _ in (*estimate_unpriced, *actual_unpriced)}
            )
            # 有产物却没有本机的媒体调用记录（如导入的项目、在别的机器上生成）：实际费用无从统计
            missing_local_calls = not await usage_repo.has_media_calls(project_name) and _has_generated_media(
                project_data, scripts
            )
        for asset_type in ("characters", "scenes", "props", "products"):
            bucket = project_image_by_type.get(asset_type)
            if bucket:
                proj_act[asset_type] = bucket
        # segment_id 为空的记账里，资产图已按类型单列，剩下的仍是真实支出：无法按 output_path
        # 归类的图，以及 segment_id 列回填前的历史 video/audio 行。它们没有集归属线索，只并入
        # 项目级「未归属」。
        project_level_actual = actual_by_segment.get(PROJECT_LEVEL_SEGMENT_KEY, {})
        for amounts in (
            project_image_by_type.get("other", {}),
            project_level_actual.get("video", {}),
            project_level_actual.get("audio", {}),
        ):
            if amounts:
                proj_act["unassigned"] = _merge_breakdowns(proj_act.get("unassigned", {}), amounts)

        return {
            "project_name": project_name,
            "models": {
                "image": {"provider": image_provider, "model": image_model},
                "video": {"provider": project_video.provider, "model": project_video.model},
                "audio": {"provider": audio_provider, "model": audio_model},
            },
            "episodes": episodes_result,
            "project_totals": {"estimate": proj_est, "actual": proj_act},
            "unpriced": {
                "estimate": _unpriced_payload(estimate_unpriced, provider_names),
                "actual": _unpriced_payload(actual_unpriced, provider_names),
            },
            "missing_local_calls": missing_local_calls,
        }


ImageLane = Literal["t2i", "i2i"]


async def estimate_image_batch_cost(
    project: dict[str, Any],
    lanes: Sequence[ImageLane],
    *,
    resolver: ConfigResolver,
    session_factory: async_sessionmaker[AsyncSession],
) -> CostBreakdown | None:
    """一批图片生成按当前项目配置的预估费用；供应商或单价解析不出时返回 ``None``（算不出）。

    每张图按自己的生图通道计价：文生图与图生图是两个正交的能力槽，可能配置成不同的模型。
    分辨率档与执行侧同样按通道解析出的模型取，取不到时按保底档计价。
    """

    counts = Counter(lanes)
    if not counts:
        return {}
    identities: dict[ImageLane, tuple[str, str, str | None]] = {}
    try:
        async with resolver.session() as r:
            for lane in counts:
                resolved = await r.resolve_image_backend(project, None, generation_type=lane)
                try:
                    resolution = await r.resolve_resolution(project, resolved.provider_id, resolved.model_id or "")
                except (ValueError, SQLAlchemyError):
                    resolution = None
                identities[lane] = (resolved.provider_id, resolved.model_id or "", resolution)
        total: CostBreakdown = {}
        async with session_factory() as session:
            repo = CustomProviderRepository(session)
            for lane, count in counts.items():
                provider, model, resolution = identities[lane]
                price = await repo.resolve_price(provider, model)
                amount, currency = cost_calculator.calculate_cost(
                    provider,
                    PricingParams(
                        call_type="image",
                        model=model,
                        resolution=resolution or _IMAGE_PRICING_FALLBACK_RESOLUTION,
                    ),
                    custom_price_input=price.price_input,
                    custom_price_output=price.price_output,
                    custom_currency=price.currency,
                    estimate_only=True,
                )
                _add_cost(total, amount * count, currency)
    except (ValueError, SQLAlchemyError):
        logger.debug("无法估算这批图片的费用", exc_info=True)
        return None
    return total
