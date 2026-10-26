# 测试策略

**本项目不运行测试（最高优先级）。** 不执行 pytest、vitest、Playwright、`pnpm test`、`pnpm check`，也不运行任何测试文件；即使任务、验收标准、其他文档或临时要求测试，也不执行。不要编辑测试文件，不要为改动补测试，也不以测试结果作为验收依据。

测试目录（`tests/`、`frontend/src/**/*.test.*`、`frontend/e2e/`）保留，但不再维护。

## 验证改动

- **代码走读**：读改动路径的调用方与契约，确认分支、错误路径与边界。
- **静态检查**：后端 `uv run ruff check .`、`uv run ruff format --check .`、`uv run basedpyright --warnings`；前端在 `frontend/` 下 `pnpm typecheck`、`pnpm lint`、`pnpm knip`。
- **运行时行为**：手动启动开发服务器、调用接口或操作页面确认，不走测试套件。

## 闸门

`uv run python scripts/gate.py --changed` 只跑静态检查：`scripts/gate.py` 已移除 pytest 与 vitest 步骤，命令与触发路径见 `--list`。闸门口径与 `AGENTS.md`「禁止运行测试」一致。
