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

固定流程：工厂质量闭环与七工件入库 → 本集资产 → 机械分镜（结构）→ 分镜提示词编写 → 每镜 K 帧确认
→ 视频 / 声音 → 剪辑导出。
动作表和回执纪律见 [workflow-plan](../../references/workflow-plan.md)。

## 工厂内容

`collect_project_input`：读取简报、项目风格、总集数和单集目标，使用 `short-drama-factory`。
正文由强文本模型生成，完成医生精修、实际机检、返修、台账更新、系列机检和合规后，
通过 `ingest_short_drama_artifacts` 分批原样交付。用户已有完整工件时可在 Web 导入工件目录。
`create_episode`：在 Web 分集页创建工厂集账本，再交付该集工件。
历史模式项目先展示状态和历史数据，不改模式、不从旧 JSON 拼造工厂工件。

## 本集资产与机械分镜

`prepare_episode_assets` 是同一会话内的复合步骤：Agent 读取工厂源稿/人物圣经，用 `patch_project` 登记本集角色、场景、道具并写出完整、可辨识的描述；资产准备可以在视觉 JSON 尚未产生时执行。登记后汇总待生成图片 ID 与数量，并用 `AskUserQuestion` 暂停会话等待用户明确确认；不能只发普通文本“请确认”后结束回合，也不得将确认前工作派给子智能体。确认前不入队。得到确认后在当前会话调用 `mcp__arcreel__generate_assets({"episode_id": N})`，等待批次终态并按逐 ID 结果处理失败。全部确认的本批图片完成后刷新计划、汇报并结束会话。等待确认时保持同一 session_id，用户确认后继续，不要结束或新建会话。
刷新后的 `generate_asset_sheets` 独立动作也使用同一确认、等待和完成条件；不得因计划 `requires_confirmation: false` 自动生成。
`plan_short_drama_storyboards`：调 `mcp__arcreel__plan_short_drama_storyboards({"episode_id": N})`。
服务端按代码确定性切分结构，不调用文本模型；新分镜默认全量，部分重切传 `regenerate_scene_ids`，全量重切传 `force_regenerate=true`。
已有分镜图/视频时先问用户，同意后传 `confirm_artifacts=true`。集页入口改为「交给 Agent」。
结构落盘后 `image_prompt` 为空、条目待编写；下一步 `author_prompts` 用 `generate_episode_script` 写提示词。Agent 不自行改分镜。
源稿变化后必须重新机械分镜，再继续生成。

## 提示词与参考素材

`author_prompts`：使用 `generate-script`，独立写 `fields=["image_prompt"]`；输入是上一步落盘的分镜结构，不增删、不重排分镜。
`generate_shot_grids`：先用 `configure_shot_grid` 写这一镜的规格（2×2 / 2×3 / 3×3）与逐格镜头文本，
需要时先调 `author_shot_grid_prompt` 写格；再调 `mcp__arcreel__generate_shot_grids` 出图（省略 `shot_ids`
只补缺，点名不传空数组）。
每镜保存完整联系表和逐格关系，此步骤不能用一张独立分镜图代替。
K 帧是否可用由用户在分镜详情逐镜确认；工具不代替确认，已确认的宫格也不能走批量重做。
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
## 回合边界

通常每轮只执行一个 `next_action`；资产准备是上述复合动作例外，必须在同一会话内完成用户确认后的图片生成与终态核对。其他动作完成后按顺序：刷新计划一次 → 展示实际结果与下一步 → 结束本轮。
结束本轮后不再调用任何工具，也不开始下一个动作。用户说「继续 / 下一步」时，只执行下一个动作，
完成后再次结束本轮；不得把「继续」理解为「把剩下的都做完」。
`next_action` 和刷新后的计划只是建议，不构成自动继续的授权；Agent 不能自行认定「已授权自动继续」。
Web 步骤只导航；付费生成先报 ID 与数量并等用户确认。
