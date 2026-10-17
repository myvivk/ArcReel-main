---
status: proposed
---

# 分镜提示词镜头序列化与宫格关键帧：scene 多镜描述、资产引用自动链接、宫格默认 2×2

## 术语先对齐

- **分镜**：脚本里的镜头条目（`E1S01`），一条口播 + 一组出场资产。「分镜提示词」挂在它下面。
- **分镜提示词**：即 `image_prompt.scene`，也就是「分镜提示词」输入框的正文。**它是本方案第一步的产物。**
  （`scene` 只是这个字段在数据模型里的名字，与「场景资产」无关。）
- **镜头N**：分镜提示词内描述的第 N 个关键帧，一格一镜；下游宫格第 N 格、确认后的 `K{N-1}`
  都对应它。
- 旧规范里 `【镜头1】` 是**整段单帧描述的包裹前缀**，本方案将其废止；新格式里 `【镜头N｜景别】`
  是镜头序列的序号标记，一格一镜。

## 背景

「分镜提示词」现行规范要求只描述单帧画面；资产指认依赖用户手动点「引入引用资产」插入
`@[名称]` token；生成参考素材宫格时系统硬编码一段 contact-sheet 指令，把每格定义为同一镜头的
时间推进并禁止新增事件；宫格默认 3×3。

新需求（推翻上述链路，不打补丁）：

1. 分镜提示词要能描述**整分镜的镜头序列**——每个镜头就是一个关键帧；
2. 去掉「引入引用资产」——资产指认全自动，用户只写名字；
3. 宫格每格 = 提示词里的一个镜头，按各镜头的描述与景别出图；
4. 宫格默认 2×2，生成前可手选规格；
5. 旧数据**全部废弃**，无兜底、无迁移。

## 两步链路（先划清边界）

```
第一步 · 编写分镜提示词（本方案对象，不生成任何图）
  来源：① Agent 编写（单镜「编写提示词」按钮 / 批量提示词编写，
        模板 drama / ad / narration_prompt_authoring，经 generate_episode_script）
       ② 人工在「分镜提示词」输入框直接编写
  输入：已定稿分镜内容（口播、出场资产、scene_description 视觉改编描述）
  产物：image_prompt.scene = 可选导语 + 镜头序列（【镜头N｜景别】…）
        ＋ composition（景别 / 光线 / 氛围结构化字段）

第二步 · 拿这份成果生成宫格图（下游消费方，本方案只定义契约）
  解析 scene → 镜头序列 → 每格一镜 → 宫格图（默认 2×2，手选 2×2/2×3/3×3）
  → 确认参考素材 → 切分绑定 K0..Kn → 视频提示词引 K 帧（此段不变）
  另一下游：单张分镜图 = 导语 + 镜头1（与 K0 同源）
```

## 决定

**D-A：分镜提示词采用「可选导语 + 镜头序列」格式，一格一镜。**

**D-B：资产引用自动链接。** 渲染时把该镜已登记资产（角色/场景/道具/商品，即参考图列表中的
`logical_id`）在正文里的首次出现处包上 `@[名称]`，再走既有「图N」编号。用户与 Agent 只写自然语言
名字，没有任何手动 token 入口。

**D-C：宫格系统指令重写为关键帧指令**——每格严格对应该镜头的描述与景别，不增、不减、不改序；
系统不再替用户补时间。

**D-D：宫格默认 2×2**，规格在生成前由用户在分镜详情选择。

**D-E：单一事实源。** 镜头序列的唯一真相源就是 scene 本身；第二步生成时**从当前 scene 现解析**，
宫格记录里的 `cell.prompt` 只作为"本次实际使用的镜头文本"留证。改画面只改 scene。

**D-F：旧数据全部废弃。** scene 中没有任何 `【镜头N` 标记时，两个生成入口（宫格、单张分镜图）
一律拒绝，提示"分镜提示词需按镜头序列重新编写"；不迁移、不自动改写，存量项目需要时由用户主动
让 Agent 重写。理由：旧单帧描述无法映射为一格一镜，兼容层会把两种语义永久焊死。

### 新分镜提示词格式（第一步的作者侧规范）

```
枯井井底，井壁是层层灰黑色旧砖，砖缝脱浆、青苔沿缝渗水，顶部井口漏下强光，
空气中悬浮水泥粉尘。（导语段：可选；跨镜头的环境 / 光线 / 风格基调，各镜头已自含时可省略）
【镜头1｜全景】沈昭左手将铁镐斜插进砖缝，镐头横卡在上方碎砖之间，右脚踏上镐柄横木，
  借支点撑起，身体紧绷。
【镜头2｜中景】她右腿发力上撑，膝弯微屈，身体整体上移，井壁泥水溅起。
【镜头3｜特写】面部特写：咬肌绷紧，咬牙切齿，瞳孔微缩，额前碎发滴落汗水。
【镜头4｜仰拍】井口灰白泥浆倾泻砸落，碎渣与粉尘在顶光光柱中翻飞，泥浆即将落在她肩头。
```

- 导语段：可选，写在镜头1之前；承载跨镜头的环境、光线、氛围基线；
- 镜头段：`【镜头N｜景别】正文`，N 从 1 递增；景别可省略（`【镜头N】正文`）；
- 每个镜头是一个**自含的关键帧画面描述**：主体姿态/表情/服装、环境、光线、氛围，叙述句；
- 镜头序号即宫格阅读序（左→右、上→下），镜头数应对上所选宫格数（不齐时按 D2）；
- 资产直接写名字（`沈昭`），不要求也不禁止手写 `@[]`——渲染时自动链接；
- 回忆/闪回改编为此刻可见载体，画面只有一个时空；
- 不得写 `Style`/`Scene`/`Composition`/`Avoid`/网格排版指令等最终模型包装字段。

### 新宫格提示词（第二步的系统指令草案，替换现有 contact-sheet 指令）

```
Create a cinematic keyframe sheet with exactly {rows} rows and {cols} columns, {count} equal
panels in left-to-right, top-to-bottom reading order. No text, labels, numbers or gutters
anywhere. Each panel is one keyframe of the SAME shot: render every panel strictly from its
frame description below, in the given order, at the given shot size. Keep character appearance,
wardrobe, props, setting, lighting and visual style consistent with the references and across
all panels. Panel 1 is the opening frame, panel {count} is the closing frame; do not add, merge,
skip or reorder frames.
```

其后拼：导语段（经 `storyboard/image` 模版包装的风格块 + 参考图声明 + Avoid），再逐格
`Panel i（{景别}）：{镜头正文}`；延续格（D2）用 `Panel i（continuation）：Continue the action
of frame N toward its natural conclusion; do not introduce new events.`

## 改动清单

### 提示词规范与模板（第一步）

| 文件 | 改动 |
| --- | --- |
| `lib/prompts/prompt_templates/templates/partials/shared/image_prompt_writing_guide.md` | 重写 scene 规范：可选导语 + 镜头序列、一格一镜、自然资产名；正例换成沈昭镜头序列版 |
| `lib/prompts/prompt_templates/templates/text/drama_prompt_authoring.md` | 同步（经 partial 继承，检查无冲突表述） |
| `lib/prompts/prompt_templates/templates/text/ad_storyboard_prompt_authoring.md` | 同上 |
| `lib/prompts/prompt_templates/templates/text/narration_prompt_authoring.md` | 同上 |
| `lib/prompts/prompt_templates/templates/text/ad_storyboard_script.md` | 同上（引用该 partial） |
| `lib/script/script_models.py` `ImagePrompt.scene` | 字段描述改为「整分镜画面描述：可选导语 + 镜头序列（【镜头N｜景别】）」 |

### 后端（第二步及渲染出口）

| 文件 | 改动 |
| --- | --- |
| `lib/script/grid/keyframe_parser.py`（新） | 解析导语与 `【镜头N｜景别】`；容错：N 不连续按出现序重排、景别可缺省、重复 N 留首 |
| `server/services/tasks/generation_tasks.py::execute_shot_grid_task` | 替换硬编码指令为 D-C 草案；从当前 scene 解析镜头序列并回写 `grid.cells`；每格行带景别；无镜头标记时拒绝（D-F） |
| `lib/prompts/prompt_builders.py::render_storyboard_image_prompt` | 自动链接：按 references 的 `logical_id` 在正文首次出现处包 `@[名称]`（长名优先、已有 mention 跳过），再走既有 `render_reference_mentions` |
| `lib/prompts/prompt_utils.py` | 新增「导语 + 镜头1」投影，供单张分镜图使用（D8） |
| `lib/script/grid/shot_models.py` | `from_dict` 默认 rows/cols 3 → 2 |
| `server/routers/shot_grids.py` | PUT 默认 2×2；confirmed 宫格禁止改规格（现 PUT 会静默重置已确认宫格，防误杀）；generate 守卫改为「scene 可解析出至少一个镜头」 |
| `lib/script/grid/prompt_builder.py`（多宫格分镜） | 单元格描述改用「导语 + 镜头1」投影（D5） |

### 前端

| 文件 | 改动 |
| --- | --- |
| `frontend/src/components/canvas/timeline/ShotDetail.tsx` | 删除 image 侧 `PromptReferenceMenu`（约 567-576 行）及 `storyboardAssetReferenceText` / `insertReference("image", …)` 链路；保留 video 侧 K 帧菜单（614 行）与「编写提示词」「查看提示词」 |
| `frontend/src/components/canvas/timeline/ReferenceMaterialsSection.tsx` | 新增宫格规格选择器（2×2 / 2×3 / 3×3，默认 2×2，复用 `shot_grid_layout` i18n key）；生成前先 `saveShotGrid(rows, cols)` 再 `generateShotGrid`（API 已存在、当前无调用方） |
| `frontend/src/i18n/zh/dashboard.ts` 等 | 补规格选择器与重新编写提示文案 |
| `frontend/e2e/regions/shot-detail.spec.ts` | 「引用资产插入分镜提示词」用例重写为镜头序列 / 自动链接用例；同步录制备 `prompt-templates.json` |

### 测试与闸门

- 新增：`tests/unit/lib/script/grid/test_keyframe_parser.py`、自动链接单测、
  `execute_shot_grid_task` 拼装单测（含镜头数不齐两种分支、无镜头标记拒绝分支）；
- 修改：`ShotDetail.prompt-form.test.tsx`、`ShotDetail.save-and-generate.test.tsx`、
  `tests/unit/lib/script/grid/test_grid_prompt_builder.py`（若动多宫格投影）；
- push 前按 `AGENTS.md` 跑 `uv run python scripts/gate.py backend frontend`。

### 文档

- `website/docs/guide/workflows.md` §4.1「参考素材」段重写（一格一镜、默认 2×2、手选规格）；
- 本 ADR review 通过后置 `status: accepted`；`CHANGELOG.md` 记一条。

## Considered Options

- **保留单帧规范，只在宫格端把一镜拆成时间点**（现状）：模型拿到的仍是单帧描述，时间推进靠
  "gradual continuity" 自由发挥，与"一格一镜"直接冲突——放弃。
- **逐格编辑器 UI 代替文本镜头标记**：与"分镜提示词输入框是第一步唯一产物"冲突，Agent 批量编写
  无处落盘，且多出一份需同步的状态——放弃，镜头序列写进 scene。
- **旧格式兼容层**：单帧 scene 自动拆格或整体当一格——两种语义焊死，违背"全部废弃"——放弃。
- **开放 4×4 / 5×5**：单格分辨率随格数下降，低分辨率档拖垮下游视频画质——本期不做。

## 待确认决策点（review 焦点）

- **D1 镜头标记语法**：`【镜头N｜景别】正文`（建议，已按此写方案）｜`第N镜（景别）：正文`｜其他。
- **D2 镜头数 ≠ 宫格数**：多则截断到前 N 个并警告、少则剩余格为「延续镜头N」自动格（建议）；
  备选：必须相等，UI 按镜头数默认选规格、不齐禁止生成。
- **D3 画格边框**：无边框无间隙（建议——切分按等格裁剪，边框会污染单格）；备选：黑细边框
  （需同步改 splitter）。
- **D5 多宫格分镜（多镜拼一张联合图）**：保留，单元格描述用「导语 + 镜头1」投影（建议）；
  备选：本期冻结该入口，后续单独设计。
- **D6 每格高级覆盖（`cells[].prompt` API）**：保留为可选进阶（建议，默认仍走 scene 解析）；
  备选：删除，只认 scene。
- **D7 规格集合**：仅 2×2 / 2×3 / 3×3（建议，与后端现允许集一致）；备选：增加 2×4 / 4×4。
- **D8 单张分镜图取哪段**：导语 + 镜头1，与 K0 同源（建议）；备选：仅镜头1。

## Consequences

- 第一步只产提示词（镜头序列），第二步才产图——边界干净：改画面只改 scene，重出宫格只是
  第二步重跑；
- 作者侧一次写好整分镜的镜头序列，宫格即"关键帧草图"；单张分镜图、宫格格0、视频首帧三者同源；
- 资产指认从"作者记得插 token"变为"渲染时按参考图列表机械链接」，「引入引用资产」及其 e2e、
  fixture 一并退役；Agent 编写模板改写真名即可，向后兼容（手写 `@[]` 仍被识别）；
- 存量旧格式 scene 全部废弃：不迁移，两个生成入口拒绝并提示重新编写（D-F）；
- 宫格记录从"提示词编辑态"退化为"本次镜头留证 + 切图状态"。
