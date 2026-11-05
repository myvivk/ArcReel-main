---
name: generate-script
description: 为机械分镜产物的派生状态补齐或重写唯一动态分镜提示词 image_prompt；不生产剧本正文。
---

# 编写视觉提示词

先调用 `mcp__arcreel__get_workflow_plan({"episode_id": N})`，使用目标集当前脚本名和 operation 准入。
工厂源稿未入库或资产未准备时进入 Web 对应工厂步骤；尚无分镜结构时先调用
`mcp__arcreel__plan_short_drama_storyboards({"episode_id": N})`（或 Web「机械分镜」），再回来写提示词。
分镜结构由代码确定性落盘，编写提示词不增删、不重排、不改时长与台词。

## 工具调用

- 动态分镜提示词：`mcp__arcreel__generate_episode_script({"episode_id": N, "fields": ["image_prompt"]})`。
- 指定范围：增加 `entry_ids`；只有用户明确要求重写时再增加 `rewrite: true`。
- 仅查看 prompt：增加 `dry_run: true`，不入队、不收费。

`image_prompt` 是参考素材和视频生成共同消费的唯一提示词。每个镜头块必须带连续时间段，并包含
主体动态、空间调度、画面与光线、声音四层；提示词先于 K 帧生成，不得写 K0/K1 等编号。
不得请求、补写或重写 `video_prompt`。默认只补缺失提示词，已有提示词和其他视觉字段保持原样。

覆盖已有字段时工具返回 `prompt_overwrite_required` 和影响清单。向用户展示清单，按已有授权取得
同意后，将回执 revision 作为 `overwrite_revision` 重试；输入变化则重新读取，不复用旧令牌。

正文、台词、原稿锚、备注、尾帧和已生成媒体不由此步骤改写。用户要求改剧情时，回到工厂返修并入库，
随后重新机械分镜与提示词。Agent 不自行编写最终 JSON 或直接写正式脚本。

内嵌 Agent 等待工具终态；外部 Agent 使用 `get_generation_batch` 轮询回执句柄。
汇报实际成功、失败与阻断目标，按返回问题码处理，不将入队成功写成编写完成。
