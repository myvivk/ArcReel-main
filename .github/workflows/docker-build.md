# docker-build 工作流图形化

> 对应 `docker-build.yml`。GitHub 原生渲染 Mermaid，直接预览本文件即可看到节点图。

```mermaid
flowchart TD
    subgraph TRIG["触发 on"]
        T1["workflow_dispatch"]
        T2["push → master"]
        T3["pull_request → master"]
    end

    T1 --> F
    T2 --> F
    T3 --> F

    subgraph JOBF["Job: frontend"]
        direction TB
        F["① Checkout code<br/>actions/checkout@v4"] --> F2
        F2["② Set up pnpm + Node 24<br/>pnpm/action-setup@v4<br/>actions/setup-node@v4"] --> F3
        F3["③ pnpm install --frozen-lockfile"] --> F4
        F4["④ pnpm build"] --> F5
        F5["⑤ Upload artifact<br/>actions/upload-artifact@v4<br/>(frontend-dist)"]
    end

    F5 -.->|artifact| D2

    subgraph JOBD["Job: docker (needs: frontend)"]
        direction TB
        D1["① Checkout code<br/>actions/checkout@v4"] --> D2
        D2["② Download frontend artifact<br/>actions/download-artifact@v4"] --> D3
        D3["③ Set up Docker Buildx<br/>docker/setup-buildx-action@v4"] --> D4
        D4["④ Log in to Docker Hub<br/>docker/login-action@v4<br/><b>仅 push 事件</b>"] --> D5
        D5["⑤ Extract metadata<br/>docker/metadata-action@v6<br/>tags: latest + sha"] --> D6
        D6["⑥ apt refresh token<br/>date +%G-W%V"] --> D7
        D7["⑦ Build and push<br/>docker/build-push-action@v6<br/>cache: gha"]
    end

    F --> JOBF
    JOBF --> JOBD

    D4 -. "push=false (PR)" .-> Skip["跳过登录，仅构建验证"]
    D7 -. "push=false (PR)" .-> N["仅构建，不推送 ✓"]
    D7 -. "push=true (合并到 master)" .-> P["推送镜像至 Docker Hub<br/>arcreel/arcreel:latest<br/>arcreel/arcreel:sha-xxxxxxx"]

    style JOBF fill:#1f6feb22,stroke:#1f6feb
    style JOBD fill:#1f6feb22,stroke:#1f6feb
    style P fill:#2da44e,color:#fff
    style N fill:#8957e5,color:#fff
    style Skip fill:#6e7681,color:#fff
```

## 节点说明

### Job: `frontend`（前端预构建）

| 节点 | Action | 触发条件 |
|------|--------|----------|
| ① Checkout | `actions/checkout@v4` | 总是 |
| ② Set up pnpm + Node 24 | `pnpm/action-setup@v4` + `actions/setup-node@v4` | 总是 |
| ③ pnpm install | `pnpm install --frozen-lockfile` | 总是 |
| ④ pnpm build | `pnpm build` | 总是 |
| ⑤ Upload artifact | `actions/upload-artifact@v4` | 总是（产物 `frontend-dist`）|

### Job: `docker`（needs: `frontend`）

| 节点 | Action | 触发条件 |
|------|--------|----------|
| ① Checkout | `actions/checkout@v4` | 总是 |
| ② Download artifact | `actions/download-artifact@v4` | 总是（取 `frontend-dist` → `frontend/dist`）|
| ③ Buildx | `docker/setup-buildx-action@v4` | 总是 |
| ④ Login | `docker/login-action@v4` | **仅 push 到 master** |
| ⑤ Metadata | `docker/metadata-action@v6` | 总是（产出 `latest` + `sha` 标签）|
| ⑥ apt token | `date +%G-W%V` | 总是（按周刷新系统包缓存层）|
| ⑦ Build/Push | `docker/build-push-action@v6` | 构建总是；**推送仅 push 到 master** |

**关键分支**：`pull_request` 触发时 ④ 跳过、⑦ 只构建不推送；`push` 到 `master` 时才登录并推送镜像到 Docker Hub。`docker` job 依赖 `frontend` 产出，必须等其完成。
