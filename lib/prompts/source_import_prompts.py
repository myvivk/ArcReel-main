"""原文导入与可改编性评估的提示词。"""

from __future__ import annotations

from dataclasses import dataclass

from lib.script.source_import import SourceImportTier


@dataclass(frozen=True, slots=True)
class SourceImportPromptContext:
    tier: SourceImportTier
    project_title: str
    style: str
    aspect_ratio: str
    episode: int
    episode_title: str
    total_episodes: int | None
    speech_rate: float | None
    source_text: str
    registered_characters: tuple[str, ...]
    registered_scenes: tuple[str, ...]
    registered_props: tuple[str, ...]
    previous_characters: tuple[str, ...]
    previous_scenes: tuple[str, ...]
    previous_props: tuple[str, ...]


def build_source_readiness_prompt(text: str, tier: SourceImportTier) -> str:
    """评估原文是否有足够、明确的叙事素材可改编为一集短剧。"""

    return "\n".join(
        [
            "# 角色",
            "你是短剧工厂的原文准入审稿人。你只评估原文是否足以改编，不写剧本，不续写原文。",
            "# 目标",
            f"评估素材是否足以支撑一集约 {tier.target_seconds} 秒的竖屏短剧，题材不限。",
            "使用红果短剧的创作方向作为质量参考：尽快进入人物困境/冲突；事件有明确因果、目标、阻碍、行动和结果；信息持续推进，避免只有背景介绍、重复解释或无事件梗概；结尾有可发展的悬念或情绪牵引。",
            "这些是审稿维度，不是固定字数或每隔固定秒数必须反转的硬规则。不得因原文是叙述体、对白少或没有引号而判不合格。",
            "只根据原文明确提供的事实判断，不得脑补人物、动机、事件、因果、反转或结局。区分‘原文明示’和‘缺失/含糊’。",
            "# 判定标准",
            "ready 只有在素材足以支持一集连贯改编时为 true。至少识别：主角/核心人物及关系（可在原文中明确或合理唯一推断）、本集要处理的具体事件/目标、阻力或冲突、至少一个可拍摄的行动/变化、行动造成的结果或后续悬念。不是要求每项都必须以显式标签出现。",
            "若只有世界观/人物介绍、情节方向、摘要结论，缺少具体事件或因果链；若核心人物/关系/动机相互矛盾；或只能靠编造关键情节才能写成连贯一集，则 ready=false。",
            f"另做一项口播体检，必须把两项分开算：① 角色实际开口说出的对白字数；② 心理活动、内心独白、无人在场的叙述可转为旁白/画外音的字数。本档目标 {tier.target_seconds} 秒：口播下限约 {tier.min_spoken_chars} 字，推荐目标约 {round(tier.target_seconds * 4.3)} 字，容量上限约 {round(tier.target_seconds * 4.5)} 字（机械分镜每段还要留约 1.2 秒画面，再长就装不进 12~15 秒的分镜段）。"
            "若①低于下限的 50%，必须在 warnings 写：原文角色对白约多少字、本档至少需要约多少字、角色台词会偏少，建议补充对白或接受更多旁白/画外音。"
            f"若①或①+②超过容量上限，必须在 warnings 写：口播约多少字、超过本档上限 {round(tier.target_seconds * 4.5)} 字，机械分镜会装不下，需要延长单集目标时长而不是压缩剧情；不要只说‘超过下限’。"
            "只有当口播总量落在下限~上限之间、但设定说明/旁白比例偏高时，才只写设定密度提示。不得用②充足来抵消①不足。",
            "问题必须具体、可操作，说明缺少什么原文事实。建议只能要求用户补充或澄清原文，不提供替用户创作的剧情；不要只说‘内容不够丰满’。",
            "# 输出格式",
            '只输出严格 JSON：{"ready":true|false,"summary":"简短判断","issues":[{"code":"缺失项短码","problem":"原文中的具体缺口","suggestion":"建议补充/澄清的原文信息"}],"warnings":["可改编但需注意的密度提示"],"evidence":["支撑判断的原文事实"]}。',
            "ready=true 时 issues 必须为空。ready=false 时至少给一项 issue。不得输出 Markdown 围栏或其他文字。",
            "# 原文",
            text,
        ]
    )


def build_source_import_prompt(ctx: SourceImportPromptContext) -> str:
    """首轮改写提示词：角色 + 项目 + 连续性 + 原文 + 写作规则 + 输出模板。"""

    tier = ctx.tier
    word_ranges = {"standard": "350~500", "long": "650~850", "manju": "260~400"}
    # 机械分镜按约 12~15 秒一段切；剧本拍数太少会切不出段，太多又会退化成电报式分镜脚本。
    # 台词优先：标准档 105 秒目标约 20 句对白 + 动作段，故拍数区间整体上移。
    min_beats = max(16, (tier.target_seconds + 4) // 5)
    max_beats = max(24, (tier.target_seconds + 2) // 3)
    # 口播预算跟着单集时长走：4.3 字/秒给画面留 1.2 秒/段；上限 4.5 字/秒。
    target_spoken_chars = max(tier.min_spoken_chars, round(tier.target_seconds * 4.3))
    max_spoken_chars = round(tier.target_seconds * 4.5)
    lines: list[str] = [
        "# 角色",
        "你是短剧编剧。把用户提供的小说 / 原文片段改编为一集竖屏短剧剧本。",
        "只输出剧本 Markdown 本身：不要解释、不要前言后记、不要代码块围栏。",
        "",
        "# 项目",
        f"- 剧名：{ctx.project_title or '（未命名项目）'}",
        f"- 风格：{ctx.style or '（未设置，按短剧写实风格）'}",
        f"- 画面比例：{ctx.aspect_ratio or '9:16'}（竖屏）",
        f"- 目标集：第 {ctx.episode} 集" + (f"「{ctx.episode_title}」" if ctx.episode_title else ""),
        f"- 篇幅档位：{tier.label}；提示词目标 {tier.target_seconds} 秒（{tier.min_seconds}~{tier.max_seconds} 秒）",
        f"- 单集场景上限：{tier.max_scenes} 个；台词行下限：{tier.min_dialogue_lines} 行",
    ]
    if ctx.total_episodes is not None:
        lines.append(f"- 全剧规划：{ctx.total_episodes} 集")
    if ctx.speech_rate is not None:
        lines.append(f"- 口播语速：{ctx.speech_rate:g} 字/秒（用于估算说话时长）")
    lines += [
        "",
        "# 故事结构锁（不可违反）",
        "原文是唯一故事事实来源。保持原文事件、人物关系、目标、动机、因果链、关键揭示、结果和事件顺序不变。不得新增/删除/调序/合并关键事件，不得改变人物选择及其后果，不得擅自补写原文没有交代的决定性信息。",
        "允许的改编仅限表达层：压缩重复、调整叙述与对白表达、把原文明确事件改写为可拍动作、让对白更口语并提高信息效率。不得以追求钩子/节奏为由改变故事结构。原文信息不足时不得脑补，按现有事实保守表达。",
        "",
        "# 连续性（可缺省）",
        "同名人物 / 场景 / 道具必须沿用下面的名字与外观，不得改名、不得改设定；本集新出现的资产也要写成「名字｜外观」，外观要具体到可出图（年龄、发型、服装、材质、光线）。",
        "已登记人物：" + ("；".join(ctx.registered_characters) or "（无）"),
        "已登记场景：" + ("；".join(ctx.registered_scenes) or "（无）"),
        "已登记道具：" + ("；".join(ctx.registered_props) or "（无）"),
        "前序集人物声明：" + ("；".join(ctx.previous_characters) or "（无）"),
        "前序集场景声明：" + ("；".join(ctx.previous_scenes) or "（无）"),
        "前序集道具声明：" + ("；".join(ctx.previous_props) or "（无）"),
        "",
        "# 原文（唯一素材）",
        ctx.source_text,
        "",
        "# 写作规则",
        "1. 只写目标集：不写其他集，不写‘未完待续’之外的收束说明。",
        f"2. 第一行必须是：第【{ctx.episode}】集：本集标题。",
        "3. 头部字段必须齐全，且只用这些字段：【所属阶段】【黄金前3秒钩子】【情绪流变】【本集时长】"
        + ("【本集结构】【本集中段小钩】" if tier.requires_mid_hook else "")
        + "【本集断章卡点】。字段只概括原文已有内容，不得虚构。",
        f"4. [场景] 1~{tier.max_scenes} 行，必须紧接头部之后、任何动作/台词之前；每行「场景说明｜外观」，外观写空间、材质、光线、时间。",
        "5. [人物] 一行，顿号分隔每个出场人物；每人写成「名（状态标签｜外观）」，外观必填。",
        "6. [道具] 一行一个「名称｜外观」，只列参与剧情的道具；正文里必须写出道具名字。",
        "7. 动作行以 △ 开头，一行一拍：一拍是一个完整动作段，可含连续动作与可见结果（谁做了什么、环境随之怎么变），用逗号/句号连成自然语句；不要把同一个动作拆成多行电报式短句，也不要写成标签或心理描写。",
        f'8. 每句独占一行：「角色名（语气）："台词。"」；只有原文里角色**实际开口说出**的话才用这个格式；单句常规 10~18 字，硬上限 25 字；角色台词不少于 {tier.min_dialogue_lines} 行（目标 {tier.min_dialogue_lines + 8} 行），本集口播（角色台词 + 画外音）不少于 {tier.min_spoken_chars} 字（目标约 {target_spoken_chars} 字）。可以扩写成问答、追问、打断和短句来回，但不得替角色说出原文没有的心里话，也不得为了口播量新增剧情事实。',
        f"8.0 口播不要贴着 5 字/秒顶满：机械分镜每段还要留约 1.2 秒给画面，推荐把总口播控制在 {tier.min_spoken_chars}~{target_spoken_chars} 字；超过 {max_spoken_chars} 字会装不进 12~15 秒的分镜段。装不下时应延长单集目标时长，不要靠删台词压剧情。",
        '8.1 画外音 / 内心独白仍以角色名开头，用括号标记：「萧炎（内心独白）："……"」「萧炎（画外音）："……"」；纯叙述用「旁白（画外音）："……"」。这些是画外音，不是角色开口，不得写成普通台词。',
        "8.2 心理活动、内心独白、回忆、无人在场的叙述、环境/时间/前情交代，一律用 8.1 的画外音格式，不得改写成角色问答；角色台词只写他真正说出口的话。",
        f"9. 全集合计 {min_beats}~{max_beats} 拍（△ 动作段 + 台词行），不要为了凑拍把连续动作拆成多行。按约 12~15 秒一段的节奏组织，让后续机械分镜能直接切段；动作段与台词段要交替推进，避免整段只有动作或只有对白。",
        "10. 每个动作/台词 beat 至少承担一种作用：推进原文事件或因果、体现人物对既有信息的反应、呈现已有冲突/情绪变化。不得用重复反应或无关空镜凑拍。无对白 beat 必须呈现有剧情功能的可见行动、线索或反应。",
        "11. 禁止镜头语言（景别、角度、运镜、转场），这些由 AI 分镜负责；原文心理活动、内心独白保持旁白/画外音，不改成角色台词；只有原文明确是角色开口说的话才写成台词，其余叙述可改写成符合原意的表情或动作。",
        "12. 保留原文主线与人物关系；砍支线、并人物、压缩时间线时不得改变因果或事件先后；对白不整段照抄原文。",
        "13. 禁止 JSON、代码围栏、解释性段落、自造【…】头部字段。",
        "14. 结尾必须有【本集断章卡点】：只能使用原文结尾或原文已建立的未解问题，不得新增反转/威胁/证据。",
        "15. 输出前逐项对照原文，确认关键事件与顺序一致；如某项无法在不脑补的情况下写出，不得伪造，保持原文现有信息。",
        "",
        "# 输出模板",
        f"第【{ctx.episode}】集：{ctx.episode_title or '本集标题'}",
        f"【所属阶段】：第X幕・（第 {ctx.episode}/{ctx.total_episodes or 'N'} 集 ｜ 本集定位）",
        "【黄金前3秒钩子】：仅选择原文已有冲突/危机/信息差；无依据时如实写最强已有事件，不虚构",
        "【情绪流变】：按原文事件顺序概括已有情绪变化",
        f"【本集时长】：{tier.target_seconds} 秒（约 {word_ranges.get(tier.name, '350~500')} 字）",
    ]
    if tier.requires_mid_hook:
        lines += [
            "【本集结构】：按原文事件整理双回合；不得创造新回合事实",
            "【本集中段小钩】：仅从原文已有事件/信息中选择",
        ]
    lines += [
        "[场景]：内景・地点・夜｜场景外观",
        "[人物]：角色名（状态标签｜外观）、角色名（状态标签｜外观）",
        "[道具]：道具名｜外观",
        "△ 角色名 + 连续动作 + 可见结果（一拍一段，不拆成多行）",
        "角色名（神态）：",
        '"台词。"',
        "△ 下一拍动作段",
        "【本集断章卡点】：原文已有信息形成的画面停格描述",
        "",
        "# 输出前自检",
        "- 第一行集号正确；头部字段齐全；场景/人物/道具声明格式正确。",
        "- 每拍都有叙事功能；无依据的旁白、台词、事件、反转均未添加。",
        "- 台词行数、拍数达标；没有代码围栏、解释或镜头语言。",
    ]
    return "\n".join(lines)


def build_source_import_repair_prompt(base_prompt: str, previous_output: str, failures: list[str]) -> str:
    """返修生成稿，约束只修质量缺陷且保留故事结构。"""

    failure_lines = "\n".join(f"{index}. {failure}" for index, failure in enumerate(failures, start=1))
    return "\n".join(
        [
            base_prompt,
            "",
            "# 返修任务",
            "修复下列问题，但必须遵守上述‘故事结构锁’：不得新增、删除、调序、合并关键事件，不得改变人物关系/目标/因果/揭示/结果。只优化表达、压缩冗余、细化原文已有动作和反应。无法在结构锁内修复的项必须保留并报告，不能编造来凑合格。",
            "## 上一版",
            previous_output,
            "## 必须修复的问题",
            failure_lines,
            "只输出修订后的完整剧本 Markdown。",
        ]
    )
