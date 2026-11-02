# RunningHub 接入 · UI 预览

这是《8.1 RunningHub 接入 UI 设计》的静态可交互原型，不是真实应用；数据均为示例。

## 打开方式

1. 本地服务（推荐）：<http://127.0.0.1:8770/>
2. 直接打开文件：`index.html`

服务由仓库根目录执行：

```bash
# 在 ArcReel 仓库根目录执行
python -m http.server 8770 --bind 127.0.0.1 --directory mockups/runninghub
```

## 预览视图

顶部预览栏可切换 7 个视图，右侧「错误态」开关可查看失败样式。

| 视图 | URL 参数 | 内容 |
| --- | --- | --- |
| 供应商详情 | `?view=provider` | RunningHub 供应商：连接、工作流端点、模型、并发；含缺失 workflowId 的告警 |
| 新建供应商 | `?view=new-provider` | 接入方式卡片、连接字段、工作流端点空态、模型空态 |
| 拉取工作流 | `?view=pull` | 新建端点页 + “从 RunningHub 拉取”弹窗 |
| 端点详情 | `?view=endpoint` | 远端工作流、节点绑定、定义、workflow 本体、端点测试 |
| 模型行 | `?view=model` | 展开模型行、端点下拉（RunningHub / 本地 ComfyUI 分组、缺 ID 禁用） |
| 测试结果 | `?view=test` | 端点测试成功时间线、taskId、费用、产物 |
| 测试失败 | `?view=test&state=error` | 节点失败、重新拉取并绑定入口 |
| 使用位置 | `?view=activate` | 默认模型页：全局默认 + 项目级覆盖说明 + 生效顺序 |

## 可交互点

- 供应商详情「测试连接」：模拟账户检查，显示 RH 币余额；切到错误态后显示 401。
- 「从 RunningHub 拉取」：填写 workflow ID → 拉取中 → 成功预览 → 继续确认节点绑定（跳到端点详情）。
- 端点详情「从 RunningHub 重新拉取」：二次确认 → 拉取中 → toast。
- 端点测试「测试连接」：提交 → 排队 → 运行 → 成功/失败；成功显示 taskId、耗时、RH 币、产物。
- 「模型行」：展开/收起、端点下拉、选择本地 ComfyUI 端点、缺 workflowId 提示。
- 顶部「错误态」：切换连接失败与任务失败样式。

## 设计说明

- 详细线框、文案、状态与验收标准：`../../方案设计/8.1 RunningHub 接入 UI 设计.md`
- 后端接入方案：`../../方案设计/8. RunningHub 供应商接入方案.md`

## 截图

- `shot-provider.png`：供应商详情（1440×1000）
- `shot-new-provider.png`：新建供应商
- `shot-pull.png`：拉取工作流弹窗
- `shot-endpoint.png`：端点详情
- `shot-model.png`：模型行与端点选择
- `shot-test.png` / `shot-test-error.png`：测试成功 / 失败
- `shot-activate.png`：使用位置（默认模型 / 项目覆盖）
