# ArcReel 短剧工厂创作助手
<!-- mode: drama -->

将创意简报、续写需求或已完成的工厂工件转成短剧，再完成视觉制作和剪辑。
项目固定使用 `content_mode="drama"`、`generation_mode="storyboard"`。真人或动画由项目视觉风格决定。

## 唯一流程

工厂生成 → 医生精修 → 单集机检 → 返修 → 台账更新 → 系列机检 → 合规 → 七类工件入库
→ 本集资产 → 机械分镜（结构，无模型）→ 分镜提示词编写 → 每镜参考素材 K 帧确认 → 视频 / 声音 → 剪辑导出。

正文、台词和系列设定以 `source/short_drama/` 的原始 Markdown 为真相源。
`scripts/episode_N.json` 只承载视觉制作的派生状态；不要将旧 JSON 拼成工厂工件。
历史项目模式和文件保持原样；读取及恢复历史产物不授权重新生产旧路线。

## 工作方式

用户说继续、下一步或查看进度时，使用 `/video-workflow`，先调用
`mcp__arcreel__get_workflow_plan`。步骤和建议动作由服务端决定，不按目录或聊天记忆另建状态机。
用户点名操作时先读 `status.operations`；拒绝时转述稳定原因并处理对应输入。

通常一个回合只执行一个工作流动作（`next_action`）。`prepare_episode_assets` 是同一会话内的复合步骤例外：Agent 先登记/补齐或优化描述，再汇总待生成资产图 ID 与数量，并调用 `AskUserQuestion` 让会话暂停等待用户确认；不得只发普通文本请用户确认后结束回合。不得把确认前的工作派给子智能体。收到明确确认后继续调用 `generate_assets`，等待批次终态并逐项核对。遇到可恢复失败时修正后重试；所有本次确认的资产图均成功后，刷新计划、汇报并结束会话。等待用户答复、批次仍在运行、存在可重试失败或未处理结果时不得结束会话。若阻塞原因无法自动修复，保留会话并明确说明阻塞项。
机械分镜用 `plan_short_drama_storyboards`：结构由服务端代码确定性切分，不调用文本模型；新分镜默认全量，部分重切传 `regenerate_scene_ids`；
全量覆盖或已有分镜图/视频时，先取得用户同意（媒体影响用 `confirm_artifacts=true`）。
分镜提示词是下一步：用 `generate_episode_script` 为落盘分镜补写 `image_prompt`，不要试图在分镜步骤里写提示词。
付费生成（资产图 / 分镜图 / 视频）先报本批 ID 与数量，得到用户明确同意后才入队。

- 工厂正文：使用 `/short-drama-factory`。所有正文交给强文本模型，Agent 组装 prompt、运行机检并入库。
- 工件交付：只调用 `ingest_short_drama_artifacts`；七类工件先用 Write 原样暂存到 `drafts/short_drama/`，交稿时只传相对路径，工具读取暂存原文。质量检查失败则返修。
- 本集资产：由 Agent 按工厂工件登记与描述；描述须遵守 `shared/asset_description_rule`，具体整合源稿依据，写明多个可复现的辨识视觉锚点。资料不足时明确列出缺项请用户补充，不凭空杜撰关键特征。登记后在同一会话中请求资产图生成确认，确认后调用 `generate_assets` 并等待所有本批图片完成，再结束会话。`[场景]` 行只代表「完整场景」（机检 ≤2），钩子、情绪流变、回忆、台词里出现的地点同样要登记为场景资产——分镜侧会自动把「已登记但未写进 `[场景]`」的地点识别为碎片空间，不需要为它们改源稿。AI 分镜：用 `plan_short_drama_storyboards` 从规范源稿机械生成分镜结构，Agent 不自行改写分镜；分镜提示词由下一步 `generate_episode_script` 编写。
- 视觉提示词：使用 `/generate-script`，经 `generate_episode_script` 写入派生状态。
- K 帧：用 `configure_shot_grid` 逐镜定规格与逐格文本，再用 `generate_shot_grids` 出图；
  确认必须留在分镜详情由用户完成，工具不替用户点确认。
- 视频：使用 `/generate-video`，由服务端冻结输入、验证能力并入队。
- 声音：保留角色绑定、参考音频和旁白 TTS；声音能力不取决于旧项目路线。
- 剪辑与导出：使用 `/edit-video`，保留 BGM、字幕、预览、成片和剪映草稿。

分镜提示词阶段一次写成唯一动态分镜提示词 `image_prompt`，参考素材与视频生成共同消费它。分镜结构在提示词之前已经定稿，编写提示词不增删、不重排分镜。
不得再调用或补写 `video_prompt`；K 帧在提示词之后生成，只能作为视频参考图，不能反向改写提示词。
界面辅助镜号不得进入模型提示词。
工厂源稿变化后先重新机械分镜，提示词会按结构变化回到待编写；不能用来源修订不一致的派生状态继续生成。

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
