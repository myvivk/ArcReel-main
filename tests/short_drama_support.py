"""完整工厂交付包，供入库单测与 HTTP / Agent 集成测试共用。"""

import base64
import re
from pathlib import Path


def factory_episode() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "agent_runtime_profile/.claude/skills/short-drama-factory/examples/ep01-longwang-demo.md"
    ).read_text(encoding="utf-8")


def factory_package(markdown: str | None = None, *, episode: int = 1) -> list[tuple[str, bytes]]:
    text = factory_episode() if markdown is None else markdown
    characters = re.findall(
        r"([^、\n]+)[（(][^｜|）)]*[｜|]([^）)]+)[）)]",
        next(line.removeprefix("[人物]：") for line in text.splitlines() if line.startswith("[人物]")),
    )
    bible = "\n".join(f"【角色】：{name}\n【外形】：{appearance}" for name, appearance in characters)
    ledger = (
        f"# 台账\n当前集: {episode}\n总集数: 80\n"
        "## 伏笔账\n| ID | 内容 | 埋于 | 拟收 | 状态 | 备注 |\n|---|---|---|---|---|---|\n"
        "## 人物账\n| 角色 | 首出 | 状态 | 关系 | 变动集 | 备注 |\n|---|---|---|---|---|---|\n"
        + "".join(f"| {name} | 1 | 存活 | 主角 | {episode} | |\n" for name, _ in characters)
        + "## 道具账\n| 道具 | 持有 | 状态 | 变动集 |\n|---|---|---|---|\n"
        "## 世界观规则账\n| 规则 | 确立集 | 引用集 | 违反 |\n|---|---|---|---|\n"
        "一级墙 第12集 / 二级墙 第30集 / 三级墙 第56集\n"
    )
    contents = {
        "character-bible.md": bible,
        "series-outline.md": "# 全剧大纲\n情绪契约：受辱反击。\n",
        "ledger.md": ledger,
        f"episode-plan/episode_{episode:02d}.plan.md": "# 分集规划\n前段施压，反击后留钩。\n",
        f"episodes/episode_{episode:02d}.md": text,
        f"reports/episode_{episode:02d}.machine.md": "# 机检报告\n结果: PASSED\n",
        f"compliance/episode_{episode:02d}.compliance.md": "# 合规报告\n结果: PASSED\n",
    }
    return [(path, content.encode("utf-8")) for path, content in contents.items()]


def artifact_request(package: list[tuple[str, bytes]]) -> dict[str, list[dict[str, str]]]:
    return {
        "artifacts": [{"path": path, "content_base64": base64.b64encode(raw).decode("ascii")} for path, raw in package]
    }
