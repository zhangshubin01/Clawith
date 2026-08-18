# ─── Backend Dockerfile (Optimized) ─────────────────────
# Multi-stage: deps → production
# 基础镜像走国内加速（docker.m.daocloud.io），加速器不可用时回退 docker.io
FROM docker.m.daocloud.io/library/python:3.12-slim AS deps

WORKDIR /app
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's/deb.debian.org/mirrors.ustc.edu.cn/g' /etc/apt/sources.list.d/debian.sources; \
    else \
        sed -i 's/deb.debian.org/mirrors.ustc.edu.cn/g' /etc/apt/sources.list; \
    fi && \
    apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 shared-mime-info pango1.0-tools && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
ARG CLAWITH_PIP_INDEX_URL
ARG CLAWITH_PIP_TRUSTED_HOST
RUN if [ -n "$CLAWITH_PIP_INDEX_URL" ] && [ -n "$CLAWITH_PIP_TRUSTED_HOST" ]; then \
        pip install --no-cache-dir --index-url "$CLAWITH_PIP_INDEX_URL" --trusted-host "$CLAWITH_PIP_TRUSTED_HOST" . uv; \
    elif [ -n "$CLAWITH_PIP_INDEX_URL" ]; then \
        pip install --no-cache-dir --index-url "$CLAWITH_PIP_INDEX_URL" . uv; \
    else \
        pip install --no-cache-dir . uv; \
    fi
# lark-channel-sdk 仅在公共 PyPI 发布，清华镜像未同步
RUN pip install --no-cache-dir lark-channel-sdk>=1.2.0

# ─── Production ─────────────────────────────────────────
FROM docker.m.daocloud.io/library/python:3.12-slim AS production

WORKDIR /app
# D9 加固：/app 对运行用户只读，禁止运行期写回 __pycache__（.pyc 写失败会产生噪音并拖慢冷启动）
ENV PYTHONDONTWRITEBYTECODE=1
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's/deb.debian.org/mirrors.ustc.edu.cn/g' /etc/apt/sources.list.d/debian.sources; \
    else \
        sed -i 's/deb.debian.org/mirrors.ustc.edu.cn/g' /etc/apt/sources.list; \
    fi && \
    apt-get update && \
    apt-get install -y --no-install-recommends libpq5 curl shadowsocks-libev gosu bubblewrap libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 shared-mime-info pango1.0-tools fonts-wqy-microhei fonts-wqy-zenhei fonts-noto-cjk chromium git && \
    fc-cache -fv && \
    rm -rf /var/lib/apt/lists/*

# Copy installed packages from deps stage
COPY --from=deps /usr/local/lib/python3.12/site-packages/ /usr/local/lib/python3.12/site-packages/
COPY --from=deps /usr/local/bin/ /usr/local/bin/

# Copy application code
COPY . .

# 兜底清理构建上下文带入的宿主编译产物：.dockerignore 排除模式在 docker CLI 29 + buildkitd
# v0.29.0/v0.32.2（含 OrbStack 捆绑版）的会话传输链路实测不被应用（上游问题，断点未定位，
# 见 docs/technical-plans/20260816-p0-security-fix-plan.md P1），__pycache__/*.pyc、测试 venv、
# 运行时数据目录会被 COPY 进镜像。这里显式删除，保证交付镜像干净、/app 仅含平台源码。
RUN rm -rf /app/.venv /app/.venv-test /app/agent_data /app/data /app/.pytest_cache /app/.debug /app/*.egg-info /app/.env* && \
    find /app -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; \
    find /app -name "*.pyc" -delete 2>/dev/null; \
    true

# D9 加固：/app 平台代码保持 root 属主交付（COPY 默认 root，运行用户 clawith 只读可执行）。
# 运行期写路径仅保留 /data（agent 数据、venv、uv 缓存）与 /home/clawith（git 全局配置）。
# 这样路径穿越/沙箱逃逸拿到 clawith 权限也无法覆盖平台自身代码（见 docs/technical-plans/20260816-p0-security-fix-plan.md D9）。
RUN useradd --create-home clawith && \
    mkdir -p /data/agents && \
    chmod +x /app/entrypoint.sh && \
    chmod u+s /usr/bin/bwrap && \
    chown -R clawith:clawith /data

RUN su - clawith -c "git config --global user.name 'Clawith Agent' && git config --global user.email 'agent@clawith.local' && git config --global safe.directory /data/agents"

# Note: USER is removed to allow entrypoint.sh to fix permissions of mounted volumes
# at runtime. The entrypoint script will drop privileges to 'clawith' after fixing permissions.

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

EXPOSE 8000
# entrypoint.sh bootstraps Alembic and LangGraph checkpoints before `uvicorn`
ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
