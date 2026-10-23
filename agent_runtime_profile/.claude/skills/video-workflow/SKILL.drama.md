---
name: video-workflow
description: 短剧工厂的创作与制作编排入口。用户要求做短剧、创建项目、继续、下一步、查看进度、续写或完成制作时使用；单个媒体重做使用对应生成 Skill。
---
<!-- mode: drama -->

# 短剧工厂工作流

每次进入、续作或完成动作后，调用 `mcp__arcreel__get_workflow_plan({})`。
用户指定一集时，按播出位置或标题在 `episodes[]` 找到集 ID，再传 `episode_id`。
使用 `plan.steps[]`、`plan.status.operations`、`next_action` 和 `next_alternatives` 作权威判断，
Read 只补充已选动作需要的内容，不按文件存在性或聊天记忆另建状态机。

固定流程：工厂质量闭环与七工件入库 → 本集资产 → AI 分镜 → 每镜 K 帧确认
→ 独立视频提示词 → 视频 / 声音 → 剪辑导出。
动作表和回执纪律见 [workflow-plan](../../references/workflow-plan.md)。

## 工厂内容

`collect_project_input`：读取简报、项目风格、总集数和单集目标，使用 `short-drama-factory`。
正文由强文本模型生成，完成医生精修、实际机检、返修、台账更新、系列机检和合规后，
通过 `ingest_short_drama_artifacts` 分批原样交付。用户已有完整工件时可在 Web 导入工件目录。
`create_episode`：在 Web 分集页创建工厂集账本，再交付该集工件。
历史模式项目先展示状态和历史数据，不改模式、不从旧 JSON 拼造工厂工件。

## 本集资产与 AI 分镜

`prepare_episode_assets`：在 Web 单集「资产」步骤按工厂工件准备角色、场景、道具和描述。
描述缺失时补齐对应资产，不回到原生规划。资产准备可以在视觉 JSON 尚未产生时执行。
`generate_asset_sheets`：调 `mcp__arcreel__generate_assets({"episode_id": N})`，按逐 ID 结果汇报。
`plan_short_drama_storyboards`：在 Web 本集「AI 分镜」执行全量或指定范围重分镜。
源稿变化后必须更新视觉派生状态，再继续生成。

## 提示词与参考素材

`author_prompts`：使用 `generate-script`，独立写 `fields=["image_prompt"]`。
`generate_shot_grids`：在 Web 分镜详情的参考素材区按镜生成、审阅和确认 K 帧。
每镜保存完整联系表和逐格关系，此步骤不能用一张独立分镜图代替。
`author_video_prompts`：K 帧确认后，用 `generate-script` 独立写 `fields=["video_prompt"]`。
`generate_storyboards` 只用于用户点名的工厂视觉图操作，走 `generate-storyboard`，不改变 K 帧依赖。

## 视频与剪辑

`generate_videos`：使用 `generate-video`，先读整批准入。
非 `admitted` 的批次没有入队；按逐 ID 问题处理，修好后原批重试，不拆批绕过。
默认补缺使用 `target={"scope":"episode","episode_id":N}`。
用户明确点名重做时使用 `target={"scope":"selected","ids":[...]}` 和 `force:true`；
不把计划空 `requested_ids` 传成工具的显式空 ID 数组。

`create_edit_timeline`：使用 `edit-video`。项目选择 TTS 且缺 narrator 配音时，
使用 `generate-narration-audio` 补齐；视频生成不等待 TTS。
随后调用 `mcp__arcreel__create_timeline({"from":"script","episode":N,"name":"完整版"})`。
时间线至少一条即完成制作步骤，按用户要求继续剪辑、预览、出片或导出剪映。

## 问题与恢复

`patch_episode_script`：只改回执指出的视觉派生字段。先 `get_episode_script` 取得 revision，
再一次 `patch_episode_script` 提交有序 operations；冲突后重新读取。剧情修改回到工厂返修。
`wait_for_task`：查询在途 batch 和任务，不重复生成。外部 Agent 按回执建议间隔轮询 `get_generation_batch`。
其他恢复动作按 [workflow-plan](../../references/workflow-plan.md) 处理。

分别陈述步骤进度、任务状态、供应商 checkpoint 与产物时效；任务成功不等于产物 current。
`stale` 产物仍可用，是否重做由用户决定。已提交供应商的中断任务先恢复或查询，避免重复计费。
完成动作后展示实际结果并刷新计划；已授权自动继续时直接推进，否则由用户选择下一创作动作。
