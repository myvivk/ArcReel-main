"""资产 AI 补全：上下文分组与 prompt 优先级（D-35）。"""

from __future__ import annotations

from pathlib import Path

from server.services.asset_ai_fill import (
    build_ai_fill_prompt,
    collect_asset_context,
)


def _project(*episodes) -> dict:
    return {
        "title": "测试剧",
        "premise": "一句前提",
        "episodes": list(episodes),
    }


def _episode(episode: int, script_file: str) -> dict:
    return {"episode": episode, "script_file": script_file}


def _write_script(project_dir: Path, name: str, scene: str, speaker: str) -> None:
    import json

    path = project_dir / "scripts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "scenes": [
                    {
                        "scene": scene,
                        "scene_description": f"{scene}的描写",
                        "characters_in_scene": [speaker],
                        "dialogue": [{"speaker": speaker, "line": "这句是本集的台词"}],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class TestCollectAssetContext:
    def test_the_focused_episode_excerpts_are_separated_from_other_episodes(self, tmp_path: Path):
        _write_script(tmp_path, "ep1.json", "本集场景", "阿岚")
        _write_script(tmp_path, "ep2.json", "别集场景", "阿岚")
        project = _project(_episode(1, "scripts/ep1.json"), _episode(2, "scripts/ep2.json"))

        context = collect_asset_context(tmp_path, project, "character", "阿岚", {}, episode=1)

        assert [item["scene"] for item in context["episode_excerpts"]] == ["本集场景"]
        assert [item["scene"] for item in context["other_excerpts"]] == ["别集场景"]
        assert context["focus_episode"] == "1"

    def test_without_an_episode_every_script_is_one_pool(self, tmp_path: Path):
        _write_script(tmp_path, "ep1.json", "本集场景", "阿岚")
        _write_script(tmp_path, "ep2.json", "别集场景", "阿岚")
        project = _project(_episode(1, "scripts/ep1.json"), _episode(2, "scripts/ep2.json"))

        context = collect_asset_context(tmp_path, project, "character", "阿岚", {})

        assert context["focus_episode"] is None
        assert context["other_excerpts"] == []
        assert [item["scene"] for item in context["episode_excerpts"]] == ["本集场景", "别集场景"]


class TestPriorityInPrompt:
    def test_episode_context_puts_the_episode_script_above_canon(self):
        context = {
            "title": "测试剧",
            "premise": "一句前提",
            "existing": {},
            "episode_excerpts": [{"scene": "本集场景", "scene_description": "描写", "dialogue": ["阿岚: 台词"]}],
            "other_excerpts": [],
            "canon": ["人物圣经里的旧设定"],
            "focus_episode": "3",
        }

        prompt = build_ai_fill_prompt("character", "阿岚", context)

        assert "本集正式剧本 > 项目设定 > 其他集剧本摘录 > 题材补全。" in prompt
        # 本集定稿块排在项目设定之前：模型先看到的是本集出场的场景与台词。
        assert prompt.index("【本集正式剧本摘录") < prompt.index("【项目设定")
        assert prompt.index("【项目设定") < prompt.index("【其他集剧本摘录")
        assert "这句是本集的台词" not in prompt

    def test_global_context_keeps_canon_first(self):
        context = {
            "title": "测试剧",
            "premise": "一句前提",
            "existing": {},
            "episode_excerpts": [{"scene": "某场景", "scene_description": "描写", "dialogue": []}],
            "other_excerpts": [],
            "canon": ["人物圣经里的设定"],
            "focus_episode": None,
        }

        prompt = build_ai_fill_prompt("character", "阿岚", context)

        assert "项目设定 > 剧本摘录 > 题材补全。" in prompt
        assert prompt.index("【项目设定") < prompt.index("剧本摘录：")

    def test_existing_fields_are_listed_as_untouchable(self):
        context = {
            "title": "测试剧",
            "premise": "一句前提",
            "existing": {"description": "已有的描述"},
            "episode_excerpts": [],
            "other_excerpts": [],
            "canon": [],
            "focus_episode": None,
        }

        prompt = build_ai_fill_prompt("scene", "茶楼", context)

        assert "description（已有，勿改）：已有的描述" in prompt
        assert "已有内容绝不覆盖" in prompt
