"""工厂项目的列出与创建工具。"""

from __future__ import annotations

from server.agent_toolset.declaration import UnscopedToolDeclaration
from server.tool_runtime import (
    CreateProjectToolRequest,
    NoArguments,
    create_project,
    list_projects,
)

LIST_PROJECTS = UnscopedToolDeclaration(
    name="list_projects",
    description=(
        "列出当前项目根下可供后续工具寻址的 ArcReel 项目，返回每个项目的 name、标题、内容模式与生成模式。"
        "缺少 project.json 或无法读取的目录不列出。只读，无副作用。"
    ),
    request_model=NoArguments,
    domain_key="projects",
    handler=list_projects,
)

CREATE_PROJECT = UnscopedToolDeclaration(
    name="create_project",
    description=(
        "创建短剧工厂项目，固定采用 drama / storyboard，返回项目 name 与元数据。"
        "保留标题、画面比例、视觉风格、模型快照和声音配置。同名项目已存在时拒绝覆盖；写入失败时回滚目录。"
    ),
    request_model=CreateProjectToolRequest,
    domain_key="project",
    handler=create_project,
)

PROJECT_ENTRY_TOOLS = (LIST_PROJECTS, CREATE_PROJECT)

__all__ = [
    "CREATE_PROJECT",
    "LIST_PROJECTS",
    "PROJECT_ENTRY_TOOLS",
]
