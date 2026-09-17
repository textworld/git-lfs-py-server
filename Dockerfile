# syntax=docker/dockerfile:1.7
# ---------- Stage 1: builder ----------
# 用 uv 安装依赖到 .venv（比 pip 快 10-100x），再把 .venv 拷到运行时镜像
FROM python:3.12-slim AS builder

WORKDIR /app

# 安装 uv（官方镜像，约 30MB）
COPY --from=ghcr.io/astral-sh/uv:0.7.12 /uv /uvx /usr/local/bin/

# uv 行为调优（构建期）
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# 1. 先只复制依赖清单，构建出独立的依赖层（利用 Docker 缓存）
#    改业务代码不会重装依赖
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# 2. 再复制源码，安装项目本身
COPY app ./app
COPY run.py ./
RUN uv sync --frozen --no-dev

# ---------- Stage 2: runtime ----------
# 极简运行时镜像：只有 python slim + .venv + 业务代码
FROM python:3.12-slim AS runtime

# 安全：不要以 root 运行
RUN groupadd -r app && useradd -r -g app app

# 时区（容器默认 UTC，logs 时间可读性差）
ENV TZ=UTC \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/app/.venv/bin:$PATH" \
    METADATA_DB_PATH=/data/lfs_metadata.db

WORKDIR /app

# 从 builder 拷贝虚拟环境和应用代码
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/app /app/app
COPY --from=builder --chown=app:app /app/run.py /app/run.py

# 数据卷：存放 SQLite 元数据
RUN mkdir -p /data && chown -R app:app /data
VOLUME ["/data"]
USER app

EXPOSE 8000

# 健康检查：依赖 docker 的 HEALTHCHECK（被 compose / k8s 也会用）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)" || exit 1

CMD ["python", "run.py"]
