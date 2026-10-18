# 短剧 Markdown 源稿与重新导入实现规格

本文记录短剧工厂 Markdown 从首次导入、持久化编辑、重新导入到后续视觉提示词编写的完整实现。修改短剧导入、剧本页、分镜页动作、正式脚本写入、资产登记或分集删除前，先阅读本文。

本文对应基线提交 `adce16372f7a0c5a5fbc950d0083de2fc1ca5187` 上的未提交实现。当前工作区源码是最终事实源；本文用于在相同基线上完整复建实现、审查行为是否缺失，以及在后续重构时保持契约。

## 目标行为

短剧工厂生成的单集 Markdown 是该集的可编辑内容源稿。正式脚本是 Markdown 解析后的生产结构，不是源稿的替代品。

用户流程固定为：

1. 首次导入短剧 Markdown，或由短剧工厂生成后自动导入。
2. 系统保存 Markdown 到项目目录，并生成正式剧情演绎脚本。
3. 用户在集页「剧本」页签编辑 Markdown。
4. 「保存源稿」只保存 Markdown，不修改正式脚本，解析失败的草稿也允许保存。
5. 「重新导入」从已保存 Markdown 重建正式脚本。
6. 用户进入「分镜」页签，通过「编写提示词」补齐 `image_prompt` 和 `video_prompt`。
7. 用户显式触发分镜图和视频生成。

重新导入不执行以下动作：

- 不调用视频、图像或文本生成供应商。
- 不生成视觉提示词。
- 不生成分镜图或视频。
- 不把结构化正式脚本反向伪装成历史项目的原始 Markdown。

## 核心不变量

实现和重构必须同时满足以下条件：

1. Markdown 固定存储在 `source/short_drama/episode_{N}.md`。
2. 源稿 revision 是文件原始 UTF-8 字节的 SHA-256 十六进制摘要。
3. 正式脚本的 `short_drama_meta.source_path` 和 `short_drama_meta.source_revision` 必须指向本次解析使用的源稿。
4. 单独保存源稿采用乐观并发控制；revision 不匹配返回 HTTP 409。
5. 重新导入只接受当前已保存源稿的 revision，不接受请求体携带另一份 Markdown。
6. 正式脚本、项目集索引和缺失资产登记在 `ProjectManager.save_script(..., project_update=...)` 的同一项目事务内提交。
7. 正式脚本提交前，在项目锁内再次读取源稿并核对 revision；源稿在解析与提交之间变化时回滚脚本提交。
8. 首次导入和批量导入必须登记脚本引用的角色、场景和道具。
9. 已登记资产不覆盖；跨类型重名记录为 `asset_conflicts`，不破坏已有资产。
10. 重新导入前检测现有分镜图和视频；存在媒体时必须由用户显式确认。
11. 重新导入保留旧媒体文件，由 Artifact Manifest 将其判为 stale；不自动删除或重新生成媒体。
12. 导入阶段不读取视频供应商能力。拆幕使用项目 `default_duration`；该值缺失或非法时使用 8 秒。
13. 视频模型时长档位只在后续视频生成准入阶段校验。
14. 历史导入没有 Markdown 时，接口返回 `exists=false`，页面明确提示用户重新粘贴；不得从 `source_text` 或正式脚本拼接伪造原稿。
15. 删除分集时必须同时删除对应短剧 Markdown，并纳入正式文件回滚范围。
16. `PATCH /script-scenes/{scene_id}` 必须允许保存 `scene_description`。
17. 已导入短剧的分镜页隐藏「AI 原创剧本」「导入短剧工厂剧本」「AI 生成剧本」，只保留「编写提示词」和媒体生成入口。
18. 在线程中写源稿或正式脚本必须使用 `run_sync_transaction`，请求取消也要等磁盘事务结算后再传播。
19. 必须保留主线 `patch_episode_script` 的可选 `base_revision` 与 `update.fields` 工具说明，短剧工作流不得回退通用 Agent 编辑能力。

## 数据模型

### 源稿

后端领域对象位于 `lib/script/short_drama_source.py`：

```python
@dataclass(frozen=True, slots=True)
class ShortDramaSource:
    episode: int
    path: str
    markdown: str
    revision: str
```

路径函数：

```python
short_drama_source_relative_path(episode)
# source/short_drama/episode_{episode}.md
```

`episode` 必须是正整数。源稿使用 UTF-8 编解码。`load_short_drama_source` 在文件不存在时返回 `None`。

### 正式脚本来源信息

转换结果保留原有 `short_drama_meta` 字段，并增加：

```json
{
  "short_drama_meta": {
    "stage": "...",
    "opening_hook": "...",
    "emotion_flow": "...",
    "structure": "...",
    "mid_hook": "...",
    "duration_seconds": 120,
    "source_path": "source/short_drama/episode_1.md",
    "source_revision": "<sha256>"
  }
}
```

只要正式脚本存在字典型 `short_drama_meta`，前端和工作流就将其识别为短剧工厂导入集。

## HTTP API

路由统一位于 `server/routers/short_drama.py`，生产前缀为 `/api/v1`。

### 读取源稿

```http
GET /projects/{project_name}/episodes/{episode}/short-drama-source
```

已有源稿：

```json
{
  "episode": 1,
  "path": "source/short_drama/episode_1.md",
  "markdown": "第【1】集：标题\n...",
  "revision": "<sha256>",
  "exists": true
}
```

历史项目没有源稿：

```json
{
  "episode": 1,
  "path": "source/short_drama/episode_1.md",
  "markdown": "",
  "revision": null,
  "exists": false
}
```

项目必须存在、`content_mode` 必须是 `drama`、集 ID 必须存在。

### 保存源稿

```http
PUT /projects/{project_name}/episodes/{episode}/short-drama-source
Content-Type: application/json

{
  "markdown": "...",
  "expected_revision": "<sha256-or-null>"
}
```

语义：

- `expected_revision=null` 只允许创建不存在的源稿。
- 已有源稿时必须提交当前 revision。
- revision 不匹配抛 `ShortDramaSourceConflict`，路由映射为 `ConflictError("short_drama_source_conflict")`。
- 保存不解析 Markdown，不修改正式脚本。
- 写入在 `ProjectManager.locked_source_mutation` 下执行，并通过 `atomic_write_bytes` 原子替换。

成功响应与 GET 已有源稿响应相同。

### 重新导入

```http
POST /projects/{project_name}/episodes/{episode}/short-drama-source/reimport
Content-Type: application/json

{
  "source_revision": "<sha256>",
  "confirm_artifacts": false
}
```

成功响应：

```json
{
  "episode": 1,
  "scenes": 6,
  "source_path": "source/short_drama/episode_1.md",
  "source_revision": "<sha256>",
  "registered_assets": {
    "character": ["角色名"],
    "scene": ["场景名"]
  },
  "asset_conflicts": [],
  "declared_seconds": 48,
  "actual_seconds": 40,
  "duration_shortfall": true
}
```

可选字段为空时不返回。声明时长与实际时长相等时，不返回三个时长诊断字段。

拒绝条件：

- 源稿不存在：404，`short_drama_source_missing`。
- revision 不一致：409，`short_drama_source_conflict`。
- 已有分镜图或视频且 `confirm_artifacts=false`：409，`short_drama_reimport_confirmation_required`。
- Markdown 解析失败：422，`short_drama_import_refused`，诊断包含解析原因。

### 原有首次导入接口

以下接口继续存在，并改为调用同一工作流：

```http
POST /projects/{project_name}/short-drama/import
POST /projects/{project_name}/short-drama/import-batch
```

首次导入使用 `replace_short_drama_source` 无条件替换该集源稿，然后提交正式脚本。批量导入并发处理各集，但每集源稿写入和项目元数据更新由项目锁串行保护。

## 后端实现

### `lib/script/short_drama_source.py`

该模块是源稿路径、revision 和文件写入的单一事实源，包含：

- `ShortDramaSourceConflict`
- `ShortDramaSource`
- `short_drama_source_relative_path`
- `load_short_drama_source`
- `save_short_drama_source`
- `replace_short_drama_source`

`save_short_drama_source` 的 `expected_revision` 有三种语义：

- 字符串：当前文件必须匹配该 revision。
- `None`：当前文件必须不存在。
- 私有 `_UNCONDITIONAL` 哨兵：兼容首次导入，允许替换。

### `server/services/short_drama_workflow.py`

该模块是 Web 和 Agent 共用的统一命令层，包含：

- `ShortDramaWorkflowError(code, detail)`
- `ShortDramaImportResult`
- `_DEFAULT_SCENE_DURATION_SECONDS = 8`
- `_import_scene_duration(project)`
- `_require_episode(project, episode)`
- `script_has_media_artifacts(script)`
- `import_short_drama_episode(...)`

`import_short_drama_episode` 的参数：

```python
async def import_short_drama_episode(
    *,
    project_name: str,
    episode: int,
    markdown: str,
    projects: ProjectManager,
    save_source: bool = True,
    expected_source_revision: str | None = None,
    confirm_artifacts: bool = True,
) -> ShortDramaImportResult:
```

执行顺序不可改变：

1. 在线程池加载项目。
2. 校验项目模式和集 ID。
3. 当 `confirm_artifacts=false` 时加载当前正式脚本，检测 `generated_assets.storyboard_image` 或 `generated_assets.video_clip`。
4. 解析 Markdown。
5. 用 `project.default_duration` 或 8 秒调用 `convert_to_drama_script(..., supported_durations=[duration])`。
6. 去掉 `short_drama_meta` 后用 `DramaEpisodeScript.model_validate` 做结构自检。
7. 首次导入通过 `run_sync_transaction` 保存源稿；重新导入则重新读取已保存源稿，核对 revision 和 Markdown 内容。
8. 把 `source_path/source_revision` 写入 `short_drama_meta`。
9. 用 `referenced_asset_names` 收集资产引用。
10. 通过 `run_sync_transaction` 调用 `ProjectManager.save_script`，传入 `project_update`。
11. `project_update` 在项目锁内重新读取源稿并核对 revision，然后调用 `register_missing_assets`。
12. 返回场景数、来源信息、资产登记结果和时长偏差。

此函数不接收 capabilities/config resolver，也不得调用 `video_capabilities_for_project`。

### `server/tool_runtime.py`

`import_short_drama_script` 和 `import_short_drama_series` 只做 Tool Envelope 映射：

- 调用 `import_short_drama_episode`。
- 把 `ShortDramaWorkflowError` 转为 `ToolProblem`。
- 批量导入对每集调用统一命令，并按 `continue_on_error` 汇总。

删除旧的 `_register_short_drama_assets`、`_duration_shortfall`、`ShortDramaTierUnavailable` 和 `_short_drama_split_durations`，避免 Web 与 Agent 出现两套行为。

`ImportShortDramaScriptRequest.markdown` 的工具说明必须明确：导入按项目默认分镜时长切幕，未设置时为 8 秒，导入阶段不读取视频供应商能力。

同样更新 `server/agent_toolset/script_authoring.py` 中 `IMPORT_SHORT_DRAMA_SCRIPT` 的说明。

### `lib/script/short_drama_import.py`

保留纯转换职责：

- Markdown 解析为 `ParsedEpisode`。
- 动作和神态写入 `scene_description`。
- 台词写入 `utterances`。
- 原始拍文本写入每幕 `source_text`。
- 根据调用方传入的规划时长切幕。
- `image_prompt` 和 `video_prompt` 置为 `None`。
- `pending_authoring` 置为 `True`。

`referenced_asset_names(script)` 返回：

```python
{
    "character": [...],
    "scene": [...],
    "prop": [...],
}
```

去重保序。实现使用 `Mapping`，循环变量不得命名为 `field`，避免遮蔽 `dataclasses.field`。

### `server/routers/projects.py`

`update_scene` 的 `allowed` 集合必须包含：

```python
"scene_description"
```

缺少该字段时，前端画面描述保存请求会返回成功但静默丢弃内容。

### `lib/episode/episode_deletion.py`

单集和批量删除的 `formal_paths` 都必须加入：

```python
project_dir / short_drama_source_relative_path(episode)
```

单集删除把该路径加入 `source_files`，提交钩子中删除。批量删除通过 `formal_paths` 去重后统一删除。这样源稿删除受 `formal_write_transaction` 回滚保护。

## 前端实现

### API 与动作层

`frontend/src/api.ts` 增加：

- `API.getShortDramaSource(projectName, episode)`
- `API.updateShortDramaSource(projectName, episode, markdown, expectedRevision)`
- `API.reimportShortDramaSource(projectName, episode, sourceRevision, confirmArtifacts)`

`frontend/src/actions/shortDrama.ts` 增加：

- `ShortDramaSource` 接口。
- `reimportShortDramaSource(...)` 动作。

动作成功后显示 `short_drama_source_reimported`，失败时使用 `errMsg` 显示服务端错误。

### `DramaScriptContentView`

`frontend/src/components/canvas/episode-page/DramaScriptContentView.tsx` 完全替换旧的逐场景正式脚本编辑器。

组件职责：

1. 按项目和集加载源稿。
2. 默认显示「Markdown 源稿」。
3. 提供「解析结果」只读页签。
4. 使用 `useEditUnit<SourceValue>` 管理脏态、保存状态和离开保护。
5. 保存时把 `saved.revision` 作为 `expected_revision`。
6. 用 `revisionRef` 保存 PUT 返回的新 revision，保证紧接着重新导入使用最新 revision。
7. 「重新导入」先执行 `unit.save()`；保存失败时不继续。
8. 已有媒体时打开 `AlertDialog`，确认后传 `confirm_artifacts=true`。
9. 重新导入成功后调用 `refreshAfterWrite(projectName, t)`。
10. 只读解析结果展示场景 ID、时长、地点、角色、`scene_description` 和台词。

关键状态：

```ts
interface SourceValue {
  markdown: string;
  revision: string | null;
}
```

加载状态携带 `${projectName}:${episode}` key，异步响应只更新对应请求。`ShortDramaSourceWorkspace` 的 React key 只能使用项目和集，不能包含 revision；把 revision 放进 key 会在保存成功后重挂载组件，导致「源稿已保存」反馈立即消失。

历史项目 `source.exists=false` 时显示说明，编辑器为空；「解析结果」仍展示现有正式脚本。

### `EpisodePage`

当：

```ts
script?.content_mode === "drama" && script.short_drama_meta != null
```

「剧本」页签渲染 `DramaScriptContentView`。传入：

- `script`
- `projectName`
- `episode`
- `readOnly`
- `hasArtifacts`

不再传 `scriptFile` 和集账本。短剧集的 source 页签显示名仍为「剧本」。

### 分镜页动作

`frontend/src/components/canvas/timeline/TimelineCanvas.tsx` 派生：

```ts
const importedShortDrama =
  editorContentMode === "drama" &&
  (episodeScript as DramaEpisodeScript | null)?.short_drama_meta != null;
```

只有 `!importedShortDrama` 时才显示：

- `ShortDramaGenerateButton`，文案「AI 原创剧本」。
- `ShortDramaImportButton`，文案「导入短剧工厂剧本」。
- `ShortDramaVisualButton`，文案「AI 生成剧本」。

已导入短剧保留通用 `PromptAuthoringButton`。页头的「编写提示词」作用于待编写范围；场景内同名按钮作用于当前场景，两者不是重复状态。

### 批量导入标题

`frontend/src/components/canvas/shared/ShortDramaImportDialog.tsx` 导出：

```ts
export function parseShortDramaBatch(...)
```

标题正则必须同时匹配短剧工厂原生格式和旧的普通格式：

```ts
const HEADER_RE = /^(?:#\s*)?第\s*[【[]?\s*(\d+)\s*[】\]]?\s*集(?:[：:\s]|$)/;
```

至少支持：

- `第【1】集：标题`
- `# 第 1 集 标题`

导入和批量建集后的项目刷新统一使用 `refreshAfterWrite`，不得直接丢弃 `refreshProject` 的结算值。

### 多语言

以下 key 必须同时存在于 `frontend/src/i18n/en/dashboard.ts`、`vi/dashboard.ts` 和 `zh/dashboard.ts`：

```text
short_drama_source_loading
short_drama_source_blank
short_drama_source_save_first
short_drama_source_leave_title
short_drama_source_save
short_drama_source_reimport
short_drama_source_tabs_aria
short_drama_source_tab
short_drama_parsed_tab
short_drama_source_legacy
short_drama_source_artifacts_hint
short_drama_source_placeholder
short_drama_source_editor_aria
short_drama_source_saved
short_drama_reimport_confirm_title
short_drama_reimport_confirm_description
short_drama_reimport_confirm
short_drama_source_reimported
```

批量导入 placeholder 必须示例 `第【N】集：标题`，不能只写无法被工厂原文匹配的英文 Episode 格式。

以下后端错误 key 必须同时存在于 `lib/i18n/en/errors.py`、`vi/errors.py` 和 `zh/errors.py`：

```text
short_drama_source_missing
short_drama_source_conflict
short_drama_reimport_confirmation_required
```

## 文件清单

### 新增

- `lib/script/short_drama_source.py`
- `server/services/short_drama_workflow.py`
- `tests/integration/server/routers/test_short_drama_router.py`
- `frontend/src/components/canvas/shared/ShortDramaImportDialog.test.ts`
- `docs/agents/short-drama-markdown-reimport.md`

### 修改

- `AGENTS.md`
- `lib/script/short_drama_import.py`
- `lib/episode/episode_deletion.py`
- `lib/i18n/{en,vi,zh}/errors.py`
- `server/routers/short_drama.py`
- `server/routers/projects.py`
- `server/tool_runtime.py`
- `server/agent_toolset/script_authoring.py`
- `tests/integration/server/test_tool_runtime_short_drama.py`
- `tests/integration/server/agent_toolset/test_host_consistency.py`
- `frontend/src/api.ts`
- `frontend/src/actions/shortDrama.ts`
- `frontend/src/components/canvas/episode-page/DramaScriptContentView.tsx`
- `frontend/src/components/canvas/episode-page/EpisodePage.tsx`
- `frontend/src/components/canvas/shared/ShortDramaImportDialog.tsx`
- `frontend/src/components/canvas/timeline/TimelineCanvas.tsx`
- `frontend/src/i18n/{en,vi,zh}/dashboard.ts`
- `tests/integration/lib/episode/test_episode_management.py`
- `tests/integration/server/routers/test_projects_script_edit_endpoints.py`
- `frontend/src/components/canvas/timeline/TimelineCanvas.test.tsx`

## 复建顺序

按以下顺序实施；每一步满足完成条件后再继续。

1. 实现 `short_drama_source.py`。
   完成条件：固定路径、SHA-256 revision、原子写入和 revision 冲突测试可独立成立。
2. 实现 `short_drama_workflow.py`。
   完成条件：单集导入能同时生成正式脚本、写来源元信息并登记资产；能力解析器即使配置为调用即抛错，导入仍成功。
3. 接入 Tool Runtime 和 Web Router。
   完成条件：首次导入、批量导入、GET、PUT、reimport 五条路径共用同一转换提交逻辑。
4. 补正式脚本编辑和分集删除边界。
   完成条件：`scene_description` 可保存；删除集后短剧源稿不存在。
5. 实现 API、动作层和源稿工作区。
   完成条件：源稿可保存、刷新后仍存在、revision 冲突可见、解析结果只读。
6. 收敛分镜页动作。
   完成条件：已导入短剧不存在三个内容创建按钮，存在「编写提示词」。
7. 修复批量标题解析和多语言。
   完成条件：原生 `第【N】集：标题` 和旧格式都能拆分。
8. 执行测试和浏览器验收。
   完成条件：下节全部通过。

## 测试与验收

后端定向测试：

```bash
uv run python -m pytest -q \
  tests/integration/server/routers/test_short_drama_router.py \
  tests/integration/server/routers/test_projects_script_edit_endpoints.py::TestProjectsRouter::test_scene_segment_and_overview_endpoints \
  tests/integration/lib/episode/test_episode_management.py::TestDelete::test_delete_removes_short_drama_markdown_source
```

必须覆盖：

- 历史项目 GET 返回 `exists=false`。
- 首次 PUT 成功，旧 revision PUT 返回 409。
- reimport 使用已保存源稿。
- 正式脚本包含 `source_path/source_revision`。
- 角色和场景完成资产登记。
- 初次导入持久化 Markdown。
- 已有媒体时未确认返回 409，确认后成功。
- 视频能力替身设置为调用即抛错，所有导入测试仍通过。
- `scene_description` PATCH 后真实落盘。
- 删除集时源稿删除。

前端定向测试：

```bash
cd frontend
pnpm exec vitest run \
  src/components/canvas/shared/ShortDramaImportDialog.test.ts \
  src/components/canvas/timeline/TimelineCanvas.test.tsx
pnpm typecheck
pnpm lint
```

浏览器验收：

1. 打开短剧导入集的 `?view=source`。
2. 默认页签为「Markdown 源稿」，编辑器显示项目源稿。
3. 初始「保存源稿」禁用；修改后启用。
4. 保存后显示「源稿已保存」，刷新后内容保持。
5. 「解析结果」显示正式脚本场景。
6. 无视频供应商时点击「重新导入」成功，不出现视频模型档位错误。
7. 有媒体时出现覆盖影响确认。
8. 进入 `?view=board`，确认「AI 原创剧本」「导入短剧工厂剧本」「AI 生成剧本」均不存在。
9. 页头保留「编写提示词」，随后可显式补齐分镜图和视频。

## 当前实现文件指纹

以下 SHA-256 对应基线 `adce16372f7a0c5a5fbc950d0083de2fc1ca5187` 上完成冲突整合后的整文件。共享文件可能包含同一工作区的故事规划改动，复建时以行为契约和测试为主，以指纹做当前快照的字节级验收。

```text
e0eb846f1fab8add9c66ad98ec06afeb9badbe3972d07b34d1ba74a4ea7b418e  lib/script/short_drama_source.py
0a006f0f348892b1943ce8c2b030903c86e56b523e4c70591b59ce445166e6e5  server/services/short_drama_workflow.py
c83625e4940917ea778025b0221771b0e95dd799b0a02f4460f1148363890999  server/routers/short_drama.py
2d32c13ea6611c989356f100343f221a69c55d0ed2cdc477c209eb7e7b7db307  server/tool_runtime.py
7997111b87dffbd809553410aa774b125b8f82757cdb1abadf521e819cfbb41d  lib/script/short_drama_import.py
2699feddfb658f7421c0f381baee128dc6aa2c1061f8670e48bf81d178544458  frontend/src/actions/shortDrama.ts
7f91f92bf91bdbda6f0601b567af5007d1157b86970fbddf2d58d01ac33fcc1e  frontend/src/api.ts
5a59885b0fd341a336a0b43e40584476fa1c496fbbdceab0ece19f9ec206274d  frontend/src/components/canvas/episode-page/DramaScriptContentView.tsx
e6aab9ce1b0a008af59760f37e9a08bdfa7f879bdc1818eb382003b232b1c6a3  frontend/src/components/canvas/shared/ShortDramaGenerateDialog.tsx
bcc8f0b05cbbb5eea82a99c208fe3a6bb2cd0c5ec59ead9da8a1fb2658d76814  frontend/src/components/canvas/shared/ShortDramaImportDialog.tsx
b602faf9430ba0a283c216dd9a500765e10fafb6aedfcd7de82b1974b5a09e66  frontend/src/components/canvas/timeline/TimelineCanvas.tsx
adc2d7bf827f713453d4ad5662bb0f11e21b660a766f9132bb05beebd18b285d  tests/integration/server/routers/test_short_drama_router.py
08fbff7db0c2c213698ffd9f5e4111e2237b9e5984105eb1a4a85bfc8fa11e66  tests/integration/server/test_tool_runtime_short_drama.py
85bcbd7c8bf7b9109eaf75bc2056b5a2f903236ce512b6b5378e2265a450acb2  tests/integration/server/agent_toolset/test_host_consistency.py
76a61a39d320b563a3e1f79e9366ebf95a7b02455bec631128cdd58cc28466d3  frontend/src/components/canvas/shared/ShortDramaImportDialog.test.ts
91ad8440547c581f8d18d92223cab917fc963854954f8bfec8bc20c2eadadc0d  frontend/src/components/canvas/timeline/TimelineCanvas.test.tsx
```

## 常见错误实现

- 在重新导入时调用 `video_capabilities_for_project`：会让纯文本转换错误依赖供应商配置。
- 保存 Markdown 时立即解析：会导致用户无法保留暂时不合法的草稿。
- 把 PUT 与 reimport 合并成一个动作：用户无法区分保存草稿和应用到正式脚本。
- 从正式脚本的 `source_text` 拼接历史 Markdown：会伪造已丢失的标题、元信息、空行和原始格式。
- 只更新脚本，不登记资产：后续资产图和生成入口会因引用未登记而失败。
- 先写脚本、再独立更新项目资产：第二步失败会留下半提交状态。
- 解析后不在项目锁内复核源稿 revision：并发保存可能让正式脚本对应旧源稿。
- 用 revision 作为 React key：保存成功后组件重挂载，保存反馈消失。
- 已导入短剧仍展示三个内容创建按钮：会把已经完成的内容导入误导为待执行步骤。
- 删除集时遗漏嵌套源稿：项目目录会积累孤儿文件。

## 工厂转写方案追加（道具 / 密度 / 外观 / AI 分镜入口）

对应 `方案设计/2. 工厂转写方案.md`，在上一节契约之上追加以下行为：

### 标记行格式

```text
[场景]：内景·仓库·夜｜水泥地，铁门单侧顶光     ← 「｜」后是场景外观
[人物]：林辰（隐忍龙王｜三十岁上下，寸头，旧夹克磨白）  ← 「｜」后是角色外观，且须与人物圣经【外形】逐字一致
[道具]：铁门｜锈迹斑斑，门轴下垂                ← 一个道具一行，「｜」后是外观
```

`｜` 同时接受半角 `|`。角色括号内没有 `｜` 时只算状态标签，外观为空；导入器据此不写 `description`。
`[道具]` 只登记本幕动作或台词里真正出现的道具，避免"建了资产但画面里没有"。

### 机检 E12–E14

判定在两处实现且必须同源：

- 工厂脚本 `agent_runtime_profile/.claude/skills/short-drama-factory/scripts/validate_episode.py`
  （独立 `python3` 运行，`--bible references/character-bible.md` 启用 E14 逐字比对）。
- 服务端 `lib/short_drama_quality.py`，由 `lib/short_drama_ingest.py` 在工件落盘前调用。

| 规则 | 判定 | 级别 |
| --- | --- | --- |
| E12 | `△` 动作行 + 台词行 ≥ `ceil(本集秒数 ÷ 2)` | fail |
| E13 | `[道具]` 行缺 `｜`/名称/外观、同集重复登记 | fail |
| E13 | 道具与角色或场景重名、已登记但未进戏 | warn |
| E14 | `[人物]` 行「｜」后外观 < 8 字、与人物圣经不一致、圣经里没有该角色 | fail |

一致性由 `tests/unit/lib/test_short_drama_quality_parity.py` 守：同一批 fixture 分别跑 CLI 与 `check_episode`，逐条比对 `E12/E13/E14` 输出。
工件包内任一集 FAIL 时整包拒绝（`ShortDramaArtifactError`），一个字节都不写入。

### 导入回执 `assets_without_description`

导入与重新导入在成功响应里追加：

```json
"assets_without_description": [
  {"type": "character", "name": "林辰", "reason": "source_missing"},
  {"type": "prop", "name": "铁门", "reason": "existing_blank"}
]
```

`source_missing` 指源稿没给外观，`existing_blank` 指资产已在项目里但描述为空。列表为空时不返回该字段。
前端单集/批量导入在拿到回执后追加一条 warning toast。

### AI 补全上下文（`ai-fill`）

`server/routers/_asset_router_factory.py` 的 `_collect_asset_context` 增加 `canon`：

- 角色：人物圣经里该角色的整段（`【角色】：名字` 到下一个角色前）。
- 场景：全剧大纲里提到该场景的一行 + 各集 `[场景]` 标记行。
- 道具：各集 `[道具]` 标记行（带文件名）。

提示词里 canon 以「【项目设定｜优先依据】」出现，优先级：项目设定 > 剧本摘录 > 题材补全。

### 分镜页入口

- `ShortDramaStoryboardButton`（`components/canvas/shared/`）：已导入短剧集在分镜页页头显示「AI 分镜」/「AI 重新分镜」，弹窗展示源稿行数/字数、已有分镜时要求勾选覆盖确认，提交即 `reimportShortDramaSource`，成功后弹窗内显示分镜数、缺描述黄条与「去补描述」。
- `FactoryStoryboardStart`：工厂集还没转写时，分镜页空态从「请在左侧选择剧集」换成生成入口 + 「先看剧本」。
- `EpisodePage` 把 `hasShortDramaSource` 注入 `EpisodeCanvasContext`，`StudioCanvasRouter` 再传给 `TimelineCanvas` / `GridImageToVideoCanvas`。
- 剧本页移除「重新导入」按钮，只留「保存源稿」，并在已有媒体时提示入口已移到分镜页。

### 追加多语言 key

```text
short_drama_ai_storyboard
short_drama_ai_restoryboard
short_drama_ai_storyboard_title
short_drama_ai_restoryboard_title
short_drama_ai_storyboard_desc
short_drama_ai_storyboard_source
short_drama_ai_storyboard_source_missing
short_drama_ai_storyboard_overwrite
short_drama_ai_storyboard_overwrite_confirm
short_drama_ai_storyboard_submit
short_drama_ai_restoryboard_submit
short_drama_ai_storyboard_result
short_drama_ai_storyboard_missing
short_drama_ai_storyboard_fix_missing
short_drama_ai_storyboard_close
short_drama_storyboard_empty
short_drama_storyboard_empty_hint
short_drama_storyboard_view_script
short_drama_asset_character
short_drama_asset_scene
short_drama_asset_prop
short_drama_reimport_moved_hint
short_drama_imported_one_missing_descriptions
short_drama_imported_batch_missing_descriptions
```

### 追加测试

```bash
uv run python -m pytest -q -p no:timeout \
  tests/unit/lib/test_short_drama_quality.py \
  tests/unit/lib/test_short_drama_quality_parity.py \
  tests/unit/lib/test_short_drama_ingest.py \
  tests/unit/lib/script/test_short_drama_import.py \
  tests/unit/server/routers/test_asset_router_factory.py \
  tests/integration/server/routers/test_short_drama_router.py
python agent_runtime_profile/.claude/skills/short-drama-factory/scripts/validate_episode.py --self-test
```
