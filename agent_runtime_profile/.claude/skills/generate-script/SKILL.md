---
name: generate-script
description: 为工厂 AI 分镜的派生状态编写视觉提示词。用于补齐或重写 image_prompt，或在参考素材确认后单独编写 video_prompt；不生产剧本正文。
---

# 编写视觉提示词

先调用 `mcp__arcreel__get_workflow_plan({"episode_id": N})`，使用目标集当前脚本名和 operation 准入。
工厂源稿未入库或资产未准备时进入 Web 对应工厂步骤；尚无 AI 分镜时先调用
`mcp__arcreel__plan_short_drama_storyboards({"episode_id": N})`（或 Web「AI 分镜」），再回来写提示词。

## 工具调用

- 分镜提示词：`mcp__arcreel__generate_episode_script({"episode_id": N, "fields": ["image_prompt"]})`。
- 视频提示词：先生成、审阅和确认该镜 K 帧，再调用
  `mcp__arcreel__generate_episode_script({"episode_id": N, "fields": ["video_prompt"]})`。
- 指定范围：增加 `entry_ids`；只有用户明确要求重写时再增加 `rewrite: true`。
- 仅查看 prompt：增加 `dry_run: true`，不入队、不收费。

不得在一个请求中同时传两类字段。默认只补缺失提示词，已有提示词和其他视觉字段保持原样。
视频提示词依赖已确认 K 帧，尚未确认的分镜不进入本批编写。

覆盖已有字段时工具返回 `prompt_overwrite_required` 和影响清单。向用户展示清单，按已有授权取得
同意后，将回执 revision 作为 `overwrite_revision` 重试；输入变化则重新读取，不复用旧令牌。

正文、台词、原稿锚、备注、尾帧和已生成媒体不由此步骤改写。用户要求改剧情时，回到工厂返修并入库，
随后重新 AI 分镜。Agent 不自行编写最终 JSON 或直接写正式脚本。

内嵌 Agent 等待工具终态；外部 Agent 使用 `get_generation_batch` 轮询回执句柄。
汇报实际成功、失败与阻断目标，按返回问题码处理，不将入队成功写成编写完成。
