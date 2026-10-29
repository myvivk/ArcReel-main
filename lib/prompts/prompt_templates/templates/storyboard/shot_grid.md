---
id: storyboard/shot_grid
category: storyboard
title: 分镜参考素材宫格
description: 单个分镜的逐镜头关键帧宫格图。布局、风格锚点与纯净性约束由系统拼装，逐格文本来自弹窗编辑的宫格镜头提示词或 scene 现解析。
stage: shot_grid
invoked_by:
  kind: generation_task
  name: shot_grid
applies_to: {}
slots:
  reference_images: 参考图类型声明，按随请求发出的参考图序位编号为「图N」；无参考图为空
  rows: 宫格行数
  cols: 宫格列数
  cell_count: 画格总数
  grid_aspect_ratio: 整张宫格图的比例
  panel_aspect_ratio: 单个画格的比例，由整图比例与行列数算出
  scene_opening: 分镜提示词 scene 的导语段，跨镜头的环境 / 光线 / 氛围基线；无导语为空
  cells: 逐格内容（number、shot_size、text）
  style: 项目画风
  style_description: 项目风格描述
protected: false
---
{% if reference_images %}
Reference_Images: {{ reference_images }}

{% endif %}
你是一位专业的分镜画师。请把下面这一个分镜的关键瞬间画成一张宫格图：每一格是一个关键瞬间，不是一条镜头。

【布局要求】
- 恰好 {{ rows }} 行 {{ cols }} 列，共 {{ cell_count }} 个画格，阅读顺序：从左到右，从上到下
- 整体图片比例：{{ grid_aspect_ratio }}
- 每个画格比例：{{ panel_aspect_ratio }}，所有画格大小完全相同
- 画格之间无边框、无间隙、无留白，紧密排列
- 不得合并画格、不得遗漏画格、不得错位排列

【帧链节奏】
- 第 1 格是这个分镜的**开始画面**，最后一格是**结束画面**
- 中间各格是过程关键帧：前一格的结束 = 后一格的开始
- 相邻格之间必须看得出动作、姿态与视线的延续；断裂处（闪回、时间跳）只在文本明确写出时画成硬切
- 每一格是冻结的单一瞬间：不画镜头运动、先后动作或渐变过程；这些由视频阶段承担
- 格与格之间确实是剪接（换景别、换机位、正反打）时，画成明显的断点，不要把两格硬接成连续运镜

【风格与锚点】
- 所有画格来自同一个分镜；同一时空内保持同一套角色外观、服装、道具、场景、光线方向与时段，跨格一致；明确的剪接点除外
{% if scene_opening %}- 画面基调：{{ scene_opening }}
{% endif %}
【各格内容】
{{ partial("storyboard/shot_grid/lists/cells") }}

{{ partial("shared/media_style") }}

{{ partial("storyboard/shot_grid/avoid") }}
