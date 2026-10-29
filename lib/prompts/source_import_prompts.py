"""导入原文的提示词装配：把项目/连续性/原文与工厂单集格式契约拼成一次改写请求。

不走 ``prompt_templates`` 引擎：那套模板的 stage/trigger 是闭集且面向前端可编辑，导入原文是
服务端内部提示词，放这里避免为一个新链路改前端设置页（方案 §6.5）。

输出模板严格取自 ``short-drama-factory/templates/episode-format.md``，并逐条对应解析器契约
（``lib.script.short_drama_import.parse_short_drama_episode``）：不新增剧本格式、不允许代码块、
不允许自造头部字段，场景/人物/道具声明集中在 beat 之前。
"""

from __future__ import annotations

from dataclasses import dataclass

from lib.script.source_import import SourceImportTier


@dataclass(frozen=True, slots=True)
class SourceImportPromptContext:
    """一次导入改写的全部输入：项目事实、连续性、前序集声明与原文。"""

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


_WORD_RANGES: dict[str, str] = {
    "standard": "350~500",
    "long": "650~850",
    "manju": "260~400",
}


def _asset_lines(title: str, lines: tuple[str, ...], empty: str) -> list[str]:
    body = [f"- {line}" for line in lines] if lines else [f"- {empty}"]
    return [f"{title}：", *body]


def _format_speech_rate(value: float | None) -> str | None:
    if value is None:
        return None
    return f"{value:g} 字/秒（用于估算说话时长）"


def build_source_import_prompt(ctx: SourceImportPromptContext) -> str:
    """首轮改写提示词：角色 + 项目 + 连续性 + 原文 + 写作规则 + 输出模板。"""

    tier = ctx.tier
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
    if (speech_rate := _format_speech_rate(ctx.speech_rate)) is not None:
        lines.append(f"- 口播语速：{speech_rate}")
    lines += [
        "",
        "# 连续性（可缺省）",
        "同名人物 / 场景 / 道具必须沿用下面的名字与外观，不得改名、不得改设定；本集新出现的资产",
        "也要写成「名字｜外观」，外观要具体到可出图（年龄、发型、服装、材质、光线）。",
        *_asset_lines("已登记人物", ctx.registered_characters, "（无）"),
        *_asset_lines("已登记场景", ctx.registered_scenes, "（无）"),
        *_asset_lines("已登记道具", ctx.registered_props, "（无）"),
        *_asset_lines("前序集人物声明", ctx.previous_characters, "（无）"),
        *_asset_lines("前序集场景声明", ctx.previous_scenes, "（无）"),
        *_asset_lines("前序集道具声明", ctx.previous_props, "（无）"),
        "",
        "# 原文（唯一素材）",
        ctx.source_text,
        "",
        "# 写作规则",
        "1. 只写目标集：不写其他集，不写「未完待续」之外的收束说明。",
        f"2. 第一行必须是：第【{ctx.episode}】集：本集标题。",
        "3. 头部字段必须齐全，且只用这些字段："
        "【所属阶段】【黄金前3秒钩子】【情绪流变】【本集时长】"
        + ("【本集结构】【本集中段小钩】" if tier.requires_mid_hook else "")
        + "【本集断章卡点】。",
        "4. [场景] 1~" + str(tier.max_scenes) + " 行，必须紧接头部之后、任何动作/台词之前；"
        "每行「场景说明｜外观」，外观写空间、材质、光线、时间。",
        "5. [人物] 一行，顿号分隔每个出场人物；每人写成「名（状态标签｜外观）」，外观必填。",
        "6. [道具] 一行一个「名称｜外观」，只列参与剧情的道具；正文里必须写出道具的名字。",
        "7. 动作行以 △ 开头，一行一拍：写清「谁 + 做什么动作 + 可见结果」（光、物体、身体状态），"
        "禁止只写标签，禁止心理描写。",
        '8. 台词两行式：第一行「角色名（神态）：」，第二行「"台词。"」；'
        "常规 12~18 字，硬上限 25 字；本集台词行不少于 " + str(tier.min_dialogue_lines) + " 行。",
        f"9. 本集拍数（△ 动作行 + 台词行）不少于 ceil({tier.target_seconds} / 2) = "
        + str((tier.target_seconds + 1) // 2)
        + " 拍；一拍一句，不合并、不概括。",
        "10. 禁止镜头语言（景别、角度、运镜、转场），这些由 AI 分镜负责；心理活动改写成表情、动作或画外音。",
        "11. 保留原文主线与人物关系；砍支线、并人物、压缩时间线；对白不整段照抄原文。",
        "12. 禁止 JSON、禁止代码块围栏、禁止解释性段落、禁止自造【…】头部字段。",
        "13. 结尾必须有【本集断章卡点】：写画面停格或黑屏钩子。",
        "",
        "# 输出模板（严格按此结构与顺序）",
        f"第【{ctx.episode}】集：{ctx.episode_title or '本集标题'}",
        "【所属阶段】：第X幕・（第 " + str(ctx.episode) + "/" + str(ctx.total_episodes or "N") + " 集 ｜ 本集定位）",
        "【黄金前3秒钩子】：五母型之一（生死绝境 / 极端羞辱 / 身份暴涨 / 物证背叛 / 规则悬念）",
        "【情绪流变】：A ➔ B ➔ C ➔ 绝命断章",
        f"【本集时长】：{tier.target_seconds} 秒（约 {_WORD_RANGES[tier.name]} 字）",
    ]
    if tier.requires_mid_hook:
        lines += [
            "【本集结构】：双回合（回合A ____ ➔ 回合B ____）",
            "【本集中段小钩】：≈90 秒处的新信息 / 新威胁 / 反转半步",
        ]
    lines += [
        "[场景]：内景・地点・夜｜场景外观",
        "[人物]：角色名（状态标签｜外观）、角色名（状态标签｜外观）",
        "[道具]：道具名｜外观",
        "△ 角色名 + 动作 + 可见结果",
        "角色名（神态）：",
        '"台词。"',
        "△ 下一拍动作",
        "【本集断章卡点】：画面停格描述。（黑屏：下集钩子）",
        "",
        "# 输出前自检",
        "- 第一行集号正确；头部字段齐全（长档另加【本集结构】【本集中段小钩】），【本集时长】是整数秒。",
        "- [场景]/[人物]/[道具] 都在动作与台词之前；每个人物、道具都有「｜」外观。",
        "- 台词行数达标、总拍数达标；没有代码块、没有解释、没有镜头语言。",
    ]
    return "\n".join(lines)


def build_source_import_repair_prompt(
    base_prompt: str,
    previous_output: str,
    failures: list[str],
) -> str:
    """返修提示词：带上首轮输出与失败项，要求只修失败项、其余保持。"""

    failure_lines = "\n".join(f"{index}. {failure}" for index, failure in enumerate(failures, start=1))
    return "\n".join(
        [
            base_prompt,
            "",
            "# 上一轮输出（未通过校验）",
            previous_output.strip(),
            "",
            "# 必须修复的失败项",
            failure_lines,
            "",
            "# 返修要求",
            "按上面的输出模板与写作规则重新输出完整的一集剧本；只修正这些失败项，",
            "不需要改的地方尽量保持原文。不要解释、不要代码块围栏。",
        ]
    )


__all__ = [
    "SourceImportPromptContext",
    "build_source_import_prompt",
    "build_source_import_repair_prompt",
]
