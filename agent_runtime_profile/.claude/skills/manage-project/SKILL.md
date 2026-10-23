---
name: manage-project
description: 管理工厂项目的角色、场景、道具和设置，级联改名或合并资产，并查询视频模型能力。
user-invocable: false
---

# 工厂项目管理

正式项目文件通过 MCP 工具修改，不用 Write / Edit / Bash 直改。
剧情和系列设定以工厂工件为准，资产描述的改写需要用户意图，不能静默覆盖人工描述。

## 资产与设置

`patch_project` 接受两种调用形态，每次只用一种：

- 资产：`{"table":"characters","entries":{"角色名":{"description":"...","voice_style":"..."}}}`。
  表仅为 characters / scenes / props；按 name upsert，新名新增、同名合并字段。
- 设置：`{"settings":{"episode_target_duration":90}}`。
  可配置单集目标时长、narration_voice、narration_speed 和 character_voice_binding，具体边界服从请求 schema。

角色衍生用 `entries.{角色名}.derivatives` 登记，仅写相对本体变化的 description。
视觉引用写 `@[角色/衍生]`，声音始终绑定本体。
资产图、原图和媒体路径由生成或上传服务维护，不通过 upsert 伪造。

`character_voice_binding="prompt"` 使用角色 voice_style 软约束；`"reference_audio"`
请求模型支持原生声音输入且角色已有参考音频。TTS 音色和语速取项目快照，仅影响该项目。
项目路线固定，既有历史数据模式保持原样。

## 改名与合并

改资产名用 `mcp__arcreel__rename_asset`，一次更新资产 key、各集引用和历史文件关系。
使用 upsert 写新名会新增资产，不能用于改名。

合并同一身份用 `mcp__arcreel__merge_asset`，先确定 source / target。
以 `dry_run:true` 取得引用改写和媒体影响清单，展示需要保留的描述、声音、原图和版本关系，
按用户授权再执行。同一角色的另一套外观可用 `as_derivative:true`，其他类型不适用。

## 模型能力

调用 `mcp__arcreel__get_video_capabilities({})`；能力查询全项目同一口径，无需 episode_id。
以返回的模型、supported_durations、duration_constraints、输入上限和音频能力为准。
工厂正文与 AI 分镜的初始编排时长不依赖供应商查找，视频提交才验证实际模型请求能力。
无法解析能力时按 problem 修复配置，不编造档位，不更换创作链路。
