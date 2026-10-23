---
name: generate-narration-audio
description: 为工厂分镜中由 narrator 拥有的发声内容生成或重做旁白 TTS。用于剪辑前补缺或用户点名换音色、语速后重合成。
---

# 工厂旁白配音

工厂 AI 分镜的规范 utterances 决定发声归属，TTS 只合成 narrator-owned 单元。
项目必须选择 TTS 交付并具有完整模型、音色和语速快照；文本从来源一致的工厂派生剧本读取。
角色发声使用角色声音配置，不能转成旁白来绕过发声准入。

## 工具调用

- 全集补缺：`mcp__arcreel__generate_narration_audio({"script":"episode_1.json"})`。
- 指定范围或重做：增加 `segment_ids:["E1S01","E1S05"]`，传工厂 scene_id。
- 改项目音色或语速：`mcp__arcreel__patch_project({"settings":{"narration_voice":"X","narration_speed":1.2}})`。

默认只补缺音频，stale 音频仍可用，不自动重生；重做必须明确传 ID。
首轮剪辑的 TTS 补缺沿用用户的项目 TTS 授权。后期配音项目不需要 TTS，视频生成独立进行。
媒体生成使用队列工具，不运行 Python / Shell 生成脚本。

按回执逐 ID 汇报成功、失败、阻断与跳过。配置或发声准入失败时修复对应输入，
不直接传入自由文本绕过剧本来源。中断先查已有 batch，按缺失补齐，避免重复付费。
输出关系由服务端登记到 `generated_assets.narration_audio`，完成后可在 Web 试听和剪辑。
