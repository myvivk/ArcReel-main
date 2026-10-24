# 工作流计划与动作

`get_workflow_plan` 返回 `schema_version: 2`，包含 `status`、`steps`、`blockers`、`problems`、
`next_action` 和 `next_alternatives`。REST 与 MCP 共用同一序列化。
步骤适用性、顺序、进度和操作准入以服务端为准，不由 Agent 重算。

## 四条状态轴

- 步骤：`completed / ready / active / blocked / pending / skipped`。
- 产物：`current / stale / missing / blocked`；stale 仍可预览、剪辑、导出，不自动重生。
- 任务：队列、运行、成功、失败或中断；任务成功不证明 current。
- 供应商提交：`provider_checkpoint.submitted` 独立表示供应商已收单、可能已经计费。

`status.operations` 表示用户点名操作的结构准入：`admitted / refused / not_applicable`，
拒绝时 `reason` 为稳定码。`blockers` 表示项目整体不可用，`issues` 只陈述内容问题。
`status.content` 包含工厂正文、正式视觉状态、待编写条目和资产缺口，不提供旧路线规划门禁。
`status.artifacts` 枚举各类产物状态。`steps[].tasks` 显示在途任务，不把它们计作 current 产物。

## 受控动作表

`next_action.type` 只能取下表值。使用回执里的 `args`、`requested_ids` 和当前目标集，
不要按旧项目模式改选流程。

| 动作 | 执行方式 |
| --- | --- |
| `none` | 无变更；有 blocker 就展示原因。检查 issues 和完成状态。 |
| `collect_project_input` | 使用 short-drama-factory 完成内容质量闭环并入库七类工件，或 Web 工件目录导入。 |
| `create_episode` | Web 分集页新建工厂集账本，再交付工件。 |
| `prepare_episode_assets` | Web 本集资产步骤按工件准备角色、场景、道具和描述。 |
| `generate_asset_sheets` | generate_assets 按目标集补齐缺图；描述或引用缺口按逐 ID 问题处理。 |
| `plan_short_drama_storyboards` | Web AI 分镜从规范工厂源稿生成视觉派生状态，支持全量和指定范围。 |
| `author_prompts` | generate_episode_script，独立 fields=["image_prompt"]。 |
| `generate_storyboards` | 用户点名的工厂单图生成，经 generate_storyboards；不能替代 K 帧确认。 |
| `generate_shot_grids` | configure_shot_grid 逐镜定规格与逐格文本（可先 author_shot_grid_prompt），再 generate_shot_grids 出图；K 帧确认留给用户在分镜详情完成。 |
| `author_video_prompts` | 已确认 K 帧后 generate_episode_script，独立 fields=["video_prompt"]。 |
| `generate_videos` | generate_videos；整批准入 admitted 后入队。 |
| `create_edit_timeline` | 按 edit-video 新建本集剪辑时间线，按用户要求剪辑、预览和导出。 |
| `patch_episode_script` | 按问题字段修改视觉状态；get_episode_script 取 revision 后一次 patch_episode_script。 |
| `retry_project_migration` | 展示迁移失败细节，修复后 retry_project_migration；迁移失败前不修改产物。 |
| `retry` | 按回执重试失败目标；先排除在途或已提交任务，避免重复付费。 |
| `fix_input` | 修复回执指出的输入，再刷新计划。 |
| `generate_dependency` | 完成缺失依赖，再刷新计划。 |
| `wait_for_task` | 查询已有 batch 和任务，等待终态，不再入队。 |
| `replan_unit` | 在 AI 分镜步骤修复目标视觉分组；内容修改回到工厂返修。 |
| `confirm_request_duration` | 展示逐档位时长与费用，确认后把 confirmed_request_durations 带回原批。 |
| `configure_provider` | Web 设置修复模型与供应商配置，重新查询能力与准入。 |
| `repair_artifact_state` | 按回执修复产物登记或选择；不自动重生已付费媒体。 |
| `retry_artifact_download` | 恢复已完成供应商请求的下载，不重新提交生成。 |

## 请求选择与准入

默认补缺省略 ID 或使用工具的 episode scope。明确点名重做时才使用 selected scope 和 force。
计划空 `requested_ids` 不能传成工具显式空数组。

视频 `admission.decision` 为 `admitted / confirmation_required / blocked`。
后两者一个任务也没建；逐 ID 说明缺口，被同批缺口扣下的目标带
`generation_batch_admission_withheld`。修复后原批重试，不拆小批绕过。
`admission.confirmation.tiers` 给出申请时长档位、目标与费用；确认值只作用于本次请求，不写项目。

## 来源与声音

正式生成要求工厂源稿和视觉来源修订一致，源稿变更后先重新 AI 分镜。
提示词先 image_prompt，再 K 帧确认，最后单独 video_prompt，不同字段不得同批写入。

角色声音、参考音频和旁白 TTS 保留。项目选 TTS 时按缺失补齐 narrator 单元音频，
已经 stale 的音频仍复用，只有用户要求时重合成。TTS 不阻塞视频生成；后期配音项目无需 TTS。
剪辑声音和字幕时序读取服务端 presentation，Agent 不自行估算。

## 历史与恢复

历史模式数据不清空、不改模式、不自动转换为工厂工件；只读、迁移和已提交任务恢复保留。
外部 Agent 长任务返回持久 batch，按建议轮询间隔调用 get_generation_batch，终态后读取实际结果。
覆盖回执要求的 revision 必须来自当前输入；冲突后重新读取，不复用过期凭据。
