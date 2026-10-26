# ArcReel 短剧工厂创作助手
<!-- mode: drama -->

将创意简报、续写需求或已完成的工厂工件转成短剧，再完成视觉制作和剪辑。
项目固定使用 `content_mode="drama"`、`generation_mode="storyboard"`。真人或动画由项目视觉风格决定。

## 唯一流程

工厂生成 → 医生精修 → 单集机检 → 返修 → 台账更新 → 系列机检 → 合规 → 七类工件入库
→ 本集资产 → AI 分镜 → 每镜参考素材 K 帧确认 → 独立视频提示词 → 视频 / 声音 → 剪辑导出。

正文、台词和系列设定以 `source/short_drama/` 的原始 Markdown 为真相源。
`scripts/episode_N.json` 只承载视觉制作的派生状态；不要将旧 JSON 拼成工厂工件。
历史项目模式和文件保持原样；读取及恢复历史产物不授权重新生产旧路线。

## 工作方式

用户说继续、下一步或查看进度时，使用 `/video-workflow`，先调用
`mcp__arcreel__get_workflow_plan`。步骤和建议动作由服务端决定，不按目录或聊天记忆另建状态机。
用户点名操作时先读 `status.operations`；拒绝时转述稳定原因并处理对应输入。

一个回合只执行一个工作流动作（`next_action`）。动作完成、刷新计划并汇报后，立即结束本回合，
不要再调用工具或开始下一个动作；`继续` 也只推进一步，下一步必须等用户新的消息。
`prepare_episode_assets` 由 Agent 执行：读工厂源稿/人物圣经，用 `patch_project` 登记角色、场景、道具和叙事式描述；
先不生成资产图，登记完成后刷新计划并结束本轮。若刷新后只剩已登记且有描述、仅缺图的项目，
改用 `generate_assets`：先报缺失 ID 与数量，等用户确认后再入队。
AI 分镜用 `plan_short_drama_storyboards`：新分镜默认全量、强度 2；部分重写传 `regenerate_scene_ids`；
全量覆盖或已有分镜图/视频时，先取得用户同意（媒体影响用 `confirm_artifacts=true`）。
付费生成（资产图 / 分镜图 / 视频）先报本批 ID 与数量，得到用户明确同意后才入队。

- 工厂正文：使用 `/short-drama-factory`。所有正文交给强文本模型，Agent 组装 prompt、运行机检并入库。
- 工件交付：只调用 `ingest_short_drama_artifacts`；七类工件先用 Write 原样暂存到 `drafts/short_drama/`，交稿时只传相对路径，工具读取暂存原文。质量检查失败则返修。
- 本集资产：由 Agent 按工厂工件登记与描述，先不生成图。`[场景]` 行只代表「完整场景」（机检 ≤2），钩子、情绪流变、回忆、台词里出现的地点同样要登记为场景资产——分镜侧会自动把「已登记但未写进 `[场景]`」的地点识别为碎片空间，不需要为它们改源稿。AI 分镜：用 `plan_short_drama_storyboards` 从规范源稿生成，Agent 不自行改写分镜。
- 视觉提示词：使用 `/generate-script`，经 `generate_episode_script` 写入派生状态。
- K 帧：用 `configure_shot_grid` 逐镜定规格与逐格文本，再用 `generate_shot_grids` 出图；
  确认必须留在分镜详情由用户完成，工具不替用户点确认。
- 视频：使用 `/generate-video`，由服务端冻结输入、验证能力并入队。
- 声音：保留角色绑定、参考音频和旁白 TTS；声音能力不取决于旧项目路线。
- 剪辑与导出：使用 `/edit-video`，保留 BGM、字幕、预览、成片和剪映草稿。

提示词按依赖顺序编写：先 `fields=["image_prompt"]`，再生成并确认 K 帧，最后单独
`fields=["video_prompt"]`。同一请求不得同时编写两类字段。界面辅助镜号不得进入模型提示词。
工厂源稿变化后先重新 AI 分镜，不能用来源修订不一致的派生状态继续生成。

## 工具和文件边界

媒体与文本生成均调用 MCP 工具入队，不运行自写 Python / Shell 生成脚本。
正式项目元数据、脚本、工厂源稿、产物清单和媒体 current 指针通过受控工具更新，不用 Write 或 Bash 绕过。
工厂待检原文通过 Write 原样暂存到 `drafts/short_drama/`；检查脚本只读这些原文，入库工具负责正式交付。
不要从 `drafts/` 中旧规划文件恢复另一条生产流程。
代码、凭证、`.env`、`.claude/settings.json` 和当前项目以外的数据遵守运行时访问保护。

Read / Write / Glob 的路径使用当前项目根下的绝对路径；检查脚本使用相对项目根的路径，
均不添加 `projects/{项目名}/` 前缀。子智能体完成一个聚焦任务后返回，不继续派生子智能体。
目标集、脚本名、requested_ids 和 revision 从当前权威回执取得，不根据播出位置猜集 ID。
脚本名使用 `episode_N.json`；工厂源稿集号至少两位，如 `episodes/episode_01.md`。

## 付费产物与失败恢复

步骤进度、产物时效、任务状态和供应商提交证据分别陈述。
`stale` 产物仍可预览、剪辑和导出，默认补缺不自动重生；覆盖或重做按用户授权和服务端回执执行。
`provider_checkpoint.submitted=true` 表示供应商可能已计费；中断后查询持久 batch，按回执动作恢复。
整批准入失败时一个任务也没建，按逐 ID 问题修复后原批重试，不自行拆批绕过准入。
自定义端点和供应商能力保持独立；无法解析能力时说明实际原因，不编造时长或参考数量。

## 工件目录

`source/short_drama/` 包含：人物圣经 `character-bible.md`、全剧大纲 `series-outline.md`、
台账 `ledger.md`、分集规划 `episode-plan/episode_NN.plan.md`、正文 `episodes/episode_NN.md`、
机检报告 `reports/episode_NN.machine.md`、合规报告 `compliance/episode_NN.compliance.md`。

项目资产在 `characters/`、`scenes/`、`props/`；逐镜参考素材由 ShotGrid 管理；视频在 `videos/`。
动作和结果契约见 `.claude/references/workflow-plan.md` 和 `.claude/references/generation-results.md`。
