# docker-build 工作流图形化

> 对应 `docker-build.yml`。GitHub 原生渲染 Mermaid，直接预览本文件即可看到节点图。

```mermaid
flowchart TD
    subgraph TRIG["触发 on"]
        T1["workflow_dispatch"]
        T2["push → master"]
        T3["pull_request → master"]
    end

    T1 --> J
    T2 --> J
    T3 --> J

    J["Job: docker<br/>runs-on: ubuntu-latest<br/>permissions: contents: read"] --> S1

    S1["① Checkout code<br/>actions/checkout@v4"] --> S2
    S2["② Set up Docker Buildx<br/>docker/setup-buildx-action@v4"] --> S3
    S3["③ Log in to Docker Hub<br/>docker/login-action@v4<br/><b>仅 push 事件执行</b>"] --> S4
    S4["④ Extract image metadata<br/>docker/metadata-action@v6<br/>tags: latest + sha 短哈希"] --> S5
    S5["⑤ Compute weekly apt refresh token<br/>date +%G-W%V → 例如 2026-W41"] --> S6

    S6["⑥ Build and push<br/>docker/build-push-action@v6<br/>cache-from/to: type=gha"]

    S3 -. "push=false (PR)" .-> Skip["跳过登录，仅构建验证"]
    S6 -. "push=false (PR)" .-> N["仅构建，不推送 ✓"]
    S6 -. "push=true (合并到 master)" .-> P["推送镜像至 Docker Hub<br/>arcreel/arcreel:latest<br/>arcreel/arcreel:sha-xxxxxxx"]

    style J fill:#1f6feb,color:#fff
    style S1 fill:#21262d,color:#fff
    style S2 fill:#21262d,color:#fff
    style S3 fill:#21262d,color:#fff
    style S4 fill:#21262d,color:#fff
    style S5 fill:#21262d,color:#fff
    style S6 fill:#21262d,color:#fff
    style P fill:#2da44e,color:#fff
    style N fill:#8957e5,color:#fff
    style Skip fill:#6e7681,color:#fff
```

## 节点说明

| 节点 | Action | 触发条件 |
|------|--------|----------|
| ① Checkout | `actions/checkout@v4` | 总是 |
| ② Buildx | `docker/setup-buildx-action@v4` | 总是 |
| ③ Login | `docker/login-action@v4` | **仅 push 到 master** |
| ④ Metadata | `docker/metadata-action@v6` | 总是（产出 `latest` + `sha` 标签）|
| ⑤ apt token | `date +%G-W%V` | 总是（按周刷新系统包缓存层）|
| ⑥ Build/Push | `docker/build-push-action@v6` | 构建总是；**推送仅 push 到 master** |

**关键分支**：`pull_request` 触发时 ③ 跳过、⑥ 只构建不推送；`push` 到 `master` 时才登录并推送镜像到 Docker Hub。
