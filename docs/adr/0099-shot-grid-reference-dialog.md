---
status: proposed
---

# 参考素材弹窗：宫格规格 + 镜头提示词（AI 生成 / 人工编辑）→ 确定出宫格图

> **与 0098 的关系（按 `docs/agents/domain.md` 显式声明冲突）**：本 ADR 接管并修订 0098 的
> **D-C**（宫格系统指令的内容与来源）与 **D-E**（"scene 是唯一真相源"）。0098 让宫格在生成时现解析
> `image_prompt.scene`；本方案在 scene 与出图之间插入一份**可编辑的宫格镜头提示词**
> （`ShotGrid.prompt`），它是弹窗输入框的内容、也是最终出图依据。scene 退为"意图源"，仍是
> 「改画面该改哪里」的答案。0098 的 D-A（一格一镜 / `【镜头N｜景别】`）、D-B（资产自动链接）、
> D-D（默认 2×2）、D-F（旧格式废弃）继续有效。

## 术语

| 术语 | 指什么 | 落点 |
| --- | --- | --- |
| 分镜提示词 | 第一步产物，整分镜的画面意图（导语 + 镜头序列） | `image_prompt.scene` |
| 宫格镜头提示词 | **第二步产物**，本次出图的逐格镜头文本，可编辑 | `ShotGrid.prompt`（新增） |
| 宫格图 | 一张 N×M 的关键帧联合图 | `ShotGrid.composite_path` |
| 系统指令 | 网格排版 / 风格 / 参考图声明 / 纯净性约束，不由用户与 AI 写 | 渲染时拼装 |

## 背景

「生成参考素材」目前是分镜详情里的一个**内联小按钮**（`ReferenceMaterialsSection.tsx:128`），点了
直接 `enqueueShotGrid`，规格硬编码 3×3、提示词由后端现场拼一段 contact-sheet 指令
（`generation_tasks.py:1617-1626`）。`saveShotGrid`（`api.ts:3861`）与 i18n key `shot_grid_layout`
已存在但**零调用方**。

新需求分两处：

1. **制作进度新增「参考素材」一行**，排在「分镜图 / 宫格」与「视频」之间。这一步有两条路：
   **整集并行制作**（自动生成，不弹窗）与**单个分镜制作**（走弹窗，手动控制规格与提示词）。
2. **弹窗** = 宫格规格（2×2 / 2×3 / 3×3）+ 输入框（承接镜头提示词）+ 两个按钮
   （AI 生成提示词 / 确定）。弹窗里不展示参考资产。

同时给了一份「宫格生成规范」：【基础设置】+【风格与角色锚点】+【逐格镜头描述】+【纯净性约束】。

---

## 一、宫格提示词规范可行性分析

### 判定表

| 规范条目 | 判定 | 说明 |
| --- | --- | --- |
| 提示词必须是四段式结构 | ✅ **采纳** | 与仓库已有的 `storyboard/grid.md`（布局要求 / 帧链节奏 / 各格内容 / avoid）几乎同构，不是新发明 |
| 必须有显式网格排版指令（行列、阅读顺序） | ✅ **采纳** | 现状已有（`contact sheet with exactly R rows and C columns`），但**内容要重写**，见下 |
| 每格 = 一个时间节拍 / 一个动作 | ✅ **采纳** | 与 0098 D-A「一格一镜」一致；现状"每格是同一镜头的时间推进"正是被推翻的部分 |
| 明确的景别变化（全景→中景→特写） | ✅ **采纳** | 现状 `cells[].prompt` 没有景别槽位，需在解析与拼装里带上 |
| 风格与角色锚点 | ⚠️ **采纳但改写** | 见 **P4**：不能复述资产外观，否则与参考图冲突 |
| 纯净性约束（无文字 / 序号 / 气泡 / 箭头） | ✅ **采纳** | 复用 `storyboard/image/avoid` 与 `storyboard/grid/avoid` |
| "画面比例 16:9" | ❌ **否决** | 见 **P1**：项目短剧是竖屏，比例必须由系统按项目与规格算出 |
| "各格之间用黑色细边框分隔" | ❌ **否决** | 见 **P2**：下游按等分裁剪，边框会污染每一格与整张分镜图 |
| 网格排版指令写进提示词正文 | ❌ **否决（改为系统拼装）** | 见 **P3**：与弹窗上的规格选择器必然打架 |

### P1（致命）"画面比例 16:9" 是写死的，且整图比例没随规格变

- 短剧走 `resolve_video_aspect_ratio(project, "storyboards")`，`content_mode` 为 `narration`/`ad` 时
  默认 **9:16**（`lib/artifacts/video_visual_provenance.py:28`）。规范里的 16:9 直接错位。
- 更硬的问题：`execute_shot_grid_task` 把 `canvas_ratio` **直接当作整张宫格图的比例下发**，不区分
  rows/cols（`generation_tasks.py:1640`）。方形档（2×2、3×3）恰好让单格仍等于视频比例，但
  **2 行 3 列会让单格变成 3:8**——切出来的 K 帧、`storyboard_image`、视频首尾帧全部变形。
  这是现存缺陷，弹窗一开放 2×3 就必然被踩到。
- **结论**：比例是系统字段，不是提示词文本。见 **D-6**。

### P2（致命）黑细边框会污染下游

`POST /shot-grids/{grid_id}/confirm`（`shot_grids.py:178-197`）按 `width/cols`、`height/rows` **等分
裁剪**，并把**整张宫格图**绑成 `storyboard_image`、各格绑成视频首尾帧与参考图。有边框 → 每格都带
黑边、正式分镜图带网格线、视频首帧带黑边。与 0098 D3「无边框无间隙」一致：**必须无边框**。
规范里这一条要删掉。

### P3（结构性）网格指令写进正文会与规格选择器打架

弹窗上有 2×2 / 2×3 / 3×3 选择器，若输入框里的文本自带「2 列 x 2 行共 4 格」，用户改了选择器却没改
文本 → 模型收到自相矛盾的指令，且用户根本不会意识到要改。这是弹窗方案独有的风险，必须结构上消除。
解法见 **D-2**。

### P4 角色锚点不要复述资产外观

角色 / 场景 / 道具资产**图**已随请求发出，并有 `Reference_Images` 声明行与 `@[名称] → 图N` 编号
（`lib/prompts/reference_image_numbering.py`）。再让 AI 抄一遍"素色窄袖短褐、深色长裤"，一旦与资产图
不一致，模型会在"图"与"文"之间摇摆，反而破坏一致性。**锚点段只承载跨格恒定约束**（同一套服装、
同一光线方向、同一时段、不换装），资产外观交给参考图；作者侧的跨镜头环境基线写在 scene 导语段，
由系统拼进锚点位置。

---

## 二、决定

**D-1 单分镜走弹窗，整集走批量；两条路共用同一套系统指令拼装，但提示词来源不同。**

| 路径 | 入口 | 规格 | 逐格镜头文本从哪来 |
| --- | --- | --- | --- |
| 单分镜 | 分镜详情「生成参考素材」按钮 → 弹窗「确定」 | 弹窗里手选 | `ShotGrid.prompt`（AI 生成后用户可改） |
| 整集 | 制作进度「参考素材」行 → 批量 | 统一 2×2 | **从 `image_prompt.scene` 现解析**，不读 `ShotGrid.prompt` |

`ReferenceMaterialsSection` 保留结果展示、轮询与「确认参考素材」。

**D-2 四段式落地为「系统拼装 + 用户可编辑的逐格段」。** 输入框里装的是规范的第 3 段
（逐格镜头，含景别），第 1/2/4 段由系统渲染，不进输入框：

| 规范段落 | 来源 | 谁写 |
| --- | --- | --- |
| 【基础设置】 | `rows/cols` + 项目画布比例 → 整图比例 / 单格比例 / 行列数 / 阅读序 | 系统 |
| 【风格与角色锚点】 | 项目风格块（`shared/media_style`）+ 参考图声明（图N）+ scene 导语段 | 系统 + 作者（导语写在 scene） |
| 【逐格镜头描述】 | 单分镜取 `ShotGrid.prompt`；整集取 scene 现解析 | **AI 生成 + 用户编辑 ← 弹窗输入框** |
| 【纯净性约束】 | `storyboard/grid/avoid` 类模板 | 系统 |

这样既满足「AI 生成后在输入框显示、可微调」，又杜绝 P3 的规格/文本打架。

**D-3 新增 `ShotGrid.prompt`。** 宫格镜头提示词正文（逐格段），外加 `scene_revision`（生成时的
scene 指纹，用于"分镜提示词已变更"提示）。`cells[].prompt` 降级为**本次出图留证**：generate 时由
系统从 `prompt` 解析回填（正文进 `prompt`、景别进 `continuity_notes`），confirm 链路不变。

**D-4 修正 `PUT /shot-grids` 的"打开即重置"。** 现状 PUT 无条件把 `status` 打回 `draft`、清
`composite_path`（`shot_grids.py:108-112`）。弹窗方案下"打开看一眼再取消"就会把已生成的图清空——
这个缺陷会被放大成必现事故。改为：**仅当 `rows/cols/prompt` 任一与库中不同才重置**；完全相同
（含打开即取消）不动任何字段；`status == "confirmed"` 一律拒绝改规格（0098 已要求）。

**D-5 「AI 生成提示词」走新的同步端点，不落盘。** 请求带 `script_file / episode / shot_id /
rows / cols`，返回 `{ prompt }`，前端回填输入框。落盘只发生在点「确定」时——避免"AI 生成了但用户
点取消"污染记录。输入 = 当前 `image_prompt.scene` + 该分镜登记资产（只给名字与类型，外观由参考图
承担）+ 项目风格 + 格数；输出 = 纯逐格镜头文本，**不含**网格指令 / 锚点 / 约束。

**D-6 整图比例由 `(rows, cols, canvas_ratio)` 算出。** 复用 `lib/script/grid/layout.py` 的
`grid_aspect_ratio_for` 思路，方形档取视频比例本身；非方形档的比例取值见 **R4**（待拍板）。
单格比例一并写进指令（多宫格模板已有这两个槽位，单分镜宫格此前缺失）。

**D-7 `POST /generate` 守卫改为「`ShotGrid.prompt` 能解析出至少一格」**，替换现在的
「至少一格 `cells[].prompt` 非空」（`shot_grids.py:126`）。

**D-8 制作进度新增「参考素材」行，step id `shot_grid`，插在 `storyboard` 与 `video` 之间。**

位置由 `lib/workflow/workflow_rules.py:29` 的 `_STEP_IDS` 决定，不在前端拼：

```python
_STEP_IDS = (
    ...,
    "asset_sheets",
    "script_structure",
    "storyboard",
    "shot_grid",  # 新增
    "video",
    "edit",
)
```

- 适用条件与 `storyboard` 相同（`generation_mode == "storyboard"` 才适用，见 `_build_rule`）；
- 行标题复用已有文案 `dashboard:shot_reference_materials`（「参考素材」，即分镜详情那个区标题），
  不新造词；
- 截图里没有「旁白配音」行，说明短剧这集不是 `narration + storyboard`，插入位置无歧义。

**D-9 整集并行走已有的批量形态，不新造接口。** 复用 `StoryboardBatchDialog` 与
`server/routers/storyboard_batches.py`：给 `StoryboardBatchKind` 增加 `"shot_grids"`，后端补一份
`shot_grids` 的 preview / submit 规划（照 `load_storyboard_image_batch_plan` 的写法），端点仍是
`POST /projects/{p}/episodes/{e}/shot-grids/batch[/preview]` 这一族。目标是**本集缺失参考素材的每一个
分镜**，规格统一 2×2，**不弹提示词弹窗**。并行不新增机制：批量建 N 个 `shot_grid` 任务，实际并行度
由现有生成队列决定。

**D-10「参考素材」行的可用口径独立于分镜图。** confirm 会把宫格整图同时登记为 `storyboard_image`
（`shot_grids.py:225`），若复用该产物集合，两行会同时亮、重复计同一份东西。本行按
**`ShotGridManager` 记录 + `image_basis` 时效比对**判定，见 **R10**。

**D-11 批量路径不读 `ShotGrid.prompt`。** 用户已定：整集时每个分镜的逐格镜头文本**直接从
`image_prompt.scene` 现解析**。后果要写明：若某分镜曾在弹窗里手改过 `ShotGrid.prompt`，整集重做会
按 scene 重新解析、覆盖那次手改——弹窗里因此必须有这句提示，不能让人以为手改会一直生效。

---

## 三、弹窗交互

```
┌─ 生成参考素材 ────────────────────────────────────────┐
│ 宫格规格   ( •2×2   2×3   3×3 )        默认 2×2        │
│                                                        │
│ 镜头提示词                                             │
│ ┌──────────────────────────────────────────────────┐  │
│ │ 第1格（全景）：沈昭左手将铁镐斜插进砖缝…          │  │
│ │ 第2格（近景）：面部特写，咬肌绷紧…                │  │
│ │ 第3格（中景）：右腿发力上撑，泥水溅起…            │  │
│ │ 第4格（仰拍）：井口灰白泥浆倾泻砸落…              │  │
│ └──────────────────────────────────────────────────┘  │
│ ⓘ 分镜提示词已变更，建议重新生成            （可选）   │
│                                                        │
│        [ AI 生成提示词 ]        [ 取消 ]  [ 确定 ]     │
└────────────────────────────────────────────────────────┘
```

- **打开**：`getShotGrid` → 有记录则预填 `rows/cols` 与 `prompt`；无记录则 2×2 + 空。
- **AI 生成提示词**：调 `POST /shot-grids/author-prompt`，按钮转 loading、输入框禁用；返回后回填
  （若输入框非空，先提示"将覆盖当前内容"）。失败 toast，输入框保持原样。
- **切换规格**：若 `prompt` 非空，给出"格数已变，请重新生成镜头提示词"的提示，不自动改写文本。
- **确定**：`saveShotGrid({rows, cols, prompt})` → `generateShotGrid` → **关闭弹窗**，
  由 `ReferenceMaterialsSection` 继续轮询并在分镜详情里呈现宫格图。
- **取消**：不落盘、不入队。
- **弹窗不含参考资产区**：资产已在分镜详情的「引用」区陈述，这里再列一遍会让人以为可以增改。
- 手改只在**这一次出图**生效：整集重做按 `image_prompt.scene` 重解析，会覆盖它（**D-11**，提示文案见
  **R11**）。

---

## 四、制作进度里的「参考素材」行

渲染模板与截图里的「分镜图 / 宫格」行完全一致（`StepListRow` + `StaleArtifacts`），
只是换成 `shot_grid` 这个 step 与它的产物集合：

```
参考素材    共 7 个，已生成 3 个
            ⓘ 引用的资产没有资产图的分镜已跳过
            ┌ 4 件产物比当前内容旧 …（blocked 时给「重新生成全部 4 件」）
```

| 项 | 落在哪 |
| --- | --- |
| step id / 行序 | `workflow_rules.py:29` `_STEP_IDS` 插 `"shot_grid"`；`_build_rule` 里随 storyboard 模式置 applicable |
| 产物集合 | `workflow_state.py` 新增 `artifacts["shot_grids"]`；`workflow_plan.py:166` `_ARTIFACT_BY_STEP["shot_grid"] = "shot_grids"` |
| 状态判定 | `workflow_plan.py:291` `_step_done` 加分支：`has_items and not artifacts["shot_grids"]["missing_ids"]` |
| 任务归行 | `workflow_plan.py:191` `_TASK_STEP["shot_grid"] = "shot_grid"`（`task_type` 已是 `shot_grid`） |
| 下一步归属 | `workflow_plan.py:214` `_ACTION_STEP[GENERATE_SHOT_GRIDS] = "shot_grid"`；`step-list.ts::ownerRow` 同映射 |
| 过期产物可重生 | `StepListRow.tsx:120` `REGENERABLE_STEPS` 加 `"shot_grid"`；`StudioCanvasRouter.tsx:333 handleWorkflowRegenerate` 加 `shot_grid` 分支 |
| 行标题 | 复用 `dashboard:shot_reference_materials`（「参考素材」） |
| `status` / `tone` | 复用 `step-list.ts::mediaStatus()`，口径见 **D-10**（不看 `storyboard_image`） |
| 常驻入口 | 「批量生成」`kind: "ai"` → 复用 `storyboard_batch` 那套 intent，`kind: "shot_grids"`；「去分镜」`view_unit` 到第一个缺失单元 |

**下一步**：`generate_shot_grids` 挂在 `shot_grid` 步、渲染在 `shot_grid` 行，`primary` = 交给 Agent +
批量生成，`detail` = 「{{count}} 个分镜还没有参考素材」，`hint` 沿用 `unsheetedAssetsHint`。

---

## 五、整集并行生成（不弹窗）

**目标集合**由服务端定，与分镜图批量同一形态：本集**没有可用参考素材**的分镜。
跳过项（照 `BatchSkip` 枚举扩展）：

| 跳过原因 | 说明 |
| --- | --- |
| `missing_prompt` | `image_prompt.scene` 为空或 `pending_authoring`（第一步没做完） |
| `reference_unavailable` | 引用的角色 / 场景 / 道具没有可用资产图 |
| `generating` | 该分镜已有 `shot_grid` 任务在跑 |
| `confirmed` | 已确认的宫格不进批量；重做只能单个走分镜详情（**R7**） |

**规格与提示词**在批量里不由人定：规格统一 **2×2**；逐格镜头文本**从 `image_prompt.scene` 现解析**
（**D-11**），不走 AI、不读 `ShotGrid.prompt`。弹出的仍是 `StoryboardBatchDialog`（名单 / 跳过项 /
预估费用），不是提示词编辑弹窗。

任务形态：每个目标分镜一个 `shot_grid` 任务，全部挂在同一个 `batch_id` 下；
不新增并行机制，实际并行度由现有生成队列决定。

---

## 六、数据模型（`lib/script/grid/shot_models.py`）

```python
@dataclass
class ShotGrid:
    ...
    prompt: str = ""  # 新增：宫格镜头提示词（逐格段），可编辑
    scene_revision: str | None = None  # 新增：生成 prompt 时的 scene 指纹（sha256 前 12 位）
```

`from_dict` 用 `data.get("prompt", "")` 兜底，不改 schema 版本、不需要 project 迁移
（文件级 JSON，字段缺省即空）。`to_dict` 同步输出，前端类型 `ShotGridPlan` 补同名字段。

---

## 七、API

**`server/routers/shot_grids.py`**（prefix `/projects/{project_name}/shot-grids`）

| 端点 | 改动 |
| --- | --- |
| `PUT ""` | `SaveShotGridRequest` 增 `prompt: str \| None = None`（`None` = 不变，清空传 `""`）；默认 `rows/cols` 3→2；按 **D-4** 收敛重置语义；`confirmed` 拒绝改规格 |
| `POST /author-prompt`（新） | 同步生成逐格镜头文本，不落盘，返回 `{ prompt }` |
| `POST /generate` | 守卫改为「`grid.prompt` 可解析出至少一格」（**D-7**） |
| `GET ""` / `confirm` / `tasks` | 不变（`to_dict` 多带两个字段） |

**批量（扩展 `server/routers/storyboard_batches.py`，不新建文件）**

| 端点 | 说明 |
| --- | --- |
| `POST /shot-grids/batch/preview` | 只规划不建任务：目标名单、跳过项与原因、能算出时的预估费用 |
| `POST /shot-grids/batch` | 重新规划后建一个持久批次，立即返回 `batch_id` + `task_ids_by_unit` |

配套：`server/services/admission/storyboard_batch.py` 增一份 `shot_grids` 规划（照
`load_storyboard_image_batch_plan` / `submit_storyboard_image_batch`）；`StoryboardBatchKind` 增
`"shot_grids"`；`StoryboardBatchDialog` 按 kind 换文案，其余结构不变。

**工作流状态**（`lib/workflow/workflow_state.py`）

| 改动 | 说明 |
| --- | --- |
| `WorkflowActionType` | 新增 `GENERATE_SHOT_GRIDS = "generate_shot_grids"`（新增成员自动进入 `tests/unit/lib/workflow/test_workflow_action_types.py` 的覆盖检查） |
| `artifacts["shot_grids"]` | 新增产物集合，口径见 **D-10** |
| `operations[GENERATE_SHOT_GRIDS]` | 新增准入（照 `admit_*` 系列） |
| 下一步决策 | 在 `GENERATE_STORYBOARDS` / `GENERATE_GRID` 之后、`GENERATE_VIDEOS` 之前插入（约 `workflow_state.py:1859-1875`） |
| `lib/generation/generation_result.py:1079` 动作译文表 | 加一条「参考素材生成」 |

---

## 八、后端拼装（替换 `generation_tasks.py:1617-1626` 的硬编码）

```
【布局要求】                                   ← 系统，由 rows/cols/比例算出
  恰好 {rows} 行 {cols} 列，共 {count} 格，阅读顺序：从左到右、从上到下
  整图比例 {grid_aspect_ratio}；每格比例 {panel_aspect_ratio}，所有画格等大
  画格之间无边框、无间隙、无留白
【风格与锚点】                                  ← 项目风格块 + Reference_Images 声明 + scene 导语段
【各格内容】
  第1格（全景）：…                             ← 单分镜：ShotGrid.prompt
  第2格（近景）：…                             ← 整集  ：从 image_prompt.scene 现解析
【纯净性约束】                                  ← avoid 模板：无文字 / 序号 / 气泡 / 箭头 / 水印
```

- 逐格文本有两个来源（**D-1** / **D-11**），拼装出口是同一个函数；`grid.cells[].prompt` 只按本次
  实际使用的文本回填留证。
- `@[] → 图N` 与 0098 D-B 的**自动链接**都作用在逐格文本上：AI 写资产裸名，渲染时按
  references 的 `logical_id` 在首次出现处包 `@[名称]` 再编号（新增 `link_asset_mentions(text, references)`）。
- `aspect_ratio` 下发 `grid_aspect_ratio_for(rows, cols, canvas_ratio)`，不再是裸 `canvas_ratio`。
- 分辨率：3×3（9 格）建议要求 ≥2K 档；沿用多宫格的"单格分辨率随格数缩水"判断
  （`layout.py:large_grid_allowed`），本期不额外加档，但 3×3 在低于 2K 时给前端一个软提示。

---

## 九、前端

| 文件 | 改动 |
| --- | --- |
| `components/canvas/timeline/ShotGridDialog.tsx`（新） | 弹窗本体：`Dialog / DialogHeader / DialogBody / DialogFooter` + `RadioGroup` 选规格 + `Textarea` + 两个按钮；参照 `PromptAuthoringDialog.tsx` 的写法 |
| `stores/shot-grid-dialog-store.ts`（新） | 参照 `short-drama-generate-store.ts` 的 `request + open/close` 模式 |
| `components/canvas/timeline/ReferenceMaterialsSection.tsx` | 「生成参考素材」按钮改为 `open(...)`；结果区不变；顺手修 `grid-cols-${plan.cols}` 动态 class（Tailwind 扫不到，改为 2/3 的固定映射） |
| `components/canvas/timeline/ShotDetail.tsx` | 挂载弹窗宿主；把 `scriptFile / episode / shotId / saveFirst / beforeGenerate` 传进去 |
| `api.ts` | `SaveShotGridPayload` 增 `prompt?`；新增 `authorShotGridPrompt()` |
| `types/shot-grid.ts` | `ShotGridPlan` 增 `prompt` / `scene_revision` |
| `components/canvas/timeline/StoryboardBatchDialog.tsx` | 接受 `kind: "shot_grids"`，文案与跳过原因按 kind 取 |
| `components/workflow/step-list.ts` | `StepRowKey` 增 `"shot_grid"`；`buildRows` 在 boards 之后插该行；`ownerRow` 增 `generate_shot_grids`；`buildNext` 增该 case |
| `components/workflow/StepListRow.tsx` | `REGENERABLE_STEPS` 增 `"shot_grid"` |
| `components/canvas/StudioCanvasRouter.tsx` | `handleWorkflowRegenerate` 增 `shot_grid` 分支（逐单元 `enqueueShotGrid`） |
| `types/workflow.ts` | `WORKFLOW_ACTION_TYPES` 增 `"generate_shot_grids"` |
| `types/storyboard-batch.ts` | `StoryboardBatchKind` 增 `"shot_grids"` |
| `i18n/{zh,en,vi}/dashboard.ts` | 新增：弹窗标题与说明、`shot_grid_author_prompt`、`shot_grid_authoring`、`shot_grid_stale_hint`、`shot_grid_cover_hint`；复用已有的 `shot_grid_layout` 与 `shot_reference_materials` |
| `i18n/{zh,en,vi}/workflow.ts` | 新增 `next_title_generate_shot_grids`、`next_detail_generate_shot_grids`、`agent_prefill_generate_shot_grids`、`action_generate_shot_grids`；行标题复用 `dashboard:shot_reference_materials`，不新增 `row_*` |

---

## 十、测试

- 新增 `tests/unit/lib/script/grid/test_shot_grid_prompt_builder.py`：四段式拼装、比例计算、
  `@[]` 编号与自动链接、无镜头文本时的拒绝分支。
- 新增 `tests/unit/server/routers/test_shot_grids_router.py`：PUT 幂等不误杀、改规格才重置、
  `confirmed` 拒绝改规格、generate 守卫。（**现状 shot_grids 路由没有任何测试**。）
- 新增 `tests/unit/server/services/tasks/test_execute_shot_grid_task.py`：下发比例随规格变化、
  prompt 拼装快照。
- 新增 `ShotGridDialog.test.tsx`：默认 2×2、AI 回填、覆盖提示、规格变更提示、确定→保存+入队+关闭。
- 修改 `ShotDetail.save-and-generate.test.tsx`、`frontend/e2e/regions/shot-detail.spec.ts`
  （按钮从"直接入队"变为"开弹窗"）。
- push 前 `uv run python scripts/gate.py backend frontend`。

---

## Considered Options

- **输入框装完整四段式文本，切换规格时系统重写【基础设置】段**：最贴合原规范文本，但依赖正则在
  用户可自由编辑的文本里定位段落，用户删掉整段后即失效；且把只属于系统的字段暴露给编辑——放弃，
  但列为备选（见 R2）。
- **AI 生成这一步复用现有 `prompt-authoring` 任务队列**：与"生成后填回弹窗输入框"的同步交互不匹配
  （要轮询、要锁弹窗），且该链路带 409 覆盖确认与 `overwrite_revision` 令牌，是给"改脚本"设计的——
  放弃，走轻量同步端点。
- **把镜头提示词写回 `image_prompt.scene`**：会让 scene 同时承担"意图"与"某次出图参数"两种语义，
  与 0098 D-A 的镜头序列格式冲突——放弃。
- **AI 生成即落盘**：用户点取消会留下半截状态，与 D-4 的"打开即取消不留痕"矛盾——放弃。

---

## 待确认决策点（review 焦点）

- **R1 弹窗里能否手工编辑逐格文本？** 建议可以（保留微调能力）；备选：只读，改画面一律回 scene 重生成。
- **R2 输入框承载范围**：只承载逐格段（本方案，推荐）｜完整四段式 + 规格切换时重写基础设置段（备选）。
- **R3 「AI 生成提示词」的输入**：是否额外给一个自由指令输入框（如"更强调雨夜氛围"）？本期建议不加。
- **R4 2×3 的整图比例**：(a) 沿用 legacy 登记的 3:4（各 backend 尺寸表认得，单格略偏离 9:16，切分后
  center-crop 微调）｜(b) 只开放方形档 2×2 / 3×3，把 2×3 移出白名单｜(c) 用几何自洽的 27:32 并扩
  backend 尺寸表。建议 (a)，但需实测一次 2×3 竖屏出图再定。
- **R5 镜头数 ≠ 格数**：本方案里 AI 按所选格数产出等量镜头；用户手动改格数后只提示不自动改写
  （建议）。备选：改格数即自动截断 / 补延续格（0098 D2 的备选）。
- **R6 3×3 的分辨率下限**：是否硬性要求 ≥2K 档，还是只给软提示（建议软提示，避免低配环境卡死入口）。
- **R7 已 `confirmed` 的宫格**：允许改 prompt 重新生成（但要重新确认）｜一律禁止（建议禁止改规格、
  允许改 prompt 后重新走一遍生成 + 确认）。
- **R10「参考素材」行的可用口径**：按 `ShotGridManager` 记录 + `image_basis` 时效（建议，与分镜图行
  互不重复计）｜复用 `storyboard_image` 产物集合（两行会同时亮）。
- **R11 整集重做覆盖手改的提示文案**：批量按 scene 重解析会盖掉弹窗里的手改（**D-11**）。弹窗里那句
  提示写多重？建议只在"当前 prompt 与 scene 解析结果不一致"时出现，不常驻。

## 已定（本次确认）

- **整集入口形态**：批量生成本集**缺失**参考素材的每一个分镜，走 `StoryboardBatchDialog` 的
  名单 / 跳过 / 费用确认；不弹提示词弹窗。
- **整集提示词来源**：直接从 `image_prompt.scene` 解析，**不读** `ShotGrid.prompt`（**D-11**）。

## Consequences

- 第二步变成"看得见、改得动"的一步：宫格不再由后端黑箱拼装，作者能看到并微调真正发给模型的镜头文本。
- 两条路径的提示词来源不同（弹窗读 `ShotGrid.prompt`、整集解析 scene），批量重做会覆盖手改；
  靠 `scene_revision` 与 R11 的提示兜底，不强行同步。
- 修掉两个现存缺陷：PUT 打开即清空宫格图、2×3 单格比例错误；顺带让 shot_grids 路由首次有测试覆盖。
- 多了一份可编辑状态（`ShotGrid.prompt`），与 0098 D-E 的"scene 唯一真相源"冲突，需在 0098 定稿时
  同步改写 D-E，或由本 ADR 直接取代其第二步部分。
