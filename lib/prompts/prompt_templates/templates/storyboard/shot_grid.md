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
你是一位专业的分镜画师。请把下面这一个分镜的关键帧序列画成一张宫格图，每一格对应一个镜头。

【布局要求】
- 恰好 {{ rows }} 行 {{ cols }} 列，共 {{ cell_count }} 个画格，阅读顺序：从左到右，从上到下
- 整体图片比例：{{ grid_aspect_ratio }}
- 每个画格比例：{{ panel_aspect_ratio }}，所有画格大小完全相同
- 画格之间无边框、无间隙、无留白，紧密排列
- 不得合并画格、不得遗漏画格、不得错位排列、不得增删或调换镜头

【风格与锚点】
- 所有画格是同一个分镜的连续关键帧：同一套角色外观、服装、道具、场景、光线方向与时段，跨格完全一致
{% if scene_opening %}- 画面基调：{{ scene_opening }}
{% endif %}
【各格内容】
{{ partial("storyboard/shot_grid/lists/cells") }}

{{ partial("shared/media_style") }}

{{ partial("storyboard/shot_grid/avoid") }}
