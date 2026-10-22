"""资产 AI 生成描述：上下文分组与 prompt 优先级（D-35）。

优先级固定为「剧本（工厂源稿）> 故事规划 > 题材补全」，集页视图与侧栏「全部资产」入口同一
口径——``episode`` 只把剧本摘录聚焦到本集，不再翻转块序。
"""

from __future__ import annotations

from pathlib import Path

from server.services.asset_ai_fill import (
    AssetFillResponse,
    apply_asset_fill,
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
    def test_factory_original_overrides_visual_json_and_excludes_reports(self, tmp_path: Path):
        _write_script(tmp_path, "ep1.json", "视觉派生中的错误场景", "阿岚")
        source = tmp_path / "source/short_drama/episodes/episode_01.md"
        source.parent.mkdir(parents=True)
        source.write_text(
            "[场景]：茶楼｜临街木窗\n[人物]：阿岚（主角｜白衣，右眉有疤）\n△ 阿岚握紧茶杯。\n阿岚：原稿台词。\n",
            encoding="utf-8",
        )
        (source.parent.parent / "character-bible.md").write_text(
            "【角色】：阿岚\n【外形】：旧版外观\n[人物]：阿岚｜不可混进正文\n",
            encoding="utf-8",
        )
        context = collect_asset_context(
            tmp_path,
            _project(_episode(1, "ep1.json")),
            "character",
            "阿岚",
            {},
            episode=1,
        )
        prompt = build_ai_fill_prompt("character", "阿岚", context)
        assert "阿岚握紧茶杯" in prompt
        assert "原稿台词" in prompt
        assert "视觉派生中的错误场景" not in prompt
        assert len(context["script_source"]) == 1
        assert "白衣，右眉有疤" in context["script_source"][0]

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
    def test_episode_context_puts_the_script_above_the_story_plan(self):
        context = {
            "title": "测试剧",
            "premise": "一句前提",
            "existing": {},
            "episode_excerpts": [{"scene": "本集场景", "scene_description": "描写", "dialogue": ["阿岚: 台词"]}],
            "other_excerpts": [],
            "script_source": ["episode_03.md｜[人物]：阿岚｜白衣少女"],
            "story_plan": ["【人物圣经｜character-bible.md】\n性格设定"],
            "focus_episode": "3",
        }

        prompt = build_ai_fill_prompt("character", "阿岚", context)

        assert "剧本（工厂源稿）> 故事规划 > 题材补全。" in prompt
        # 剧本块排在最前：模型先看到的是本集出场的场景与台词、工厂源稿标记行里的外观。
        assert prompt.index("【剧本（工厂源稿）") < prompt.index("【故事规划")
        assert prompt.index("【故事规划") < prompt.index("【题材补全")
        assert "这句是本集的台词" not in prompt

    def test_global_context_keeps_the_same_block_order(self):
        context = {
            "title": "测试剧",
            "premise": "一句前提",
            "existing": {},
            "episode_excerpts": [{"scene": "某场景", "scene_description": "描写", "dialogue": []}],
            "other_excerpts": [],
            "script_source": ["episode_01.md｜[人物]：阿岚｜白衣少女"],
            "story_plan": ["【人物圣经｜character-bible.md】\n性格设定"],
            "focus_episode": None,
        }

        prompt = build_ai_fill_prompt("character", "阿岚", context)

        # 与集页视图同一口径：块序不按 episode 翻转。
        assert "剧本（工厂源稿）> 故事规划 > 题材补全。" in prompt
        assert prompt.index("【剧本（工厂源稿）") < prompt.index("【故事规划")

    def test_existing_fields_are_listed_as_untouchable(self):
        context = {
            "title": "测试剧",
            "premise": "一句前提",
            "existing": {"description": "已有的描述"},
            "episode_excerpts": [],
            "other_excerpts": [],
            "script_source": [],
            "story_plan": [],
            "focus_episode": None,
        }

        prompt = build_ai_fill_prompt("scene", "茶楼", context)

        assert "description（已有，勿改）：已有的描述" in prompt
        assert "已有内容绝不覆盖" in prompt


def test_fill_replaces_only_blank_description():
    blank = {"description": "  "}
    assert apply_asset_fill(blank, "scene", "茶楼", AssetFillResponse(description="临街木窗")) == ["description"]
    assert blank["description"] == "临街木窗"
    manual = {"description": "用户外观"}
    assert apply_asset_fill(manual, "scene", "茶楼", AssetFillResponse(description="AI 外观")) == []
    assert manual["description"] == "用户外观"
