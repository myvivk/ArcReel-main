# ArcReel 外部 Agent 接入任务

你正在帮助用户把当前 Agent 宿主连接到 ArcReel。完成以下步骤后再结束任务；项目创作使用短剧工厂唯一链路，按当前 MCP 工具说明完成工厂生成、七类工件入库、资产、AI 分镜、K 帧确认、独立视频提示词、视频和剪辑导出。

## 1. 安装 ArcReel skills

在当前 Agent 的工作目录运行：

```bash
npx skills add ArcReel/skills
```

确认安装结果同时包含 `setup-arcreel-skills` 与 `video-workflow`。

## 2. 获取接入信息

让用户打开 [ArcReel 设置页的「访问令牌」]({{BASE_URL}}/app/settings?section=access-tokens)，创建一个 `arc-` 前缀的访问令牌（即 API Key）。完整令牌只显示一次；用户可以把它提供给其明确选择的当前 Agent。`setup-arcreel-skills` 会按宿主与工作区的本地配置惯例持久化同一连接，供后续会话与 ArcReel skills 复用。

MCP 端点：

```text
{{BASE_URL}}/mcp
```

## 3. 执行接线

安装完成后立即使用 `setup-arcreel-skills` skill，按其指引配置 MCP 端点、Bearer 访问令牌并验证连通；`video-workflow` 会在后续创作请求中按需触发。

旧版外部 skills 可能引用已移除的普通源文、原生规划、通用草稿或参考单元工具。接线后重新获取 MCP 工具清单，以服务器当前声明为准；内容交稿仅使用 `generate_short_drama_factory` 和 `ingest_short_drama_artifacts`，AI 分镜使用 `plan_short_drama_storyboards`，提示词编写使用 `generate_episode_script`，一次只生成 `image_prompt` 或 `video_prompt`。

## 4. 完成判据

`list_projects` 调用成功并返回结构化 `projects` 列表，且宿主 MCP 与工作区配置指向同一连接时即完成；空列表也是成功。
