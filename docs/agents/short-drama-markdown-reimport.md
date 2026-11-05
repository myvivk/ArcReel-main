# 短剧工厂源稿与视觉制作

修改短剧工件入库、源稿读取、资产准备、机械分镜与分镜提示词、参考素材或视频请求前，先阅读本文。功能要求以 `方案设计/` 中的方案为准；本文记录工程接口和跨步骤不变量。

## 六步职责

| 步骤 | 输入 | 输出 | 写入边界 |
| --- | --- | --- | --- |
| 工厂生成 | 创意简报、篇幅档位、全剧总集数、已有系列工件 | 七类 Markdown 工件 | 原样写入工件，并登记 OWN 集源文 |
| 资产准备 | 本集源稿、人物圣经、大纲、连续性台账、分集规划 | 本集角色、场景、道具的登记、描述和资产图 | 项目资产表及资产文件 |
| 机械分镜 | 已保存的本集源稿、已登记且有描述的资产 | 12–15 秒分镜段、出场资产、逐字口播、原文锚、视觉改编摘要；`image_prompt` 留空待编写 | `scripts/episode_N.json` 的结构层 |
| 分镜提示词 | 机械分镜产物（分镜号、时长、资产、口播、原文锚、视觉改编）、项目风格与资产事实 | 唯一动态分镜提示词 `image_prompt` | `scripts/episode_N.json` 的视觉层 |
| 参考素材 | 动态分镜提示词、引用资产、宫格规格 | 宫格图、按时序编号的 K 帧、确认绑定 | 宫格记录、图片及视觉制作状态的素材绑定 |
| 视频生成 | 动态分镜提示词、已确认 K 帧、引用资产图、角色声音配置 | 视频及完整输入证据 | 视频文件、版本、清单与分镜视频产物字段 |

工厂源稿是剧情与对白的唯一真相源。后五步只读源稿，不改写、不迁移源稿路径。`scripts/episode_N.json` 是派生的视觉制作状态，不称为工厂「正式脚本」。原生小说规划、事件提取、脚本内容确认不参与工厂生产。

## 工件路径与入库

工件相对 `source/short_drama/` 保存：

| 类型 | 路径 |
| --- | --- |
| 人物圣经 | `character-bible.md` |
| 全剧大纲 | `series-outline.md` |
| 连续性台账 | `ledger.md` |
| 分集规划 | `episode-plan/episode_NN.plan.md` |
| 集正文 | `episodes/episode_NN.md` |
| 机检报告 | `reports/episode_NN.machine.md` |
| 合规报告 | `compliance/episode_NN.compliance.md` |

`NN` 为至少两位的正整数集号。源稿路径由 `lib/script/short_drama_source.py` 统一提供；revision 为文件原始 UTF-8 字节的 SHA-256。

新工厂入口为 `generate_short_drama_factory` 和 `ingest_short_drama_artifacts`。生成调用用户配置的 FACTORY 文本任务，映射到 COMPLEX 强模型；Agent 负责初稿、医生精修、机检、返修、台账更新、系列机检和合规检查。

`lib/short_drama_ingest.py` 在项目锁内校验完整批次。系列工件可复用已有版本；每个交付集必须同批包含规划、正文和两类报告。服务端复用工厂的 E1–E14 / S1–S7 机检，报告还须明确写出最终结论 `结果: PASSED`。空正文、不完整工件、机检失败或报告未通过均整批拒绝。

工件原始字节与 OWN 源文登记在同一事务提交，失败回滚。入库不转制原生 JSON、不登记资产、不调用图像或视频生成。系列工件变化会发出项目更新事件，阅读器随后补拉。

Agent 先用 Write 把待检原文按相同相对路径暂存到 `drafts/short_drama/`；调用 `ingest_short_drama_artifacts` 时每项只传 `path`，服务端从该暂存目录读取原样 UTF-8 字节，不要求把 132KB 级原文编码成 Base64 抄进工具参数。外部 Agent 无法共享文件系统时仍可传 `content_base64`。

目标集须先由前端或 Agent 创建；没有集账本时工件仍可保存，但跳过该集的源文登记。

HTTP 前缀为 `/api/v1`：

- `GET /projects/{project_name}/short-drama/artifacts`：读取工件。
- `GET /projects/{project_name}/episodes/{episode}/short-drama-source`：读取本集源稿及分镜前置检查。

工件写入只有 Agent 的 `ingest_short_drama_artifacts` 入口，Web 端不再提供手动上传接口。

源稿 GET 返回 `episode`、`path`、`markdown`、`revision`、`exists` 和 `assets_missing_description`。不存在时返回空文本、空检查列表与 `revision=null`，不得从 JSON 的 `source_text` 拼接原稿。

## 资产准备

资产集合从本集源稿与分集规划读取，不从派生 JSON 反推，也不把全系列资产混入本集。上下文包括 `character-bible.md`、`series-outline.md`、`ledger.md` 和当前集规划。

未登记或已登记但缺描述的本集资产阻断机械分镜。即使源稿已写外观，未登记资产也须先经过资产步骤；跨类型同名按各自类型处理。

AI 补描述以源稿动作、台词和系列设定为依据。已有描述不覆盖，空白模型结果记单项失败。生成资产图前跳过已就绪资产，并展示本集实际预估。

资产准备是同一 Agent 会话内的复合步骤：登记/补齐描述后，Agent 汇总本集待生成资产图的 ID 与数量并等待用户明确确认；确认后调用 `generate_assets`，等待批次终态、逐 ID 检查并按回执修复可恢复失败。所有确认的资产图完成后再刷新计划并结束会话。前端 `EpisodeAssetsView` 展示角色、场景、道具页签；页面入口交给 Agent，由 `patch_project` 登记/描述并按上述确认流程生成图片，页面保留查看与编辑。

## 机械分镜与分镜提示词

生产入口保留以下 URL 名称：

```http
POST /projects/{project_name}/episodes/{episode}/short-drama-source/reimport
Content-Type: application/json

{
  "source_revision": "<sha256>",
  "confirm_artifacts": false,
  "force_regenerate": false,
  "regenerate_scene_ids": []
}
```

路由调用 `server/services/short_drama_workflow.py:import_short_drama_episode`。该路径只做确定性结构生成：不调用文本模型、不读供应商配置、不生成媒体。

- 空 `regenerate_scene_ids` 表示整体机械分镜；非空表示按同一份源稿重切，并把点名分镜的提示词清回待编写。
- `planning_strength` 已废弃；旧客户端传了也会被忽略，服务端不再返回该字段。
- 同一 `source_revision` 与 `splitter_version`、且未强制重切时直接复用既有结构（响应 `reused=true`），不写盘。
- 结构规则：beat 连续覆盖、不劈断；单段 12–15 秒；切点优先 `[场景]` 块边界，台词拍不单独成段首；台词按语速估算并给画面留至少一个镜头预算，超窗整批拒绝。
- 时长按 beat 权重分摊并校准到源稿 `【本集时长】`；窗口装不下时 `duration_shortfall` 只作诊断，不让窗口为凑数让步。
- 场景/人物/道具由代码确定性归属：`characters_in_scene` 取本段动作 beat 中出现的登记角色 + 台词 speaker；`props` 取本段 beat 文本中出现的登记道具；`scenes` 按「块内唯一场景 → beat 文本点名 → 全集唯一场景 → 首场景 + `scene_attribution_ambiguous` 告警」判定。
- 碎片空间沿用 `_fragment_scenes`：已登记但未写进 `[场景]` 的地点只以光 / 声 / 叠影出现，由提示词步骤写进首镜。
- `image_prompt` 留空（`null`）并置 `pending_authoring=true`；分镜提示词由下一步 `generate_episode_script` 读取该产物编写，参考素材与视频在提示词写齐前不得消费。
- 有已有媒体且未确认影响时返回 409；结构变化或点名重切后，所选分镜的参考素材与视频按 Artifact Manifest 判为过期。
- 提交时在项目事务内复核源稿 revision、资产前提与视觉状态 fingerprint；任一变化都拒绝覆盖。正式文件写入使用 `run_sync_transaction`。

Agent 入口 `plan_short_drama_storyboards` 与上述路由共用 `import_short_drama_episode`：新分镜省略 `regenerate_scene_ids`；部分重切传 ID；全量重切传 `force_regenerate`；已有分镜图或视频时须先取得用户同意并传 `confirm_artifacts`。Agent 不参与结构判定，也不在分镜步骤里写提示词。

成功响应由 `ShortDramaImportResult.as_dict()` 提供：集号、分镜数、源稿路径/revision、`mode="mechanical"`、`splitter_version`、`pending_authoring`、`reused`、`warnings`，以及适用的重写和时长诊断。

提示词编写步骤（`server/text_generation.py:generate_episode_script`）：

- 只读正式脚本里的分镜产物：`scene_id`、时长、出场资产、`utterances`、`source_text`、`scene_description`，外加已编写相邻分镜的「上一分镜末镜」作承接上下文；不重新解析 Markdown 分镜。
- 只写 `image_prompt`；输出 schema、lint、四层结构、时间轴覆盖、承接与碎片空间校验沿用原有质量闸；失败最多 5 轮返修，耗尽整批拒绝、不写半成品。
- 写回后清除 `pending_authoring`；`image_prompt` 变化使参考素材与视频过期。

主要拒绝：

| HTTP | 错误 | 含义 |
| --- | --- | --- |
| 404 | `short_drama_source_missing` | 源稿不存在 |
| 409 | `short_drama_source_conflict` | 源稿在执行期间变化 |
| 409 | `short_drama_script_conflict` | 视觉制作状态在执行期间变化 |
| 409 | `short_drama_reimport_confirmation_required` | 需确认已有媒体影响 |
| 422 | `short_drama_import_refused` | 缺资产前提、解析失败、台词超窗或源稿无法成段，详情附原因 |

## 参考素材与唯一动态提示词

参考素材编辑器保存才落盘；取消、Esc、关闭和 AI 回填都不保存。手写格数必须与规格一致，不截尾、不自动补成另一规格。生成与确认分别执行；只有 confirmed 宫格可以进入视频生成。

参考素材的逐格提示词按单 shot 生成：spec 固定 rows/cols，输入固定该分镜的 scene/登记资产/项目画风；每格必须是冻结的单一瞬间，写了镜头运动、先后动作或渐变过程会被 `keyframe_cell_problem` 拒绝并带原因重试，最多再试两次，耗尽仍以原 `shot_grid_author_failed` 失败，不落盘、不影响其他 shot。

确认按宫格、切图、复制、绑定与清单登记的同一事务提交。`cell_paths` 从 `cells` 派生，顺序即 K0…Kn；解除确认解绑但保留图片。规格变化才重置出图，未变化的保存保留实际切图。

同分镜存在多个宫格记录时，`select_shot_grids` 统一按脚本显式绑定、confirmed、completed、最新的顺序选择；绑定缺失时不冒用其他记录。工作流、路由和视频准入共用这个选择器。视频消费只认 confirmed。

`image_prompt` 是参考素材与视频生成共同消费的唯一动态提示词。分镜提示词编写与显式提示词重写共用 `storyboard_motion_prompt_problem`：按 entry 校验时间轴、四层制作信息、资产 token 和前向 K 引用；失败条目带原因重试，成功条目保持不变。整批与局部修复共用 `_PROMPT_AUTHORING_ATTEMPTS=5` 的调用预算，耗尽仍不落半成品。

历史 `video_prompt` 与 `generated_assets.video_prompt_basis` 只用于无损读取旧项目。新流程不生成、不编辑、不比较这两个字段，也不把它们纳入工作流、准入或产物时效。

## 视频输入与连续性

`lib/script/storyboard_sequence.py` 解析 confirmed 宫格、引用资产图与完整单集顺序，`lib/script/storyboard_video_request.py` 统一输出资产链接、声音顺序与图片证据。

分镜视频固定使用 reference/r2v，不接受 `first_last` 或用户可选 `frame_mode`。`start_image` 与 `end_image` 均不下发；`reference_images` 的顺序固定为角色资产图 → 场景资产图 → 道具资产图 → K0…Kn。K 帧优先占用模型参考图上限，剩余名额再按该顺序保留资产图；K 帧本身超过上限时准入失败，整批零入队。

视频准入同时要求合法动态 `image_prompt` 和完整 confirmed 宫格绑定。准入、提示词预览与 worker 共用同一参考图投影；付费前再次复核参考图、源文件 SHA、提示词和声音输入，禁止并发替换后仍按旧输入计费。

音色复刻按角色台词 speaker 首现顺序去重，正文「音频 N」与真实文件同序。per-image 绑定只使用宫格格文本中显式的角色提及；映射到实际参考数组下标，缺证据拒绝。音频段数和总时长沿用能力硬闸。

视频版本、checkpoint 与 currency 保留唯一动态提示词、全量实际下发图片、每格 K 编号、宫格 ID/规格/整图 SHA、资产身份链接及声音依据。

## 页面与工作流

- 工厂身份按实际非空 Markdown 判断，不能只看 `short_drama_meta` 或 JSON 是否存在。
- 「故事规划」和单集「剧本」只读展示工件原文；项目更新、集切换后重新读取，网络请求用 AbortSignal 取消。
- 无 JSON 时也计算本集资产缺口，下一步为 `prepare_episode_assets` 或 `plan_short_drama_storyboards`。
- `prepare_episode_assets` 由 Agent 用 `patch_project` 登记/描述；`plan_short_drama_storyboards` 由 Agent 调同名工具。Web 按钮保留为手动入口。
- 工厂不受原生 episode plan、script plan、内容确认或旧待修复草稿影响。
- 分镜提示词与 confirmed 参考素材已齐时直接进入 `generate_videos`；不存在 `author_video_prompts` 动作。
- 工厂时间线与工作流隐藏重复的独立分镜图生成步骤，参考素材是对应媒体入口。
- 分镜提示词编写的开窗和提交均校验只读、占用与资产前提；写入成功后才刷新，刷新失败只重试刷新，不重复提交。

## 兼容接口

旧 `short-drama/import`、`short-drama/import-batch` 和源稿 PUT 保留给历史入口，其显式兼容契约不得成为新工厂生产入口。旧导入可以生成机械结构并登记资产，新工厂交稿只能走完整工件入库。

读取源稿优先现行路径，不存在时兼容 `source/short_drama/episode_N.md`。不得因旧路径存在而迁移或覆盖工厂源稿。没有源稿的历史集仍用已有兼容界面，不伪造 Markdown。

主线 `patch_episode_script` 的 `base_revision` 与 `update.fields` 能力保留。删除集须将对应源稿纳入正式文件回滚范围；相关改动另读项目迁移与删除契约。

## 校验

按 `docs/agents/testing.md` 的测试策略（本项目不运行测试）做代码走读与静态审查，最后运行 `AGENTS.md` 的完整闸门。重点覆盖：

- 工件完整批次、机检同源、报告结论、原样字节和事务回滚。
- 无 JSON 的工厂源稿阅读、资产集合、描述前提与原生规划隔离。
- AI 全量/部分重写、缓存、无机械回落及源稿/视觉状态并发冲突。
- 宫格取消不保存、规格与格数硬匹配、确认/解除确认与任务失败恢复。
- confirmed 唯一消费前提、同镜多记录选择、唯一动态提示词格式与前向 K 引用禁止。
- reference/r2v 固定模式、资产图 → K 帧顺序、K 帧优先占位、音频顺序与整批超限零入队。
- 付费前依赖变更拒绝，以及动态提示词、资产图、全部 K 帧和整图变化的 currency 判断。

本项目不运行 `pnpm e2e:record` 与 `pnpm e2e`；响应形状变化只做静态核对，页面与滚动变更只做代码走读。需要人工确认时，按五步入口、只读源稿、弹窗取消与关闭、占用状态、成功后的刷新和失败后的恢复逐项操作浏览器。
