"""导入原文：篇幅档位、结构校验、前序集声明与原文/meta 读写。

导入原文不新增剧本格式：生成结果必须能被 :func:`lib.script.short_drama_import.parse_short_drama_episode`
解析成动作/台词 beat，再交给 AI 分镜消费。本模块只做纯解析、校验与文件读写，不调用模型、
不取项目锁——调用方在 ``ProjectManager.locked_source_registration`` 内调用写函数。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lib.infra.json_io import atomic_write_bytes, load_json_or_none
from lib.project.asset_types import asset_name_comparison_key
from lib.script.short_drama_import import (
    ParsedEpisode,
    parse_short_drama_episode,
    referenced_asset_descriptions,
)
from lib.script.short_drama_source import (
    ShortDramaSourceConflict,
    read_short_drama_source_markdown,
    short_drama_source_relative_path,
)

#: 单集原文上限（字符数）：与前端 ``SOURCE_IMPORT_MAX_CHARS`` 同一口径。
MAX_SOURCE_CHARS = 50_000

#: 短原文放宽 beat 下限的阈值：梗概类短文本很难写满 10 个 beat。
SHORT_SOURCE_CHARS = 1_500

#: 连续性上下文最多回看的前序集数；更早的声明只作兜底，不无限增长提示词。
MAX_CONTINUITY_EPISODES = 5

#: 原文与 meta 的相对目录（相对 ``source/`` 或项目根）。
ORIGINAL_DIR = "source/import"

#: 结构校验认得的头部字段；不在表内的 ``【…】`` 行会被判为自造字段。
HEADER_LABELS: tuple[str, ...] = (
    "所属阶段",
    "黄金前3秒钩子",
    "情绪流变",
    "本集时长",
    "本集结构",
    "本集中段小钩",
    "本集断章卡点",
)


@dataclass(frozen=True, slots=True)
class SourceImportTier:
    """篇幅档位：与前端 ``formatForDuration`` 同一分档（default / long / manju）。"""

    name: str
    label: str
    target_seconds: int
    min_seconds: int
    max_seconds: int
    max_scenes: int
    min_dialogue_lines: int
    requires_mid_hook: bool


_STANDARD_TIER = SourceImportTier(
    name="standard",
    label="标准档 90~120 秒",
    target_seconds=105,
    min_seconds=90,
    max_seconds=120,
    max_scenes=2,
    min_dialogue_lines=4,
    requires_mid_hook=False,
)
_LONG_TIER = SourceImportTier(
    name="long",
    label="长档 165~195 秒（约 3 分钟，双回合 + 中段小钩）",
    target_seconds=180,
    min_seconds=165,
    max_seconds=195,
    max_scenes=3,
    min_dialogue_lines=8,
    requires_mid_hook=True,
)
_MANJU_TIER = SourceImportTier(
    name="manju",
    label="漫剧档 60~90 秒",
    target_seconds=75,
    min_seconds=60,
    max_seconds=90,
    max_scenes=2,
    min_dialogue_lines=4,
    requires_mid_hook=False,
)


def derive_tier(episode_target_duration: int | None) -> SourceImportTier:
    """按项目单集目标时长推导档位，并给出落在档位区间内的提示词目标秒数。

    未设目标时长按标准档 105 秒；``>=150`` 长档、``<=90`` 漫剧档，其余标准档——与
    ``frontend/src/components/canvas/shared/ShortDramaGenerateDialog.tsx::formatForDuration`` 同口径。
    """

    if episode_target_duration is None:
        return _STANDARD_TIER
    if episode_target_duration >= 150:
        return _replace_target(_LONG_TIER, episode_target_duration)
    if episode_target_duration <= 90:
        return _replace_target(_MANJU_TIER, episode_target_duration)
    return _replace_target(_STANDARD_TIER, episode_target_duration)


def _replace_target(tier: SourceImportTier, target_seconds: int) -> SourceImportTier:
    return SourceImportTier(
        name=tier.name,
        label=tier.label,
        target_seconds=min(max(target_seconds, tier.min_seconds), tier.max_seconds),
        min_seconds=tier.min_seconds,
        max_seconds=tier.max_seconds,
        max_scenes=tier.max_scenes,
        min_dialogue_lines=tier.min_dialogue_lines,
        requires_mid_hook=tier.requires_mid_hook,
    )


@dataclass(frozen=True, slots=True)
class DeclaredAsset:
    """前序集声明的一条资产：名字 + 最早出现的外观描述（可能为空）。"""

    name: str
    appearance: str


@dataclass(frozen=True, slots=True)
class PreviousEpisodeContext:
    """前序集回看结果：声明按名字去重取最早外观，markdown 供机检跨集复读检测。"""

    characters: tuple[DeclaredAsset, ...]
    scenes: tuple[DeclaredAsset, ...]
    props: tuple[DeclaredAsset, ...]
    markdowns: tuple[str, ...]
    episode_numbers: tuple[int, ...]


EMPTY_PREVIOUS_CONTEXT = PreviousEpisodeContext((), (), (), (), ())


def collect_previous_episode_context(
    project_dir: Path,
    episodes: Sequence[Mapping[str, Any]],
    target_episode: int,
    *,
    max_episodes: int = MAX_CONTINUITY_EPISODES,
) -> PreviousEpisodeContext:
    """收集目标集之前（账本播出顺序）若干集的 ``[人物]/[场景]/[道具]`` 声明。

    预算不足时只取最近 ``max_episodes`` 集；同一资产按名字（比较键）去重，保留最早出现且非空的
    外观描述。没有账本条目或目标集不在账本里时返回空上下文。
    """

    numbers: list[int] = []
    for entry in episodes:
        number = entry.get("episode")
        if type(number) is int and number > 0:
            numbers.append(number)
    if target_episode not in numbers:
        return EMPTY_PREVIOUS_CONTEXT
    previous_numbers = numbers[: numbers.index(target_episode)][-max_episodes:]

    characters: dict[str, DeclaredAsset] = {}
    scenes: dict[str, DeclaredAsset] = {}
    props: dict[str, DeclaredAsset] = {}
    markdowns: list[str] = []
    for number in previous_numbers:
        markdown = read_short_drama_source_markdown(project_dir, number)
        if not markdown.strip():
            continue
        markdowns.append(markdown)
        parsed = parse_short_drama_episode(markdown)
        descriptions = referenced_asset_descriptions(parsed)
        _merge_declarations(
            characters,
            [(name, descriptions["character"].get(name, "")) for block in parsed.blocks for name in block.characters],
        )
        _merge_declarations(
            scenes,
            [(name, descriptions["scene"].get(name, "")) for block in parsed.blocks for name in block.locations],
        )
        _merge_declarations(
            props,
            [(name, descriptions["prop"].get(name, "")) for block in parsed.blocks for name in block.props],
        )
    return PreviousEpisodeContext(
        characters=tuple(characters.values()),
        scenes=tuple(scenes.values()),
        props=tuple(props.values()),
        markdowns=tuple(markdowns),
        episode_numbers=tuple(previous_numbers),
    )


def _merge_declarations(
    collected: dict[str, DeclaredAsset],
    entries: Sequence[tuple[str, str]],
) -> None:
    """同名资产保留最早的非空外观；只有后续集写了外观时才补上。"""

    for raw_name, appearance in entries:
        name = raw_name.strip()
        if not name:
            continue
        key = asset_name_comparison_key(name)
        current = collected.get(key)
        if current is None:
            collected[key] = DeclaredAsset(name=name, appearance=appearance.strip())
        elif not current.appearance and appearance.strip():
            collected[key] = DeclaredAsset(name=current.name, appearance=appearance.strip())


def validate_source_text(text: str) -> None:
    """原文长度与空值校验；失败以 ``ValueError``（带稳定 code）抛出，由服务层映射 HTTP。"""

    if not text.strip():
        raise ValueError("source_import_text_required")
    if len(text) > MAX_SOURCE_CHARS:
        raise ValueError("source_import_text_too_long")


def source_revision(text: str) -> str:
    """原文 revision：原文 UTF-8 字节的 sha256，与源稿 revision 同一算法。"""

    return _revision(text.encode("utf-8"))


def _revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class OriginalMeta:
    """原文对应的生成记录：判断「原文已改、剧本未重新生成」。"""

    generated_from_revision: str | None = None
    generated_at: str | None = None
    model: str | None = None
    provider: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "generated_from_revision": self.generated_from_revision,
            "generated_at": self.generated_at,
            "model": self.model,
            "provider": self.provider,
        }


@dataclass(frozen=True, slots=True)
class OriginalSource:
    """已落盘的原文与生成记录。"""

    text: str
    revision: str
    meta: OriginalMeta | None

    @property
    def generated_from_revision(self) -> str | None:
        return self.meta.generated_from_revision if self.meta is not None else None

    @property
    def stale(self) -> bool:
        """原文已改、剧本尚未按当前原文重生成。"""

        return self.generated_from_revision is not None and self.generated_from_revision != self.revision


def original_text_relative_path(episode: int) -> str:
    return f"{ORIGINAL_DIR}/episode_{episode:02d}.txt"


def original_meta_relative_path(episode: int) -> str:
    return f"{ORIGINAL_DIR}/episode_{episode:02d}.meta.json"


def read_original(project_dir: Path, episode: int) -> OriginalSource | None:
    """读取已保存的原文与 meta；原文不存在时返回 None。"""

    return read_original_at(project_dir / "source", episode)


def read_original_at(source_dir: Path, episode: int) -> OriginalSource | None:
    """以 ``source/`` 目录为根读取原文与 meta（锁内、锁外共用同一口径）。"""

    text_path = source_dir / "import" / f"episode_{episode:02d}.txt"
    try:
        text = text_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    meta = _read_meta(source_dir / "import" / f"episode_{episode:02d}.meta.json")
    return OriginalSource(text=text, revision=source_revision(text), meta=meta)


def _read_meta(path: Path) -> OriginalMeta | None:
    document = load_json_or_none(path)
    if not isinstance(document, dict):
        return None
    return OriginalMeta(
        generated_from_revision=_optional_str(document.get("generated_from_revision")),
        generated_at=_optional_str(document.get("generated_at")),
        model=_optional_str(document.get("model")),
        provider=_optional_str(document.get("provider")),
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def write_original_text(source_dir: Path, episode: int, text: str, undo: ExitStack) -> str:
    """在已持有的项目锁内写原文；返回新 revision。只写原文，不动 meta 与剧本。"""

    path = source_dir / "import" / f"episode_{episode:02d}.txt"
    raw = text.encode("utf-8")
    _stage_bytes(path, raw, undo)
    return _revision(raw)


def write_original_meta(source_dir: Path, episode: int, meta: OriginalMeta, undo: ExitStack) -> None:
    """在已持有的项目锁内写 meta。"""

    path = source_dir / "import" / f"episode_{episode:02d}.meta.json"
    payload = json.dumps(meta.as_dict(), ensure_ascii=False, indent=2).encode("utf-8")
    _stage_bytes(path, payload, undo)


def _stage_bytes(path: Path, raw: bytes, undo: ExitStack) -> None:
    """原子写文件，并把「恢复旧字节 / 删除新文件」登记进回滚栈。"""

    previous = path.read_bytes() if path.is_file() else None
    undo.callback(_restore, path, previous)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, raw)


def _restore(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write_bytes(path, previous)


def read_short_drama_source_revision(source_dir: Path, episode: int) -> str | None:
    """当前工厂源稿（``source/short_drama/episodes/episode_NN.md``）的 revision。"""

    path = source_dir / "short_drama" / "episodes" / f"episode_{episode:02d}.md"
    try:
        return _revision(path.read_bytes())
    except FileNotFoundError:
        return None


def write_short_drama_source_locked(
    source_dir: Path,
    episode: int,
    markdown: str,
    *,
    expected_revision: str | None,
    undo: ExitStack,
) -> str:
    """在已持有的项目锁内按 revision 比对写工厂源稿；返回新 revision。

    与 :func:`lib.script.short_drama_source.save_short_drama_source` 同一写入口径，但不自行取锁：
    导入原文的写事务已经把源稿、原文、meta 与账本登记放在同一把项目锁里。
    """

    path = source_dir / "short_drama" / "episodes" / f"episode_{episode:02d}.md"
    current: str | None = None
    if path.is_file():
        current = _revision(path.read_bytes())
    if current != expected_revision:
        raise ShortDramaSourceConflict(
            f"short drama source revision changed: expected={expected_revision!r}, actual={current!r}"
        )
    raw = markdown.encode("utf-8")
    _stage_bytes(path, raw, undo)
    return _revision(raw)


def source_path(episode: int) -> str:
    """工厂源稿的项目相对路径（复用唯一路径真相源）。"""

    return short_drama_source_relative_path(episode)


def validate_structure(
    markdown: str,
    *,
    episode: int,
    tier: SourceImportTier,
    source_chars: int,
) -> list[str]:
    """结构校验：不通过即阻塞落盘；返回可读的失败项（空列表 = 通过）。

    只校验「下游解析器与资产步骤能否消费」这一层：集号/头部/场景与人物声明/外观/台词下限/
    截断。篇幅体量与机检规则（E1-E14）不在这里阻断，由 :func:`lib.short_drama_quality.check_episode`
    出告警。
    """

    text = markdown.strip()
    if not text:
        return ["输出为空"]
    failures: list[str] = []
    if "```" in text:
        failures.append("输出包含代码块围栏（```），只能输出剧本 Markdown 本身")

    parsed = parse_short_drama_episode(text)
    if parsed.episode_number != episode:
        failures.append(f"集号应为第 {episode} 集，实际解析为第 {parsed.episode_number} 集")
    if not parsed.title:
        failures.append(f"缺少集标题行「第【{episode}】集：标题」")
    failures.extend(_header_failures(text, parsed, tier))

    blocks = parsed.blocks
    locations = _unique(location for block in blocks for location in block.locations if location)
    characters = _unique(name for block in blocks for name in block.characters if name)
    props = _unique(name for block in blocks for name in block.props if name)
    beats = [beat for block in blocks for beat in block.beats]
    dialogue_lines = sum(1 for beat in beats if beat.kind == "dialogue")

    if not locations:
        failures.append("缺少 [场景] 声明")
    elif len(locations) > tier.max_scenes:
        failures.append(f"场景数为 {len(locations)}，超过本档上限 {tier.max_scenes} 个")
    if not characters:
        failures.append("缺少 [人物] 声明")
    descriptions = referenced_asset_descriptions(parsed)
    failures.extend(
        f"人物「{name}」缺少「｜」后的外观描述"
        for name in characters
        if not descriptions["character"].get(name, "").strip()
    )
    failures.extend(
        f"道具「{name}」缺少「｜」后的外观描述" for name in props if not descriptions["prop"].get(name, "").strip()
    )

    min_beats = 6 if source_chars < SHORT_SOURCE_CHARS else 10
    if len(beats) < min_beats:
        failures.append(f"动作/台词拍数为 {len(beats)}，少于下限 {min_beats}")
    if dialogue_lines < tier.min_dialogue_lines:
        failures.append(f"台词行数为 {dialogue_lines}，少于本档下限 {tier.min_dialogue_lines}")
    failures.extend(_declaration_order_failures(text, parsed))
    return failures


def _declaration_order_failures(text: str, parsed: ParsedEpisode) -> list[str]:
    """场景/人物/道具声明必须集中在动作与台词之前，否则解析器会把它们当成新块。"""

    lines = [line.strip() for line in text.splitlines()]
    beat_lines = {beat.raw.strip() for block in parsed.blocks for beat in block.beats if beat.raw.strip()}
    first_beat = next((index for index, line in enumerate(lines) if line in beat_lines), None)
    if first_beat is None:
        return []
    misplaced = [
        line
        for index, line in enumerate(lines)
        if index > first_beat and line.startswith(("[场景]", "[人物]", "[道具]"))
    ]
    return ["场景/人物/道具声明必须集中在动作与台词之前"] if misplaced else []


def _header_failures(text: str, parsed: ParsedEpisode, tier: SourceImportTier) -> list[str]:
    failures: list[str] = []
    if not parsed.stage:
        failures.append("缺少头部字段【所属阶段】")
    if not parsed.opening_hook:
        failures.append("缺少头部字段【黄金前3秒钩子】")
    if not parsed.emotion_flow:
        failures.append("缺少头部字段【情绪流变】")
    if parsed.total_duration <= 0:
        failures.append("缺少或无法解析头部字段【本集时长】（需写成「【本集时长】：90 秒」）")
    if not parsed.cliffhanger:
        failures.append("缺少头部字段【本集断章卡点】")
    if tier.requires_mid_hook:
        if not parsed.structure:
            failures.append("长档缺少头部字段【本集结构】")
        if not parsed.mid_hook:
            failures.append("长档缺少头部字段【本集中段小钩】")
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("【") and not any(line.startswith(f"【{label}】") for label in HEADER_LABELS):
            failures.append(f"出现未识别的头部行：{line}")
    return failures


def _unique(values: Iterable[str]) -> list[str]:
    names: list[str] = []
    for value in values:
        if value not in names:
            names.append(value)
    return names


__all__ = [
    "EMPTY_PREVIOUS_CONTEXT",
    "HEADER_LABELS",
    "MAX_CONTINUITY_EPISODES",
    "MAX_SOURCE_CHARS",
    "ORIGINAL_DIR",
    "SHORT_SOURCE_CHARS",
    "DeclaredAsset",
    "OriginalMeta",
    "OriginalSource",
    "PreviousEpisodeContext",
    "SourceImportTier",
    "collect_previous_episode_context",
    "derive_tier",
    "original_meta_relative_path",
    "original_text_relative_path",
    "read_original",
    "read_original_at",
    "read_short_drama_source_revision",
    "source_path",
    "source_revision",
    "validate_source_text",
    "validate_structure",
    "write_original_meta",
    "write_original_text",
    "write_short_drama_source_locked",
]
