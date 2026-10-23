---
name: generate-video
description: 为工厂 AI 分镜生成或重做视频，支持整集、单镜和指定范围。先确认 K 帧参考素材并独立编写视频提示词。
---

# 生成工厂视频

先读 `mcp__arcreel__get_workflow_plan`。目标必须有规范工厂源稿、来源一致的 AI 分镜、
已确认 K 帧和独立编写的视频提示词；缺口按服务端准入返回的动作处理。
媒体生成通过 MCP 入队，不运行自写 Python / Shell 生成脚本。

## 工具调用

| 操作 | 调用 |
| --- | --- |
| 整集补缺 | `mcp__arcreel__generate_videos({"script":"episode_1.json","target":{"scope":"episode","episode_id":1}})` |
| 指定范围补缺 | `mcp__arcreel__generate_videos({"script":"episode_1.json","target":{"scope":"selected","ids":["E1S01","E1S05"]}})` |
| 用户点名重做 | 同上，加 `force:true`；单镜可使用 scene scope。 |
| 费用预检 | 原请求加 `preview:true`，不建任务。 |

脚本名、集 ID、分镜 ID 从当前计划取。`target.episode_id` 是内部集 ID，不是播出位置。
默认只补缺失成片，stale 成片仍可复用；用户明确要求重做时才使用 force。
已有在途任务先查询 batch，不再次提交刚完成的目标。

## 整批准入

只有 `admission.decision=admitted` 才整批入队；`blocked` 或 `confirmation_required`
一个任务也没建。逐目标说明问题码、参数和下一步动作，健康目标被同批扣下时如实说明原因。
修复后原批重试，不拆批绕过。需要确认时长时，展示逐档位时长和费用，按回执
`confirmed_request_durations` 重发原目标集合；确认不修改项目偏好。

输入由服务端冻结：K 帧完整图片关系、前镜接续点、本镜末格或尾帧快照、资产链接、
视频提示词、角色声音与参考音频。时长和输入数量服从实际模型能力，不另写一套数值。
无模型能力或源稿修订不一致时先修复，不降级到旧生产流程。

## 结果与恢复

内嵌 Agent 等待工具终态；外部 Agent 使用 `get_generation_batch` 轮询回执句柄。
按 `requested / succeeded / failed / blocked / skipped` 汇报实际结果。
任务状态、供应商 checkpoint、产物时效与工作流步骤分别陈述。
`provider_checkpoint.submitted=true` 表示可能已计费，中断后按回执恢复或下载，不重新提交。

以 `generated_assets.video_clip` 为现行成片归属，输出在 `videos/scene_{id}.mp4`。
需要改用已有版本时调用 `mcp__arcreel__select_video_version({"unit_id":"E1S01","version":2})`，
不收费；版本不存在时按回执的 available_versions 重选。历史产物和版本不自动删除。

## 声音与剪辑

视频声音由规范 utterances、项目声音绑定和模型能力决定，参考音频仍可用。
旁白 TTS 独立于视频生成，按 `generate-narration-audio` 补齐 narrator 单元；未配置 TTS 仍可做视频。
生成后按用户要求进入 `edit-video`，剪辑、BGM、字幕、预览、成片与剪映导出继续可用。
