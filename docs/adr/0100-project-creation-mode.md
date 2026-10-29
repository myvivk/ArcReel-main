---
status: accepted
---

# 项目级创作模式 `creation_mode`：剧本来源的不可变标轴

`creation_mode` 是项目级不可变字段，二值：`ai_original`（默认，剧本由 AI 原创链路产出）与 `source_import`（剧本由用户粘贴的原文经服务端改写生成）。它只回答一个问题——**剧本从哪里来**——不参与 `content_mode`（内容类型）与 `generation_mode`（视频来源）的任何判定，也不改变资产、分镜、剪辑链路。新项目由 `ProjectManager.create_project_metadata` 显式写入；存量项目由 v18→v19 迁移补写 `ai_original`（schema 版本随之升到 19）。

## 不可变性

模式在创建时定轴，之后任何写入面都不接受它：项目 PATCH 模型（`UpdateProjectRequest`，`extra="forbid"`）不声明该字段，携带即 422；集级补丁与 Agent 项目设置白名单同样没有它；`create_project_metadata` 把它放进 `extras` 的保留集，非路由调用方也无法借 `extras` 覆盖。不可变性由「没有写入面」保证，而不是靠校验规则兜住——这与生成路线的锁定方式一致（`docs/adr/0055`）。

不可变的原因是模式同时决定入口与剧本来源：允许中途切换等于让同一个项目先由 AI 原创、后由原文改写，两条来源混写同一份 `source/short_drama/episodes/episode_NN.md`，下游的源稿 revision、过期判定与资产池口径都会失去唯一解释。需要另一种模式时另建项目。

## 模式互斥边界

`creation_mode` 是后续所有模式守卫的定轴字段，边界如下：

| 入口 / 链路 | `ai_original` | `source_import` |
| --- | --- | --- |
| 故事规划页与读接口 | 正常 | 入口不渲染；`/story` 重定向；读接口 422 |
| 「AI 原创剧本」按钮与工厂 Agent 生成 | 正常 | 不渲染、不开放 |
| `POST …/episodes/{n}/source-import` | 422 `source_import_mode_required` | 唯一开放 |
| `ingest_short_drama_artifacts`（工厂工件入库） | 正常 | 拒绝，防止两条剧本来源混写 |
| 资产 / 分镜 / 宫格 / 视频 / 剪辑 | 不变 | 不变（同一条链路） |

剧本的唯一真相源仍是 `source/short_drama/episodes/episode_NN.md`。`source_import` 的改写输出**必须逐字适配现有剧本结构**：能被 `parse_short_drama_episode` 完整解析为 beats，再由 AI 分镜转成 `scripts/episode_N.json` 的 `scenes[]`。不新增剧本格式，不直接写 `scripts/*.json`——直接写视觉状态会绕过 AI 分镜的 beat 分组、资产解析、时长与画面规划，等于另开一条视觉链路。该格式约束由本 ADR 固定；具体生成、校验与落盘在「导入原文」链路中实现。

导入模式下资产池仍由现有资产步骤维护（只补缺失、不覆盖已有描述），`creation_mode` 不触发任何自动资产登记：模式只表达剧本来源，不偷偷改变资产页的初始状态。

## 存量兼容与读侧缺省

- v18→v19 迁移（`lib/project/project_migrations/v18_to_v19_creation_mode.py`）只补写缺失或空值，已有非空值原样保留；幂等，崩溃可重试。迁移只改 `project.json`，不改剧本与产物清单——创作模式不进任何产物的生成依据，改写不让任何登记过期。
- 读侧 `resolve_creation_mode` 对缺失与无法识别的值回退 `ai_original`，覆盖迁移未跑到的归档、演示数据与手工构造的 project.json，读取永不因新字段报错。
- `DataValidator` 对缺失放行（只针对旧数据），对出现但非法的取值报错；归档导入在迁移之后校验，导出的 `project.json` 原样携带该字段，无需白名单。
- 创建请求不传 `creation_mode` 即 `ai_original`，不自动建集，AI 原创行为与改动前完全一致。`source_import` 创建成功后自动建第 1 集并在响应回带 `first_episode`——导入入口在剧本页，没有集就没有可进入的剧本页。

## 明确不采用

- **复用 `content_mode` 增加取值**：它是跨骨架、跨校验、跨提示词、跨供应商能力的枚举，新增取值会牵动内容解析与归档校验；本需求不改内容类型，只改剧本来源。
- **模式可中途切换**：见「不可变性」——两条剧本来源混写同一真相源，下游 revision 与过期判定没有唯一口径。
- **只靠读侧默认、不做迁移**：每个读点都要重复一次缺省映射，写侧仍能产出缺字段的 `project.json`，映射永远删不掉（同 `docs/adr/0055` 的结论）。
- **把模式写进剧本或集**：剧本与集是内容载体，模式是项目属性；写两份会产生可分歧的第二个真相源。

## Consequences

- `project.json` 新增顶层字段，schema 升到 19；新建项目落盘即含显式值。
- 归档导出/导入、演示项目与旧数据靠迁移 + 读侧缺省兼容：缺字段不报错，非法值在归档边界被点名。
- 前端按 `creation_mode` 收敛入口（故事规划、AI 原创按钮、剧本页导入面板），后端按同一字段做模式守卫；两侧都不需要第二个「是否导入」标志。
- 导入原文的剧本格式由现有解析器契约约束，后续改动解析器即同时约束该模式的输出。
