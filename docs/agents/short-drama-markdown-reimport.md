# 短剧工厂源稿与视觉制作

修改短剧工件入库、源稿读取、资产准备、AI 分镜、参考素材、视频提示词或视频请求前，先阅读本文。功能要求以 `方案设计/` 中的六份方案为准；本文记录工程接口和跨步骤不变量。

## 六步职责

| 步骤 | 输入 | 输出 | 写入边界 |
| --- | --- | --- | --- |
| 工厂生成 | 创意简报、篇幅档位、全剧总集数、已有系列工件 | 七类 Markdown 工件 | 原样写入工件，并登记 OWN 集源文 |
| 资产准备 | 本集源稿、人物圣经、大纲、连续性台账、分集规划 | 本集角色、场景、道具的登记、描述和资产图 | 项目资产表及资产文件 |
| AI 分镜 | 已保存的本集源稿、已登记且有描述的资产、本集事实卡（钩子 / 情绪流变 / 资产外形与来路） | 12–15 秒分镜段、分镜提示词、原稿锚点、动作与台词 | `scripts/episode_N.json` 视觉制作状态 |
| 参考素材 | 分镜提示词、引用资产、宫格规格 | 宫格图、按时序编号的 K 帧、确认绑定 | 宫格记录、图片及视觉制作状态的素材绑定 |
| 视频提示词 | 本集源稿、分镜提示词、已确认 K 帧的文本上下文 | `video_prompt` 与 `video_prompt_basis` | 对应分镜的视频提示词字段 |
| 视频生成 | 已保存的视频提示词、已确认 K 帧、前镜接续来源、角色声音配置 | 视频及完整输入证据 | 视频文件、版本、清单与分镜视频产物字段 |

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

未登记或已登记但缺描述的本集资产阻断 AI 分镜。即使源稿已写外观，未登记资产也须先经过资产步骤；AI 分镜不会代为登记。跨类型同名按各自类型处理。

AI 补描述以源稿动作、台词和系列设定为依据。已有描述不覆盖，空白模型结果记单项失败。生成资产图前跳过已就绪资产，并展示本集实际预估。

前端 `EpisodeAssetsView` 展示角色、场景、道具页签。页头生成入口改为「交给 Agent」：Agent 用 `patch_project` 登记/描述，缺图再走 `generate_assets`；页面保留查看与编辑。

## AI 分镜与重新分镜

生产入口保留以下 URL 名称：

```http
POST /projects/{project_name}/episodes/{episode}/short-drama-source/reimport
Content-Type: application/json

{
  "source_revision": "<sha256>",
  "confirm_artifacts": false,
  "planning_strength": 2,
  "force_regenerate": false,
  "regenerate_scene_ids": []
}
```

路由调用 `server/services/short_drama_workflow.py:import_short_drama_episode`，必须传 `save_source=False`、`ai_planning=True`。该路径仅调用文本模型，既不读取视频模型档位，也不生成媒体。

- 空 `regenerate_scene_ids` 表示整体分镜；非空表示只重写所选分镜。
- `planning_strength` 为 1–3；强制重生成在全选与部分选择时均可用。
- 同源、同强度且不强制重生成时可复用已有结果，并保留人工修改和媒体状态。
- 部分重写保留未选分镜的全部状态，清空所选分镜旧的 `video_prompt`。
- 有已有媒体且未确认影响时返回 409。
- AI 结果必须覆盖所有 beat，保留动作、神态、台词与原始 `source_text`，每段按 12–15 秒组织。
- 景别、机位、运镜、主体动作与神态分别校验；拒绝泛写、缺环境、未登记锚点、编号变化和高重复段落。
- 台词只存在于 `utterances` / `source_text`；`image_prompt` 与后续 `video_prompt.action` 都不得出现「台词：/对白：/画外音：」原句行。新模型输出含台词行时质量闸直接拒绝并回喂，只有读取历史脏脚本时 `parse_scene_shots` 才安全剥离并留日志/告警。
- 场景/人物/道具按幕解析：`scenes` 取规划声明的场景或提示词里的登记场景 token（二者不一致即拒），`characters_in_scene` 取本幕动作 beat 中出现的登记角色 + 台词 speaker，`props` 取本幕 beat 文本中出现的登记道具；转换层与质量闸使用同一份 `SceneAssets`，token 必须落在本幕清单内。只在 `[人物]/[道具]` 声明、却未在本幕 beat 出场的资产不再获得参考图。
- 工厂源稿允许先列全部 `[场景]` 再写 beat：连续 `[场景]` 行作为同一块的场景清单解析，场景归属由每幕提示词的场景 token 指认，不再把所有 beat 归到最后一个场景块。
- 规划输入除 beats 与资产名外，还带「本集事实卡」：`【黄金钩子】`、`【情绪流变】`、`【本集时长】` 与已登记的场景 / 人物 / 道具 `description`（`build_episode_brief`）。模型据此写穿戴、持物、道具形状与来路，不得自创卡片外的事实；把「末端系着同心结」写成「整条盘成同心结」这类补全属于事实冲突。
- 各幕 `duration_seconds` 合计必须等于源稿 `【本集时长】`（±5%），缺口整批拒绝并回喂相差秒数；`duration_shortfall` 只作诊断保留。`force=False` 复用现有脚本时按同一条复算，不足即要求 `force_regenerate=true`。
- 地点资产与 `[场景]` 声明解耦：`[场景]` 只声明「完整场景」并受工厂机检 ≤2 约束；钩子、情绪流变、回忆里出现的地点仍须登记为场景资产。分镜侧按「已登记场景的地点名出现在本集源稿、却未写进 `[场景]`」判定为**碎片空间**（`_fragment_scenes`，无白名单），把该地点连同描述写进事实卡并允许其 token：碎片空间可以出画面、可以拿参考图，但规则要求只能以光 / 声 / 叠影出现，不得当作本幕主空间，也不得把发生在那里的动作搬进本幕。换成任何地点、任何一集都按同一口径识别。
- 校验失败带原因返修，最多 `_PLAN_ATTEMPTS` 轮；耗尽后失败，不用机械骨架回落。
- 返修分两级：beat 覆盖/顺序/时长/场景解析等全局结构失败才全量重生成；全局通过、仅单个 scene 的 lint 失败时，下一轮只发送该 scene 的固定 beat 组、本幕允许资产、旧 description/image_prompt 与拒绝原因，要求返回单个 scene 替换稿。替换稿必须保持 beat_ids 与 duration 不变，拼回后再次做覆盖与全 scene lint；局部连续失败或拼回后全局校验失败才回退全量。全量与局部修复共用 5 轮预算，每轮仍写一条 `quality_gate_round`，进度与 `planning_rounds` 语义不变。
- 规划缓存版本为 `v6`（校验口径与输入事实卡变更即失效）；`force=False` 复用现有脚本前会按新闸复核台词行、单幕元数据与各幕时长合计，不合规则明确要求 `force_regenerate=true`，不静默复用脏脚本。

MiniMax 等 `json_schema` 不可靠的供应商，分镜请求改走轻量 Markdown（`【分镜N｜beats a-b｜N秒】` + `描述：` + `【镜头N｜景别】` 块），由本地解析成同一 `ScenePlanResult`；后续质量闸、缓存与落盘事务不变。其他供应商仍走原生结构化输出。

Agent 入口 `plan_short_drama_storyboards` 与上述路由共用 `import_short_drama_episode`：新分镜省略 `regenerate_scene_ids`；部分重写传 ID；全量重写传 `force_regenerate`；已有分镜图或视频时须先取得用户同意并传 `confirm_artifacts`。质量校验、返修轮次与落盘事务完全一致，Agent 不参与判定。

模型调用前冻结当前视觉状态的 canonical fingerprint。提交时在项目事务内复核源稿 revision、资产前提及视觉状态 fingerprint；任一变化都拒绝覆盖。正式文件写入使用 `run_sync_transaction`，请求取消也要等待事务结算。

成功响应由 `ShortDramaImportResult.as_dict()` 提供，包括集号、分镜数、源稿路径/revision、缓存与 AI 编写状态，以及适用的重写和时长诊断。

主要拒绝：

| HTTP | 错误 | 含义 |
| --- | --- | --- |
| 404 | `short_drama_source_missing` | 源稿不存在 |
| 409 | `short_drama_source_conflict` | 源稿在执行期间变化 |
| 409 | `short_drama_script_conflict` | 视觉制作状态在执行期间变化 |
| 409 | `short_drama_reimport_confirmation_required` | 需确认已有媒体影响 |
| 422 | `short_drama_import_refused` | 缺资产前提、解析失败或 AI 返修耗尽，详情附原因 |

## 参考素材与视频提示词

参考素材编辑器保存才落盘；取消、Esc、关闭和 AI 回填都不保存。手写格数必须与规格一致，不截尾、不自动补成另一规格。生成与确认分别执行，确认前不得编写视频提示词。

参考素材的逐格提示词按单 shot 生成：spec 固定 rows/cols，输入固定该分镜的 scene/登记资产/项目画风；校验失败会带具体原因重试，最多再试两次，耗尽仍以原 `shot_grid_author_failed` 失败，不落盘、不影响其他 shot。

确认按宫格、切图、复制、绑定与清单登记的同一事务提交。`cell_paths` 从 `cells` 派生，顺序即 K0…Kn；解除确认解绑但保留图片。规格变化才重置出图，未变化的保存保留实际切图。

同分镜存在多个宫格记录时，`select_shot_grids` 统一按脚本显式绑定、confirmed、completed、最新的顺序选择；绑定缺失时不冒用其他记录。工作流、提示词生成和路由共用这个选择器。可消费的参考素材仅认 confirmed。

视频提示词独立生成，输入包括源稿、当前分镜提示词和同一冻结宫格的 K 帧文本上下文。正文 K 引用与包装字段均校验。提交前复核完整宫格、图片 SHA、源稿身份和视觉状态 fingerprint；只写视频字段，不重写上游。

提示词编写（image_prompt / video_prompt）先在整批响应上按 entry 逐条校验；某条失败后只重发该条目的上下文、旧视觉层与拒绝原因，成功条目原样保留，拼回后整批复验。video_prompt 的正文/K 引用错误也走同一条带原因重试路径，不再让一个条目直接拖垮整批；整批与局部修复共用 `_PROMPT_AUTHORING_ATTEMPTS=5` 的调用预算。K 帧/宫格输入、`video_prompt_basis`、覆盖确认与落盘事务保持不变，耗尽仍不落半成品。

`video_prompt_basis` 记录源稿、分镜与素材依据。素材变化后提示建议重写；视频请求仍按当前实际图片组织，不静默裁剪。

## 视频输入与连续性

`lib/script/storyboard_sequence.py` 纯解析完整单集顺序，`lib/script/storyboard_video_request.py` 统一输出声明、资产链接、声音顺序与图片证据。

| 槽位 | 有前镜接续来源 | 首镜或接续来源缺失 |
| --- | --- | --- |
| `start_image` | 前镜成片实际尾帧 → 前镜用户快照 → 前镜宫格末格 | 本镜 K0，无宫格则分镜图 |
| `reference_images` | 本镜 K0…K(n-1) | 本镜 K1…K(n-1) |
| `end_image` | 本镜 Kn | 本镜 Kn；仅一格时省略 |

无宫格仅发送首帧。每格只占一个槽，不重复下发首末格。worker 才抽取前镜成片的实际尾帧，预览不抽帧、不写盘。抽帧缓存不覆盖用户快照。

`EndFrameRow` 的 upload/select/clear 三个端点、`end_frames/scene_{id}.png` 路径和本镜的 `end_frame_image` 字段保留。快照供下一镜的 `start_image` 消费，本镜 `end_image` 来自宫格末格，因此快照入口不按模型尾帧能力告警。

准入使用重新投影后的实际参考数组长度；超限整批零入队，付费前再次校验。选中子集也必须传完整单集顺序，不能把子集第一镜误判为首镜。K 引用必须属于实际下发编号，无宫格时任何 K 引用都非法。

音色复刻按角色台词 speaker 首现顺序去重，正文「音频 N」与真实文件同序。per-image 绑定只使用宫格格文本中显式的角色提及；映射到实际参考数组下标，缺证据拒绝。音频段数和总时长沿用能力硬闸。

视频版本、checkpoint 与 currency 保留全量图片来源、每格 K 编号、宫格 ID/规格/整图 SHA、资产身份链接及声音依据。付费前复核前镜和本镜依赖、源文件 SHA 和最终文本，禁止并发替换后仍按旧输入计费。

方案 6 的 R19 下发措辞及资产链接格式仍属草案，需要实际供应商验证；R20 存量 `reference_video` 处置尚未定，不能自行选择迁移或删除。

## 页面与工作流

- 工厂身份按实际非空 Markdown 判断，不能只看 `short_drama_meta` 或 JSON 是否存在。
- 「故事规划」和单集「剧本」只读展示工件原文；项目更新、集切换后重新读取，网络请求用 AbortSignal 取消。
- 无 JSON 时也计算本集资产缺口，下一步为 `prepare_episode_assets` 或 `plan_short_drama_storyboards`。
- `prepare_episode_assets` 由 Agent 用 `patch_project` 登记/描述；`plan_short_drama_storyboards` 由 Agent 调同名工具。Web 按钮保留为手动入口。
- 工厂不受原生 episode plan、script plan、内容确认或旧待修复草稿影响。
- 分镜提示词已齐、仅缺视频提示词时进入 `author_video_prompts`，不回到 AI 分镜。
- 工厂时间线与工作流隐藏重复的独立分镜图生成步骤，参考素材是对应媒体入口。
- AI 分镜开窗和提交均校验只读、占用与资产前提；写入成功后才刷新，刷新失败只重试刷新，不重复提交。

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
- confirmed 唯一消费前提、同镜多记录选择、K 引用与视频提示词提交复核。
- 全单集前镜上下文、槽位去重、实际尾帧抽取、音频顺序与整批超限零入队。
- 付费前依赖变更拒绝，以及前镜、全部 K 帧和整图变化的 currency 判断。

本项目不运行 `pnpm e2e:record` 与 `pnpm e2e`；响应形状变化只做静态核对，页面与滚动变更只做代码走读。需要人工确认时，按六步入口、只读源稿、弹窗取消与关闭、占用状态、成功后的刷新和失败后的恢复逐项操作浏览器。
