"""标准分镜提示词的封闭词表与格式闸（「方案设计/3. AI 分镜方案.md」D-3 / D-10 统一标准）。

词表与校验口径只有这一份真相源，同时供三条写入路径使用：AI 分镜的规划指令与规划校验、
提示词编写的落盘闸。任何一处单独放宽，产出的提示词都会在下游宫格解析（``parse_scene_shots``）
与出图渲染（token 换「图N」）时失效。

格式（单段多镜头文本，``image_prompt`` 或 ``image_prompt.scene``）::

    【镜头1｜中景】平视机位，缓推。沈昭@[沈昭]站在床侧，左手把一张 A4 白纸举到台灯下……
"""

from __future__ import annotations

import itertools
import re
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass
from difflib import SequenceMatcher

from lib.project.asset_types import asset_name_comparison_key
from lib.references.reference_catalog import split_derivative_reference

#: 镜头首行标记里的景别是真实镜头语言，不是「景别 / 机位 / 运镜」这类占位字面量。
SHOT_SIZES = ("大远景", "远景", "全景", "中远景", "中景", "中近景", "近景", "大特写", "特写")
CAMERA_POSITIONS = (
    "平视",
    "俯拍",
    "仰拍",
    "俯视",
    "仰视",
    "过肩",
    "主观",
    "侧拍",
    "正拍",
    "斜侧",
    "顶拍",
)
CAMERA_MOTIONS = (
    "固定",
    "推",
    "拉",
    "摇",
    "移",
    "跟",
    "升",
    "降",
    "环绕",
    "手持",
    "变焦",
    "晃动",
)

#: 具体动作判定词表：覆盖短剧里常见的肢体 / 表情 / 手部 / 移动动作。校验只要求镜头块
#: 里出现其中任意一个，词表过窄会把合法提示词误判成「没有动作」，逼出无谓返修甚至
#: 让整次规划失败——所以宁可宽，不可窄。
_ACTION_WORDS = re.compile(
    "推|拉|拽|拎|抬|举|落|放|递|接|抛|扔|丢|掷|挥|挥动|摆|摇|晃|"
    "抓|握|攥|捏|掐|拧|撕|扯|拔|抽|插|按|压|掰|挑|解|系|扣|摘|戴|"
    "敲|砸|摔|拍|推门|掀|踢|踩|踏|跺|踹|撞|抵|顶|撑|挡|拦|护|"
    "走|跑|奔|冲|退|撤|追|逃|跟|随|跃|跳|爬|翻|滚|潜|游|飞|"
    "站|坐|跪|蹲|躺|卧|靠|倚|俯身|弯腰|起身|起立|站起|坐下|跪下|转身|回头|侧身|"
    "抬头|低头|转头|回首|逼近|靠近|凑近|后退|贴近|顿住|停住|僵住|定住|"
    "看|望|盯|瞥|瞪|瞧|瞄|扫视|打量|凝视|注视|仰望|俯瞰|环顾|"
    "睁|闭|眨|眯|皱眉|挑眉|蹙眉|咧嘴|抿|咬|嚼|吞|咽|喘|吸|呼|屏息|叹|吐|"
    "笑|哭|泣|哽咽|颤抖|发抖|哆嗦|瑟缩|缩|蜷|瘫|软|"
    "点头|摇头|颔首|鞠躬|挥手|招手|握手|拥抱|搂|抱|揽|挽|扶|搀|牵|拉住|拽住|"
    "打开|合上|关上|翻开|合拢|点亮|熄灭|点|燃|吹|"
    "说话|开口|喊|叫|吼|吼道|低声|轻声|念|读|问|答|应|"
    "想|忆|想起|记起|思索|沉思"
)
_EMOTION_WORDS = re.compile(
    "眼|眉|眸|神色|神情|表情|脸|面色|目光|笑|哭|泣|怒|惊|惧|怯|悲|痛|"
    "恨|冷|暖|紧|颤|抖|慌|恐|急|忧|焦|愣|怔|僵|屏息|叹|喘|哽|咬|抿|"
    "坚定|迟疑|犹豫|压声|软|疲|累|困|兴奋|欣喜|放松|羞|傲|愧|疑|不甘|失落"
)
#: 空泛占位描写：出现即判不合格，不靠模型自觉。
_PLACEHOLDER_PHRASES = ("一位人物", "一处场景", "气氛紧张", "人物站着")

#: 镜头标记：``【镜头N｜景别】``，景别必填（校验口径）；解析容忍 ``【镜头N】`` 缺省形。
_MARK = re.compile(r"【\s*镜头\s*(\d+)\s*(?:[｜|]\s*([^】\n]*?)\s*)?】")

#: 台词行：``台词：…`` / ``对白：…`` / ``画外音：…``。image_prompt 与 video_prompt.action
#: 只写画面看得见的内容，台词只由 ``utterances`` 承载。
_DIALOGUE_LINE = re.compile(r"^[ \t]*(?:台词|对白|画外音|旁白)\s*[：:].*$", re.MULTILINE)

#: 引用 token：``@[登记名]``；供资产解析与校验共用同一条正则。
_TOKEN = re.compile(r"@\[([^]\n]+)\]")

#: 泛化前缀（``角色@[...]``）：旧三行模板的遗留写法，出图渲染对不上资产名。
_GENERIC_PREFIX = re.compile(r"(?:角色|场景|道具|商品|风格锚)\s*@\[")

#: token 内部禁止出现的标点：出现即说明堆的是外观描述，不是登记名。
_TOKEN_PUNCTUATION = "，。；、！？："

#: 场景内逐镜承接标记：位置 / 姿态 / 视线 / 手持物 / 光的延续，或明确的位移动作。
#: 每条镜头（首个除外）必须至少命中一个，否则视为「自包含」写法——分镜内部会断开，
#: 视频模型只能自行补运动（ADR 0098 D2 的连续镜头要求）。
_CONTINUITY_MARKERS = re.compile(
    "承接|仍|又|继续|随即|接着|随后|转身|回身|起身|站起|坐下|俯身|直起|"
    "走近|走到|退开|后退|上前|凑近|靠近|绕过|跨过|放下|抬起|收回|松开|"
    "接过|递|抽出|推开|拉上|撑|扶|按住|压住|盯|望|看向|扫向|循声|"
    # 显式断点：闪回 / 时间跳允许不承接，但必须在正文写出断的信号。
    "闪回|硬切|黑场|光转|跳切"
)

#: 断点标记：与承接词配套，幕间判定「显式切点」。
_BREAK_MARKERS = re.compile("硬切|黑场|光转|闪回|跳切|转场")

#: 给人看的承接词清单：提示词里直接照抄，模型才不会瞎猜闸门认什么。
CONTINUITY_MARKER_HINT = (
    "承接、仍、又、继续、随即、接着、随后、转身、回身、起身、站起、坐下、俯身、直起、"
    "走近、走到、退开、后退、上前、凑近、靠近、绕过、跨过、放下、抬起、收回、松开、"
    "接过、递、抽出、推开、拉上、撑、扶、按住、压住、盯、望、看向、扫向、循声"
)

#: 断点标记清单（闪回 / 时间跳允许不承接，但必须在正文写出断的信号）。
BREAK_MARKER_HINT = "闪回、硬切、黑场、光转、跳切"

#: 单个镜头块的丰满度下限（字符数），与动作 / 神态词共同构成质量闸。
_BLOCK_MIN_CHARS = 70

#: 剥掉镜头标记与引用 token 后的「裸文本」：动作 / 神态词只认它，资产外观描述与
#: 镜头标记不能再替正文凑数（2026-10-24 评审拍板）。NFKC 折叠全角标点后再匹配。
_BARE_NOISE = re.compile(r"【[^】\n]*】|@\[[^]\n]*\]")


@dataclass(frozen=True, slots=True)
class ShotBlock:
    """一个镜头块：标记序号、标记里的景别（可空）与标记后的正文。"""

    number: int
    shot_size: str
    body: str


def shot_blocks(scene: str) -> list[ShotBlock]:
    """按镜头标记把提示词切成块：序号重复留首、空正文丢弃，与 ``parse_scene_shots`` 同口径。"""

    blocks: list[ShotBlock] = []
    seen: set[int] = set()
    marks = list(_MARK.finditer(scene))
    for position, mark in enumerate(marks):
        number = int(mark.group(1))
        end = marks[position + 1].start() if position + 1 < len(marks) else len(scene)
        body = scene[mark.end() : end].strip()
        if not body or number in seen:
            continue
        seen.add(number)
        blocks.append(ShotBlock(number=number, shot_size=(mark.group(2) or "").strip(), body=body))
    return blocks


def _bare_text(block_body: str) -> str:
    return unicodedata.normalize("NFKC", _BARE_NOISE.sub("", block_body))


def dialogue_lines(text: str) -> list[str]:
    """返回文本里的台词行（保留原行，供诊断与 legacy 剥离共用）。"""
    return [match.group(0).strip() for match in _DIALOGUE_LINE.finditer(text)]


def dialogue_line_problem(text: str, *, label: str = "image_prompt") -> str | None:
    """画面提示词不得出现台词行：返回可回喂模型的原因，合格返回 ``None``。

    ``image_prompt`` 与 ``video_prompt.action`` 都只写画面看得见的内容；台词只由
    ``utterances`` 承载。允许「启唇说话 / 冷笑 / 画外传来宣读声」这类动作或环境描述，
    但不允许 ``台词：…`` / ``对白：…`` / ``画外音：…`` 原句行。
    """
    matches = list(_DIALOGUE_LINE.finditer(text))
    if not matches:
        return None
    first = matches[0]
    line_number = text.count("\n", 0, first.start()) + 1
    snippet = first.group(0).strip()
    if len(snippet) > 40:
        snippet = f"{snippet[:40]}…"
    return (
        f"{label} 不得包含台词行（第 {line_number} 行「{snippet}」）："
        "台词只由 utterances/source_text 承载，画面只写看得见的内容"
    )


def reference_tokens(text: str) -> list[str]:
    """正文里的 ``@[登记名]`` token（按出现序去重）。"""
    return list(dict.fromkeys(match.group(1).strip() for match in _TOKEN.finditer(text)))


def _asset_owner(inner: str) -> str:
    """token 内名字的本体名：``角色/衍生`` 的衍生不单独登记，登记判定落在本体上。"""
    return split_derivative_reference(asset_name_comparison_key(inner))[0]


def handoff_problem(
    previous_prompt: str,
    current_prompt: str,
    *,
    character_names: Collection[str],
) -> str | None:
    """幕间承接：后一幕首镜要接住前一幕末镜的在场人物，或用断点标记显式切开。

    判据是「人物交集」或「断点标记」二选一。只看角色，不看场景/道具——同一场戏里场景
    token 必然重复，用它判承接等于没判。合格返回 ``None``，否则返回可回喂的原因。
    """
    before = shot_blocks(previous_prompt)
    after = shot_blocks(current_prompt)
    if not before or not after:
        return None
    previous_text = before[-1].body
    current_text = after[0].body
    characters = {name for name in character_names if name}
    previous_cast = {name for name in characters if name in previous_text}
    current_cast = {name for name in characters if name in current_text}
    if previous_cast & current_cast:
        return None
    if _BREAK_MARKERS.search(current_text):
        return None
    # 上一镜没有人物在场时，允许用陈设/光源/声源承接：正文显式写「承接」即可。
    if "承接" in current_text:
        return None
    return (
        "首镜没有接住上一分镜末镜（上一镜在场人物："
        f"{'、'.join(sorted(previous_cast)) or '未登记角色'}，本镜一个都没出现）；"
        "要么让首镜出现上一镜的人物 / 视线 / 声源，用陈设或光源承接时写明「承接上一镜的…」，"
        "要么在首镜写断点标记（硬切/黑场/光转/闪回）"
    )


def lint_shot_prompt(
    scene: str,
    *,
    registered_names: Collection[str],
    allowed_names: Collection[str] | None = None,
    character_names: Collection[str] = (),
) -> str | None:
    """标准分镜提示词的格式闸：合格返回 ``None``，否则返回可回喂模型的不合格原因。

    ``registered_names`` 是项目资产表已登记的名字；``allowed_names`` 是该分镜的出场资产清单
    （缺省时与登记名同集）。两者都按比对坐标系判等。token 硬闸要求「token ⊆ 出场清单 ⊆ 已登记」，
    这是下游出图把 token 指认到「图N」的充分条件（参考图由条目结构化字段决定，mention 只指认）。
    ``character_names`` 用来识别空镜：画面里没有人物时不要求神态。
    """
    registered = {asset_name_comparison_key(name) for name in registered_names}
    allowed = registered if allowed_names is None else {asset_name_comparison_key(name) for name in allowed_names}
    text = scene.strip()
    if not text:
        return "分镜提示词为空"
    if problem := dialogue_line_problem(text):
        return problem
    blocks = shot_blocks(text)
    if not blocks:
        return "分镜提示词里没有【镜头N】分镜块"
    if [block.number for block in blocks] != list(range(1, len(blocks) + 1)):
        return "镜头编号必须从 1 开始连续递增，不重不漏"
    for block in blocks:
        if reason := _lint_block(block, character_names):
            return reason
    previous: ShotBlock | None = None
    for block in blocks[1:]:
        bare = _bare_text(block.body)
        if not _CONTINUITY_MARKERS.search(bare):
            # 同一人物在前后两镜都在场，本身就是承接（镜头切的是机位，不是时空）。
            if previous is not None and _shared_characters(previous, block, character_names):
                previous = block
                continue
            # 空镜（没人）没有姿态/视线可接：只要和上一镜共享同一场景标识物，就算同一场延续。
            if not (
                _is_environment_shot(bare, character_names) and previous is not None and _shared_tokens(previous, block)
            ):
                return (
                    f"镜头{block.number} 未写承接：请用上一镜的末状态（位置/姿态/视线/手持物/光）开头，"
                    "或写出明确位移（转身/走过/俯身/退开…），不要写成互不相干的自包含画面"
                )
        previous = block
    if reason := _lint_tokens(text, registered, allowed):
        return reason
    bodies = [_bare_text(block.body).strip() for block in blocks]
    for left, right in itertools.combinations(bodies, 2):
        if SequenceMatcher(None, left, right, autojunk=False).ratio() >= 0.92:
            return "有语义高度重复的凑数镜头"
    return None


def _lint_block(block: ShotBlock, character_names: Collection[str] = ()) -> str | None:
    label = f"镜头{block.number}"
    if block.shot_size not in SHOT_SIZES:
        return (
            f"{label} 缺景别或景别不在词表内（须写成「【镜头{block.number}｜中景】…」，景别取 {'/'.join(SHOT_SIZES)}）"
        )
    first_sentence = _bare_text(block.body.split("。", 1)[0])
    if not any(word in first_sentence for word in CAMERA_POSITIONS):
        return f"{label} 首句缺机位（如「平视机位，缓推。」，机位取 {'/'.join(CAMERA_POSITIONS)}）"
    if not any(word in first_sentence for word in CAMERA_MOTIONS):
        return f"{label} 首句缺运镜（如「平视机位，缓推。」，运镜取 {'/'.join(CAMERA_MOTIONS)}）"
    bare = _bare_text(block.body)
    if len(block.body.strip()) < _BLOCK_MIN_CHARS:
        return f"{label} 块过短，内容不够丰满"
    if not _ACTION_WORDS.search(bare):
        return f"{label} 没有具体动作"
    # 空镜（画面里没有人物）没有神态可写：只要求光影/环境在动，不能拿「缺神态」卡死转场镜。
    if not _EMOTION_WORDS.search(bare) and not _is_environment_shot(bare, character_names):
        return f"{label} 缺具体神态或情绪"
    if any(phrase in bare for phrase in _PLACEHOLDER_PHRASES):
        return f"{label} 包含空泛占位描写"
    return None


def _is_environment_shot(bare: str, character_names: Collection[str]) -> bool:
    """画面里没有人物即空镜：没有登记角色名、也没有人称代词。"""
    if any(name and name in bare for name in character_names):
        return False
    return not any(pronoun in bare for pronoun in ("他", "她", "众人", "有人", "人影"))


def _shared_tokens(left: ShotBlock, right: ShotBlock) -> bool:
    """两个镜头块是否共享同一个 ``@[登记名]``（通常是同一场景）。"""
    left_tokens = {match.group(1).strip() for match in _TOKEN.finditer(left.body)}
    right_tokens = {match.group(1).strip() for match in _TOKEN.finditer(right.body)}
    return bool(left_tokens & right_tokens)


def _shared_characters(left: ShotBlock, right: ShotBlock, character_names: Collection[str]) -> bool:
    """两个镜头块是否都有同一个角色在场：同人跨镜即承接。"""
    names = {name for name in character_names if name}
    if not names:
        return False
    left_names = {name for name in names if name in left.body}
    right_names = {name for name in names if name in right.body}
    return bool(left_names & right_names)


def _lint_tokens(text: str, registered: set[str], allowed: set[str]) -> str | None:
    if _GENERIC_PREFIX.search(text):
        return "禁止「角色@/场景@」这类泛化前缀，引用须写成「名字@[登记名]」"
    for match in _TOKEN.finditer(text):
        inner = match.group(1).strip()
        if any(char in inner for char in _TOKEN_PUNCTUATION):
            return "引用 token 里堆了外观描述：@[名字] 内只能是登记名（或 角色/衍生），外观由参考图承载"
        owner = _asset_owner(inner)
        if owner not in registered:
            return f"引用了未登记资产: {inner}"
        if owner not in allowed:
            return f"引用了出场资产清单之外的资产: {inner}（清单外的已登记资产写裸名，或先回分镜/资产步骤修正清单）"
    return None


__all__ = [
    "BREAK_MARKER_HINT",
    "CAMERA_MOTIONS",
    "CAMERA_POSITIONS",
    "CONTINUITY_MARKER_HINT",
    "SHOT_SIZES",
    "ShotBlock",
    "dialogue_line_problem",
    "dialogue_lines",
    "handoff_problem",
    "lint_shot_prompt",
    "reference_tokens",
    "shot_blocks",
]
