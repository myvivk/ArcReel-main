#!/usr/bin/env python3
"""short-drama-factory v3.2 单集剧本机检（分档）

用法:
    python3 validate_episode.py <单集剧本.md> [--format auto|standard|long|manju] [--prev 前集.md ...]
    python3 validate_episode.py --self-test

档位 profile (--format; 默认 auto = 按正文自检: 含"长档"→long, 含"漫剧"→manju, 否则 standard):
    standard  标准档 90~120 秒   正文体量 实拍 350~500 / 漫剧 260~400   场景 <=2   对白行 >=4
    long      长档   165~195 秒  正文体量 实拍 650~850 / 漫剧 520~680   场景 <=3   对白行 >=8   必设【本集中段小钩】
    manju     漫剧档 60~90 秒    正文体量 260~400                       场景 <=2   对白行 >=4

检查项 (FAIL=拦截必须修复, WARN=告警复核):
    E1  剧本文本体量(对白+动作行+断章画面, 不含【】字段行/[场景][人物])  FAIL(超区间)
          标准档 实拍 350~500 / 漫剧 260~400 ；长档 实拍 650~850 / 漫剧 520~680
    E2  单句台词(常规 12~18 字 / 爆发金句 8~12) 硬上限 25 字   WARN(逐句 >25)
    E3  场景数 <=2 ([场景] 标记计数)                FAIL(>2)
    E4  黄金前3秒: 首个动作/台词行含冲突信号词       FAIL(无)
    E5  开篇禁词(起床/拉窗帘/太阳升起/走在路上等)     WARN
    E6  断章卡点: 存在【本集断章卡点】且含黑屏/悬念   FAIL(缺)
    E7  情绪流变词: 存在【情绪流变】链条(-> 或 ➔)    WARN(缺)
    E8  复读检测: 主角台词与前集传入(--prev)重复     WARN(重复句)
    E9  对白行数: >= 本档位下限(标准/漫剧 4 行, 长档 8 行)  WARN(不足)
    E10 合规红线词(断肢/开膛/下蛊等一票否决直观描写)  FAIL
    E11 长档中段小钩: 存在【本集中段小钩】(long 档)      FAIL(长档缺)
    E12 密度拍数: ceil(本集秒数 / 2) 拍（△ 动作或对白）      FAIL(不足)
    E13 道具行: 每行一个「名称｜外观」，同集不重复，未入戏 WARN   FAIL/WARN
    E14 角色外观: [人物] 每个角色须带 ≥8 字外观，必要时与人物圣经一致 FAIL
退出码: 0=通过(可含WARN), 1=FAIL, 2=用法错误
"""

import math
import re
import sys

# 剧本文本体量阈值(仅计正文: 对白行 + △动作行 + 断章卡点画面描述)
NET_MIN, NET_MAX = 350, 500  # 实拍短剧
NET_MIN_ANIME, NET_MAX_ANIME = 260, 400  # 漫剧/竖屏动画
NET_LONG_MIN, NET_LONG_MAX = 650, 850  # 长档(约3分钟) 实拍
NET_LONG_ANIME_MIN, NET_LONG_ANIME_MAX = 520, 680  # 长档 漫剧/竖屏动画
SENT_MAX = 25  # 单句台词硬上限(常规 12~18)
BEAT_SECONDS = 2.0  # E12：每拍按 2 秒预算
CHARACTER_APPEARANCE_MIN = 8  # E14：挡住只有状态标签的 [人物] 行

# 档位 profile（体量区间另按 profile + 是否漫剧解析，见 net_band()）
PROFILES = {
    "auto": {"scenes": 2, "lines": 4, "midhook": False, "label": "自动分档"},
    "standard": {"scenes": 2, "lines": 4, "midhook": False, "label": "标准档 90~120 秒"},
    "long": {"scenes": 3, "lines": 8, "midhook": True, "label": "长档 约3分钟"},
    "manju": {"scenes": 2, "lines": 4, "midhook": False, "label": "漫剧档 60~90 秒"},
}

OPEN_BAN_WORDS = [
    "早晨起床",
    "起床",
    "拉窗帘",
    "太阳升起",
    "醒过来",
    "走在路上",
    "整理衣服",
    "开车上班",
    "喝咖啡闲聊",
    "悠闲的清晨",
    "阳光明媚的早晨",
]

# 注: 不含 "△" —— 动作行本身不等于冲突, 否则任何以动作开场的剧本恒过 E4
CONFLICT_SIGNALS = [
    "！?",
    "！",
    "枪",
    "刀",
    "巴掌",
    "扔",
    "砸",
    "吼",
    "冷笑",
    "逼",
    "跪",
    "撕",
    "拍在",
    "顶在",
    "推",
    "踹",
    "骂",
    "羞辱",
    "退婚",
    "病危",
    "签字",
    "死",
    "滚",
    "住手",
    "救",
    "断裂",
    "炸",
    "烧",
    "血",
]

BREAK_WORDS = ["断章", "卡点", "黑屏", "悬念"]

REDLINE_WORDS = [
    "断肢",
    "开膛",
    "破肚",
    "身首异处",
    "凌迟",
    "活埋",
    "注射死刑",
    "吸毒",
    "制毒",
    "自杀方法",
    "下蛊害人",
    "蛊毒害人",
    "真实的国家机关",
]


def _is_dialogue(line):
    """对白行: 角色名(可选括号提示): 台词  —— 不以[ △ 【开头"""
    s = line.strip()
    if not s or s.startswith(("[", "△", "【", "#", ">")):
        return None
    m = re.match(r"^([\u4e00-\u9fa5A-Za-z0-9]{1,8})(（[^）]*）|\([^)]*\))?\s*[:：]\s*(.+)$", s)
    return m.group(3) if m else None


def _issue(code, message, line=None):
    """与 ``lib.short_drama_quality.QualityIssue.render()`` 同格式。"""
    return f"{code} 第{line}行 {message}" if line else f"{code} {message}"


def net_chars(text):
    """剧本文本体量: 仅计
        - 对白行台词
        - △ 动作指示行内容
        - 【本集断章卡点】画面描述
    剔除: 【所属阶段】【黄金前3秒钩子】【情绪流变】【本集时长】等字段行,
          以及 [场景]/[人物] 标记行(结构性元数据, 非剧本正文)
    """
    total = 0
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        d = _is_dialogue(s)
        if d is not None:
            body = d
        elif s.startswith("△"):
            body = re.sub(r"^△\s*", "", s)
        elif s.startswith("【本集断章卡点】"):
            body = re.sub(r"^【本集断章卡点】\s*[:：]?\s*", "", s)
        else:
            continue  # 字段行【】/标记行[]/普通行 一律不计
        body = re.sub(r"[\s，。！？；：、…—\-·“”\"'‘’（）()《》!?.]", "", body)
        total += len(body)
    return total


def is_anime(text):
    """漫剧/竖屏动画项目: 文本含 '漫剧' 即按漫剧体量档判 E1"""
    return "漫剧" in text


def is_long(text):
    """长档项目: 文本含 '长档' 即按长档体量判 E1/E3/E9/E11"""
    return "长档" in text


def resolve_profile(text, profile="auto"):
    """auto 档按正文自检: 含"长档"→long, 含"漫剧"→manju, 否则 standard"""
    if profile != "auto":
        return profile if profile in PROFILES else "standard"
    if is_long(text):
        return "long"
    if is_anime(text):
        return "manju"
    return "standard"


def net_band(text, profile):
    """按档位 + 是否漫剧返回正文体量区间"""
    if profile == "long":
        return (NET_LONG_ANIME_MIN, NET_LONG_ANIME_MAX) if is_anime(text) else (NET_LONG_MIN, NET_LONG_MAX)
    if profile == "manju":
        return NET_MIN_ANIME, NET_MAX_ANIME
    return (NET_MIN_ANIME, NET_MAX_ANIME) if is_anime(text) else (NET_MIN, NET_MAX)


def scene_count(text):
    return len(re.findall(r"^\[场景\]|^【场景】|^##?\s*场景", text, re.M))


def resolve_declared_seconds(text, profile):
    match = re.search(r"^【本集时长】[：:]\s*(\d+)\s*秒", text, re.M)
    if match:
        return int(match.group(1))
    return {"standard": 105, "long": 180, "manju": 75}.get(profile, 105)


def _read_text(path):
    """按 UTF-8 读文本；缺文件或编码不对返回 None，由调用方决定怎么报错。"""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (FileNotFoundError, UnicodeDecodeError):
        return None


def _read_bible(path):
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (FileNotFoundError, UnicodeDecodeError):
        print(f"[-] 找不到或无法读取人物圣经: {path}")
        raise SystemExit(2) from None


def _bible_section(bible, name):
    """人物圣经里 ``【角色】：名字`` 起、到下一个角色段前的整段；没有该角色返回 None。"""
    lines = bible.splitlines()
    start = None
    sections = []
    for index, line in enumerate(lines):
        match = re.match(r"^\s*【角色】[：:]\s*(.+)$", line)
        if match:
            raw_name = re.split(r"[（(]", match.group(1), maxsplit=1)[0].strip()
            if start is not None:
                sections.append("\n".join(lines[start:index]))
            start = index if raw_name == name else None
    if start is not None:
        sections.append("\n".join(lines[start:]))
    return sections[0] if sections else None


def beat_count(text):
    return sum(bool(line.strip().startswith("△") or _is_dialogue(line)) for line in text.splitlines())


def _split_character_entries(value):
    entries, current, depth = [], [], 0
    for char in value:
        if char in "（(":
            depth += 1
        elif char in "）)":
            depth = max(0, depth - 1)
        if char in "、,，" and depth == 0:
            token = "".join(current).strip()
            if token:
                entries.append(token)
            current = []
        else:
            current.append(char)
    token = "".join(current).strip()
    if token:
        entries.append(token)
    return entries


def _character_entry(token):
    name = re.split(r"[（(]", token, maxsplit=1)[0].strip()
    match = re.search(r"[（(]([^）)]*)[）)]", token)
    appearance = ""
    if match:
        inner = match.group(1)
        for separator in ("｜", "|"):
            if separator in inner:
                appearance = inner.split(separator, 1)[1].strip()
                break
    return name, appearance


def _split_prop_line(value):
    """``铁门｜锈迹、半掩`` → ``("铁门", "｜", "锈迹、半掩")``；缺分隔符时第三项为空。"""
    name, separator, appearance = value.partition("｜")
    if not separator:
        name, separator, appearance = value.partition("|")
    return name.strip(), separator, appearance.strip()


def _declared_asset_names(text):
    """``[人物]`` / ``[场景]`` 声明的名字，用于 E13 重名告警。"""
    characters, scenes = set(), set()
    for line in text.splitlines():
        stripped = line.strip()
        person_match = re.match(r"^\[人物\][：:]\s*(.+)$", stripped)
        if person_match:
            characters.update(
                name
                for name, _ in (_character_entry(token) for token in _split_character_entries(person_match.group(1)))
                if name
            )
        scene_match = re.match(r"^\[场景\][：:]\s*(.+)$", stripped)
        if scene_match:
            scenes.add(_split_prop_line(scene_match.group(1))[0])
    return characters, scenes


def first_content_lines(text, n=3):
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(
            ("#", ">", "第", "【所属", "【黄金", "【情绪", "【本集时长", "[场景", "[人物", "【场景", "【人物")
        ):
            continue
        out.append(s)
        if len(out) >= n:
            break
    return out


def validate(text, prev_texts=(), profile="auto", bible=None):
    profile = resolve_profile(text, profile)
    prof = PROFILES[profile]
    fails, warns = [], []
    nc = net_chars(text)
    lo, hi = net_band(text, profile)
    if not (lo <= nc <= hi):
        kind = "漫剧" if is_anime(text) else "实拍"
        fails.append(f"E1 [{profile}] 剧本文本体量 {nc} 不在 {lo}~{hi} 区间（{kind}档）")

    for line in text.splitlines():
        d = _is_dialogue(line)
        if d:
            clean = re.sub(r"[\s，。！？；：、…—]", "", d)
            if len(clean) > SENT_MAX:
                warns.append(f"E2 单句台词 {len(clean)} 字(>{SENT_MAX}): {clean[:18]}…")

    sc = scene_count(text)
    if sc > prof["scenes"]:
        fails.append(f"E3 [{profile}] 场景数 {sc} >{prof['scenes']}")
    if sc == 0:
        warns.append("E3 未检出 [场景] 标记，无法核对场景数")

    head = "".join(first_content_lines(text))
    banned_hit = any(w in head for w in OPEN_BAN_WORDS)
    if banned_hit or not any(w in head for w in CONFLICT_SIGNALS):
        fails.append("E4 前3秒无冲突/命中开篇废镜头词(须直接切入冲突顶点，禁铺垫)")
    warns.extend(f"E5 开篇疑似废镜头禁词「{w}」" for w in OPEN_BAN_WORDS if w in head)

    if not re.search(r"【本集断章卡点】|\[本集断章", text):
        fails.append("E6 缺【本集断章卡点】段")
    else:
        seg = re.search(r"【本集断章卡点】(.{0,200})", text, re.S)
        if seg and not any(w in seg.group(1) for w in ["黑屏", "悬念", "下滑", "解锁"]):
            warns.append("E6 断章卡点缺黑屏/下滑解锁落点词")

    if not re.search(r"【情绪流变】.*(->|➔|➔|→)", text):
        warns.append("E7 缺【情绪流变】链条(应标: 遭受刁难 ➔ … ➔ 绝命断章)")

    rounds = 0
    my_lines = [d for d in (_is_dialogue(line) for line in text.splitlines()) if d]
    rounds = len(my_lines)
    if prof["midhook"] and not re.search(r"【本集中段小钩】|\[本集中段小钩", text):
        fails.append("E11 长档缺【本集中段小钩】(≈90 秒处必设新信息/新威胁，防滑走)")
    if rounds < prof["lines"]:
        warns.append(f"E9 [{profile}] 对白行仅 {rounds} 行(<{prof['lines']})，攻防结构不足")

    seconds = resolve_declared_seconds(text, profile)
    required_beats = math.ceil(seconds / BEAT_SECONDS)
    actual_beats = beat_count(text)
    if actual_beats < required_beats:
        fails.append(
            _issue(
                "E12",
                f"拍数 {actual_beats} < {required_beats}（{seconds} 秒 ÷ {BEAT_SECONDS:g} 秒/拍），"
                f"缺 {required_beats - actual_beats} 拍；一句概括不算拍，拆成连续动作",
            )
        )

    prop_entries = {}
    body_lines = [line.strip() for line in text.splitlines() if line.strip()]
    character_names, scene_names = _declared_asset_names(text)
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        prop_match = re.match(r"^\[道具\][：:]\s*(.+)$", stripped)
        if prop_match:
            name, separator, appearance = _split_prop_line(prop_match.group(1))
            if not separator or not name or not appearance:
                fails.append(
                    _issue(
                        "E13",
                        f"道具行缺少有效名称或「｜」后的外观描述：{name or prop_match.group(1).strip()}",
                        line_number,
                    )
                )
                continue
            if name in prop_entries:
                fails.append(_issue("E13", f"道具「{name}」在本集中重复登记", line_number))
            prop_entries[name] = line_number
            if name in character_names or name in scene_names:
                warns.append(_issue("E13", f"道具「{name}」与角色或场景重名，导入时会记录资产冲突", line_number))
            if not any(name in body for body in body_lines if not body.startswith(("[道具]", "[人物]", "[场景]"))):
                warns.append(_issue("E13", f"道具「{name}」已登记但未在动作或台词中出现", line_number))

        person_match = re.match(r"^\[人物\][：:]\s*(.+)$", stripped)
        if person_match:
            for token in _split_character_entries(person_match.group(1)):
                name, appearance = _character_entry(token)
                if not name:
                    continue
                if len(appearance) < CHARACTER_APPEARANCE_MIN:
                    fails.append(
                        _issue(
                            "E14",
                            f"[人物] 行外观缺失或过短：{name}；"
                            "每位出场角色须带「｜」后的完整外形，并从人物圣经原样抄入（30~60 字）",
                            line_number,
                        )
                    )
                elif bible is not None:
                    section = _bible_section(bible, name)
                    if section is None:
                        fails.append(_issue("E14", f"人物圣经里没有 {name}，外观抄无可抄", line_number))
                    elif re.sub(r"\s", "", appearance) not in re.sub(r"\s", "", section):
                        fails.append(_issue("E14", f"{name} 的外观不是从人物圣经原样抄入（被改写或漏抄）", line_number))

    for prev in prev_texts:
        prev_set = {re.sub(r"\s", "", p) for p in (_is_dialogue(line) for line in prev.splitlines()) if p}
        for d in my_lines:
            dd = re.sub(r"\s", "", d)
            if dd in prev_set and len(dd) >= 8:
                warns.append(f"E8 台词与前集复读: {dd[:16]}…")

    fails.extend(f"E10 合规一票否决类直观描写「{w}」" for w in REDLINE_WORDS if w in text)

    return fails, warns, nc


GOOD = """第【1】集：【龙王令·开局羞辱】
【黄金前3秒钩子】：极端羞辱
【情绪流变】：当众受辱 ➔ 隐忍握拳 ➔ 亮令反杀 ➔ 绝命断章
【本集时长】：105 秒
[场景]：内景·顶级私人会所·夜
[人物]：林辰（隐忍龙王｜三十岁上下，寸头，旧夹克磨白）、陈峰（嚣张富二代｜宽脸浓眉，金表晃眼，定制西装笔挺）、苏清雪（未婚妻｜长发披肩，杏眼含霜，浅色礼裙贴身）
[道具]：九龙玄铁令｜巴掌大，玄铁冷光，正面铸九条盘龙
[道具]：酒柜｜整面到顶，玻璃门，摆满洋酒

△ 巴掌扇来，被反手攥住。
△ 骨节咯吱作响。
△ 陈峰搂着苏清雪吐烟。
△ 保镖环立，铁棍点地。
陈峰（吐烟）：跪下，喝了这杯脏水。
△ 玻璃杯砸在脚边。
△ 水花溅上裙摆。
苏清雪（冷漠）：别不知好歹。
△ 林辰垂眸不语。
△ 右拳在身侧收紧。
△ 指节泛白。
△ 衣袖绷出褶皱。
林辰（极低）：三年前你陈家跪着求我。
△ 陈峰脸色骤变。
△ 烟灰抖落手背。
陈峰（恼羞）：你找死！废了他双手！
△ 四名保镖同时暴起。
△ 铁棍扫向后颈。
△ 林辰侧身。
△ 棍风擦过耳廓。
△ 反手扣住手腕。
△ 顺势一拧。
△ 骨裂声脆响。
△ 铁棍脱手落地。
△ 保镖倒飞。
△ 砸碎整面酒柜。
△ 玻璃碎片迸溅。
△ 酒水漫过地砖。
△ 全场死寂。
△ 陈峰踉跄后退。
△ 撞翻茶几。
△ 他抬手指着林辰。
△ 指尖发抖。
陈峰（色厉）：你敢动手？知道我爸是谁？
△ 林辰抬眼。
△ 目光钉在他脸上。
△ 他探手入怀。
△ 摸出九龙玄铁令。
△ 令牌拍在桌上。
△ 酒杯齐齐炸裂。
△ 九龙图腾泛出冷光。
△ 陈峰死盯令牌。
△ 双腿剧烈发抖。
△ 手机疯狂震动。
△ 屏幕亮起来电。
△ 显示父亲二字。
△ 他颤抖着接通。
△ 听筒传出哭嚎。
△ 手机滑落。
△ 摔在地砖上。
林辰（如刀）：传我龙王令，陈氏破产。
△ 林辰一步踏出。
△ 皮鞋碾过玻璃。
△ 他居高临下逼近。
△ 阴影罩住陈峰。
△ 画面骤黑。
△ 只余一声闷响。

【本集断章卡点】：陈峰手机落地，屏幕碎裂，来电仍在响。（黑屏：下滑解锁第2集）
"""

BAD = """第【1】集
清晨，林辰起床拉开窗帘，太阳升起。他在路上慢慢走。
陈峰：林辰，你要知道，三年前你害死了我父亲，侵占了我们家三千万的财产，今天我一定要让你血债血偿，把我的东西全部还回来！
林辰：好的。
△ 他把断肢捡起来。
"""


def self_test():
    ok = 1
    f, w, nc = validate(GOOD)
    if f:
        print("FAIL self-test: GOOD 样例不应 FAIL:", f)
        ok = 0
    if nc < 100:
        print("FAIL self-test: GOOD 净字数异常", nc)
        ok = 0
    f, w, _ = validate(BAD)
    if not any("E1" in x for x in f):
        print("FAIL self-test: BAD 应报 E1 或字数异常")
        ok = 0
    if not any("E4" in x for x in f):
        print("FAIL self-test: BAD 应报 E4 前3秒无冲突")
        ok = 0
    if not any("E6" in x for x in f):
        print("FAIL self-test: BAD 应报 E6 缺断章")
        ok = 0
    if not any("E10" in x for x in f):
        print("FAIL self-test: BAD 应报 E10 红线词")
        ok = 0
    if not any("E5" in x for x in w):
        print("FAIL self-test: BAD 应报 E5 开篇禁词")
        ok = 0
    # 回归：新增三项硬闸的最小样本（体量/场景不在本组断言范围）
    compact = (
        "第【1】集\n【本集时长】：4 秒\n[场景]：内景·仓库·夜\n"
        "[人物]：甲（隐退杀手｜短发、旧夹克，右眉有疤）\n[道具]：铁门｜锈迹斑斑、门轴下垂\n"
        "△ 甲推开铁门，冷笑。\n甲：滚开。\n【本集断章卡点】：门外传来巨响。（黑屏）\n"
    )

    def new_gate_fails(sample):
        fails, _, _ = validate(sample)
        return [x for x in fails if x.startswith(("E12", "E13", "E14"))]

    if new_gate_fails(compact):
        print("FAIL self-test: E12-E14 合法样本不应 FAIL:", new_gate_fails(compact))
        ok = 0
    if not any("E12" in x for x in new_gate_fails(compact.replace("△ 甲推开铁门，冷笑。\n甲：滚开。", "甲：滚开。"))):
        print("FAIL self-test: 拍数不足应报 E12")
        ok = 0
    if not any("E13" in x for x in new_gate_fails(compact.replace("｜锈迹斑斑、门轴下垂", ""))):
        print("FAIL self-test: 缺道具外观应报 E13")
        ok = 0
    if not any(
        "E14" in x for x in new_gate_fails(compact.replace("（隐退杀手｜短发、旧夹克，右眉有疤）", "（隐退杀手）"))
    ):
        print("FAIL self-test: 缺人物外观应报 E14")
        ok = 0
    bible = "【角色】：甲\n外形：短发、旧夹克，右眉有疤\n"
    if new_gate_fails(compact) or [x for x in validate(compact, bible=bible)[0] if x.startswith(("E12", "E13", "E14"))]:
        print("FAIL self-test: 外观与圣经一致时不应报 E14")
        ok = 0
    if not any("被改写" in x for x in validate(compact.replace("右眉有疤", "左眉有疤"), bible=bible)[0]):
        print("FAIL self-test: 外观被改写应报 E14")
        ok = 0

    # 回归1: 字段行不应灌入 E1 体量
    hdr_only = "第【1】集\n【所属阶段】：第X幕・（第 1/80 集 ｜ 往返第 N 回合）\n【黄金前3秒钩子】：极端羞辱型（婚礼当众）\n【情绪流变】：受辱 ➔ 隐忍 ➔ 反杀 ➔ 绝命断章\n【本集时长】：108 秒\n[场景]：内景·会所·夜\n[人物]：林辰（隐忍龙王｜三十岁上下，寸头）、陈峰（嚣张富二代｜宽脸浓眉）\n"
    if net_chars(hdr_only) != 0:
        print("FAIL self-test: 纯字段/标记行应计 0 字，实得", net_chars(hdr_only))
        ok = 0

    # 回归2: 以 △ 开场的废镜头剧本，E4 必须拦截
    boring = "第【1】集\n【本集时长】：4 秒\n[场景]：内景·客厅·日\n[人物]：甲（长子｜圆脸、短发，穿深色短袍）、乙（管家｜窄脸、长发束起，灰色制服）\n△ 他慢慢走进客厅，端起茶杯喝了一口。\n△ 他看了看窗外。\n甲：今天天气不错。\n乙：是啊。\n甲：那先这样。\n乙：好。\n【本集断章卡点】：两人告别。（黑屏）\n"
    f, w, _ = validate(boring)
    if not any("E4" in x for x in f):
        print("FAIL self-test: △ 开场的废镜头剧本应报 E4")
        ok = 0

    # 回归3: 漫剧档体量(260~400)与 75 秒拍数应可用
    anime = (
        "第【1】集：漫剧\n【情绪流变】：受辱 ➔ 隐忍 ➔ 反杀 ➔ 绝命断章\n[场景]：内景·堂·日\n"
        "[人物]：甲（长子｜圆脸、短发，眉尾有疤）、乙（管家｜窄脸、长发束起，灰色制服）\n"
        + "".join(f"△ 第{i}掌落下，发麻。\n" for i in range(34))
        + "甲：你确定要这么做？\n乙：我确定。\n甲：那就别怪我。\n乙：来吧。\n"
        "【本集断章卡点】：灯灭人散，只余一击。（黑屏：下滑解锁）\n"
    )
    f, w, nc = validate(anime)
    if any("E1" in x for x in f):
        print("FAIL self-test: 漫剧档不应报 E1 区冲突:", [x for x in f if "E1" in x], "体量", nc)
        ok = 0
    if any("E12" in x for x in f):
        print("FAIL self-test: 漫剧档 75 秒 38 拍不应报 E12:", [x for x in f if "E12" in x])
        ok = 0

    # 回归4: 台词硬上限 25
    longline = "第【1】集\n【本集时长】：4 秒\n[场景]：内景\n[人物]：甲（长子｜圆脸、短发，眉尾有疤）\n△ 拍桌。\n甲：这是超过二十五个字的长复合句用来触发告警检查是否生效一二三四五六七八。\n【本集断章卡点】：黑屏（黑屏）\n"
    f, w, _ = validate(longline)
    if not any("E2" in x for x in w):
        print("FAIL self-test: >25 字台词应报 E2")
        ok = 0

    # 回归5: 长档（约3分钟）—— 双回合 + 中段小钩 + 体量 650~850，auto 档自检
    long_body = "".join(f"△ 巴掌第{i}次扇落。\n" for i in range(82)) + "".join(
        f"林辰：第{i}句话说完就走。\n" for i in range(8)
    )
    long_good = (
        "第【1】集：长档样例（长档·约3分钟）\n【本集时长】：180 秒\n"
        "[场景]：内景·会所·夜\n[场景]：内景·走廊·夜\n[人物]：林辰（隐忍龙王｜三十岁上下，寸头）、陈峰（嚣张富二代｜宽脸浓眉，金表晃眼）\n"
        + long_body
        + "【本集中段小钩】：手机亮起病危通知单，缴费截止今晚，对面按下挂断键。\n"
        + "△ 林辰反手拧腕，骨裂声起，铁棍滚落。\n"
        + "【本集断章卡点】：电话那头传来绝望哭嚎，陈峰双腿发抖。（黑屏：下滑解锁第2集）\n"
    )
    f, w, nc = validate(long_good)
    if f:
        print("FAIL self-test: 长档样例不应 FAIL:", f, "体量", nc)
        ok = 0
    if not (NET_LONG_MIN <= nc <= NET_LONG_MAX):
        print(f"FAIL self-test: 长档样例体量 {nc} 应在 {NET_LONG_MIN}~{NET_LONG_MAX}")
        ok = 0
    # 长档缺中段小钩 → E11
    f, _, _ = validate(long_good.replace("【本集中段小钩】", "【中段】"))
    if not any("E11" in x for x in f):
        print("FAIL self-test: 长档缺中段小钩应报 E11")
        ok = 0
    # 显式 --format long 检标准档样本 → E1 + E11
    f, w, _ = validate(GOOD, profile="long")
    if not any("E1" in x for x in f):
        print("FAIL self-test: 标准档样本按 long 显式机检应报 E1")
        ok = 0
    if not any("E11" in x for x in f):
        print("FAIL self-test: 标准档样本按 long 显式机检应报 E11")
        ok = 0
    # 长档场景上限 3：4 场应报 E3
    f, _, _ = validate(long_good + "[场景]：内景·车库·夜\n[场景]：内景·天台·夜\n")
    if not any("E3" in x for x in f):
        print("FAIL self-test: 长档 4 场景应报 E3")
        ok = 0

    print("[+] self-test PASSED" if ok else "[-] self-test FAILED")
    return 0 if ok else 1


def parse_args(args):
    """返回 (path, profile, prev_paths, bible_path)；用法错误抛 SystemExit(2)"""
    path, profile, prevs, bible = None, "auto", [], None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--prev":
            i += 1
            while i < len(args) and not args[i].startswith("--"):
                prevs.append(args[i])
                i += 1
            continue
        if a == "--bible":
            if i + 1 >= len(args):
                print("[-] --bible 需要人物圣经路径")
                raise SystemExit(2)
            bible = args[i + 1]
            i += 1
        elif a == "--long":
            profile = "long"
        elif a == "--manju":
            profile = "manju"
        elif a == "--format" or a.startswith("--format="):
            val = a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < len(args) else "")
            if "=" not in a:
                i += 1
            if val not in PROFILES:
                print(f"[-] --format 只接受 {'/'.join(PROFILES)}，收到「{val}」")
                raise SystemExit(2)
            profile = val
        elif a.startswith("--"):
            print(f"[-] 未知参数「{a}」")
            raise SystemExit(2)
        elif path is None:
            path = a
        i += 1
    return path, profile, prevs, bible


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args[0] == "--self-test":
        return self_test()
    try:
        path, profile, prevs, bible_path = parse_args(args)
    except SystemExit as e:
        return e.code or 2
    if not path:
        print(__doc__)
        return 2
    # 友好处理缺文件/编码错误（避免裸 traceback）
    text = _read_text(path)
    if text is None:
        print(f"[-] 找不到或无法读取剧本文件: {path}")
        return 2
    prev_texts = []
    for p in prevs:
        prev_text = _read_text(p)
        if prev_text is None:
            print(f"[-] 找不到或无法读取前集文件: {p}")
            return 2
        prev_texts.append(prev_text)
    profile = resolve_profile(text, profile)
    bible = _read_bible(bible_path)
    fails, warns, nc = validate(text, prev_texts, profile, bible)
    print(f"== {path}  [{profile} {PROFILES[profile]['label']}]  正文体量≈{nc} ==")
    for x in fails:
        print("[-] FAIL:", x)
    for x in warns:
        print("[!] WARN:", x)
    if fails:
        print("结果: FAILED（修复后重跑）")
        return 1
    print("结果: PASSED" + ("（含 WARN 请复核）" if warns else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
