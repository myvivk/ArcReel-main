# ============================================================
# 生产镜像
# 前端构建产物（frontend/dist）由 CI 的 frontend job 预构建并作为构建上下文传入，
# 不再在镜像内执行 node/pnpm 构建，缩短镜像构建时间。
# ============================================================
FROM python:3.12-slim AS production

# 安装系统依赖，并把基础镜像预装的系统包升级到当前仓库版本。
# APT_REFRESH 由 CI 按 ISO 周传入，值变化时本层缓存失效，保证系统包定期刷新
ARG APT_REFRESH=unset
RUN echo "apt refresh: ${APT_REFRESH}" \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    curl \
    bubblewrap \
    socat \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# 升级基础镜像预装的 pip：依赖全部由 uv 安装、运行时不调用 pip，
# 但 python:3.12-slim 自带的旧 pip 会被镜像扫描器报 CVE，升级以清除这些告警
RUN python -m pip install --no-cache-dir --upgrade pip

# 安装 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# 禁用 Python 输出缓冲，确保日志实时输出到 Docker logs
ENV PYTHONUNBUFFERED=1

# 默认时区，可由 docker-compose / 运行时 -e TZ=... 覆盖
ENV TZ=Asia/Shanghai

# 先复制依赖和包元数据文件，利用缓存；workspace 子包作为依赖随这一步安装，源码须先就位
COPY pyproject.toml uv.lock README.md ./
COPY packages/ packages/
RUN uv sync --no-dev --no-install-project

# 复制应用代码
COPY lib/ lib/
COPY server/ server/
COPY alembic/ alembic/
COPY alembic.ini ./
COPY scripts/ scripts/
COPY agent_runtime_profile/ agent_runtime_profile/
COPY public/ public/
# 随包 ffmpeg 的许可证声明（见 NOTICE）随镜像分发
COPY LICENSE NOTICE ./
COPY LICENSES/ LICENSES/

# 复制前端构建产物（由 CI frontend job 预构建后放入构建上下文）
COPY frontend/dist/ frontend/dist/

# 暴露端口
EXPOSE 1241

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:1241/health || exit 1

# 启动命令
CMD ["uv", "run", "uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "1241"]
