"""Tests for 工具参数 schema 的派生：判别联合内联，$defs / $ref / discriminator 不出现。"""

from __future__ import annotations

from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from server.agent_toolset.declaration import AgentToolDeclaration, request_json_schema
from server.agent_toolset.media_generation import GENERATE_VIDEOS
from server.agent_toolset.toolset import AGENT_TOOLSET


class _EpisodeTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Literal["episode"]
    episode_id: int = Field(ge=1)


class _SelectedTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Literal["selected"]
    ids: list[str] = Field(min_length=1)


class _TargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: _EpisodeTarget | _SelectedTarget = Field(discriminator="scope")


class _Node(BaseModel):
    value: str
    child: _Node | None = None


class _RecursiveRequest(BaseModel):
    root: _Node


def _walk(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, dict):
        return [node, *[found for value in node.values() for found in _walk(value)]]
    if isinstance(node, list):
        return [found for item in node for found in _walk(item)]
    return []


# ---------------------------------------------------------------------------
# request_json_schema — 判别联合
# ---------------------------------------------------------------------------


def test_discriminated_union_branches_are_inlined() -> None:
    """每个分支自带字段定义：不解析 $ref 的调用方也能看到 episode_id 是整数、ids 是数组。"""

    schema = request_json_schema(_TargetRequest)
    branches = schema["properties"]["target"]["oneOf"]

    assert "$defs" not in schema
    assert {branch["properties"]["scope"]["const"] for branch in branches} == {"episode", "selected"}
    assert branches[0]["properties"]["episode_id"]["type"] == "integer"
    assert branches[1]["properties"]["ids"]["type"] == "array"


def test_schema_carries_no_ref_or_discriminator() -> None:
    schema = request_json_schema(_TargetRequest)

    assert not any("$ref" in node for node in _walk(schema))
    assert not any("discriminator" in node for node in _walk(schema))


def test_recursive_model_keeps_its_defs() -> None:
    """自引用模型内联不完：原样保留 $defs 与 $ref，既不崩也不静默截断。"""

    schema = request_json_schema(_RecursiveRequest)

    assert schema["properties"]["root"]["$ref"].startswith("#/$defs/")
    assert "$defs" in schema


# ---------------------------------------------------------------------------
# 已声明工具的 schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("declaration", AGENT_TOOLSET, ids=lambda declaration: declaration.name)
def test_every_tool_schema_is_dereferenced(declaration: AgentToolDeclaration) -> None:
    """工具目录里没有一个 schema 还带 $ref / discriminator：留一个就让调用方重新猜嵌套字段的类型。"""

    schema = declaration.input_schema

    assert not any("$ref" in node for node in _walk(schema))
    assert not any("discriminator" in node for node in _walk(schema))


def test_generate_videos_target_leaves_are_typed() -> None:
    """被严格类型拒收的那两个叶子，在 schema 里必须自带类型。"""

    branches = GENERATE_VIDEOS.input_schema["properties"]["target"]["oneOf"]
    by_scope = {branch["properties"]["scope"]["const"]: branch for branch in branches}

    assert by_scope["episode"]["properties"]["episode_id"]["type"] == "integer"
    assert by_scope["selected"]["properties"]["ids"]["type"] == "array"
