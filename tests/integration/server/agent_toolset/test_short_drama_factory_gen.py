"""工厂文本生成工具 handler 测试：验证写作经文本模型、返回原文、截断报错。"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.backends.providers import CallPurpose
from lib.backends.text_backends.base import TextGenerationResult, TextTaskType
from server.agent_toolset.script_authoring import GENERATE_SHORT_DRAMA_FACTORY
from server.tool_runtime import (
    ProjectScope,
    ShortDramaFactoryGenRequest,
    ToolOutcome,
    ToolRequest,
    generate_short_drama_factory,
)
from server.agent_toolset.declaration import BLOCKED


class _FakeGenerator:
    def __init__(self, *, task_type, purpose, text="人物：林夏\n台词：开门。\n"):
        self.task_type = task_type
        self.purpose = purpose
        self.text = text
        self.model = "factory-model"
        self.provider = "factory-provider"

    async def generate(self, request, *, require_complete=False):
        return TextGenerationResult(
            text=self.text,
            provider=self.provider,
            model=self.model,
            truncated=False,
        )


@pytest.fixture
def scope():
    return ProjectScope(project_name="demo", data_root=Path("/tmp"))


async def test_factory_gen_uses_text_model_and_returns_markdown(monkeypatch, scope) -> None:
    captured: dict = {}

    async def fake_create(task_type, project_name, *, purpose, user_id="default"):
        captured["task_type"] = task_type
        captured["purpose"] = purpose
        return _FakeGenerator(task_type=task_type, purpose=purpose)

    monkeypatch.setattr("server.tool_runtime.TextGenerator.create", staticmethod(fake_create))

    outcome: ToolOutcome = await generate_short_drama_factory(
        ToolRequest(
            ShortDramaFactoryGenRequest(
                prompt="写第 1 集",
                system_prompt="短剧风格",
                artifact="episode_01",
            )
        ),
        scope,
        _caller=None,
        _services=None,
    )

    assert outcome.problem is None, outcome
    assert captured["task_type"] is TextTaskType.FACTORY
    assert captured["purpose"] is CallPurpose.SCRIPT_GENERATION
    assert "林夏" in outcome.value["text"]
    assert outcome.value["artifact"] == "episode_01"
    assert outcome.value["model"] == "factory-model"


async def test_factory_gen_truncation_is_problem(monkeypatch, scope) -> None:
    from lib.backends.text_backends.base import TextOutputTruncatedError

    async def fake_create(task_type, project_name, *, purpose, user_id="default"):
        return _FakeGenerator(task_type=task_type, purpose=purpose)

    async def fake_generate(self, request, *, require_complete=False):
        raise TextOutputTruncatedError(provider="factory-provider", model="factory-model", output_tokens=100)

    monkeypatch.setattr("server.tool_runtime.TextGenerator.create", staticmethod(fake_create))
    monkeypatch.setattr(_FakeGenerator, "generate", fake_generate)

    outcome = await generate_short_drama_factory(
        ToolRequest(ShortDramaFactoryGenRequest(prompt="写第 1 集")),
        scope,
        _caller=None,
        _services=None,
    )
    assert outcome.problem is not None
    assert outcome.problem.code == "text_output_truncated"


def test_tool_declaration_is_blocked_and_registered() -> None:
    assert GENERATE_SHORT_DRAMA_FACTORY.migration is BLOCKED
    assert GENERATE_SHORT_DRAMA_FACTORY.name == "generate_short_drama_factory"
    from server.agent_toolset.script_authoring import SCRIPT_AUTHORING_TOOLS

    assert GENERATE_SHORT_DRAMA_FACTORY in SCRIPT_AUTHORING_TOOLS
