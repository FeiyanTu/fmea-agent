# ============================================================
# FMEA Agent — Docker 生产镜像（含全部数据）
# 
# 构建:   docker build -t fmea-agent:latest .
# 自定义 pip 源:  --build-arg PIP_INDEX_URL=https://pypi.org/simple
# 自定义 apt 源:  --build-arg DEBIAN_MIRROR=deb.debian.org
# 构建+导出:     docker build -t fmea-agent:latest && docker save fmea-agent:latest | gzip > fmea-agent.tar.gz
# 运行:          docker run -d --name fmea-agent -p 7777:7777 fmea-agent:latest
# ============================================================

# ─── 构建参数 ───
# 国内服务器: 默认清华镜像（pip + apt 双加速）
# 海外服务器: --build-arg PIP_INDEX_URL=https://pypi.org/simple --build-arg DEBIAN_MIRROR=deb.debian.org
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG DEBIAN_MIRROR=mirrors.tuna.tsinghua.edu.cn

# ─── 第一阶段：安装 Python 依赖 ───
FROM python:3.12-slim-bookworm AS builder

ARG PIP_INDEX_URL

WORKDIR /build

# 复制依赖声明
COPY fmea-agent/requirements.txt /build/requirements.txt

# 安装 Python 包（过滤掉 agno，本地框架在后面安装）
RUN grep -v "^agno" requirements.txt > req-no-agno.txt && \
    pip install --no-cache-dir --default-timeout=120 \
        -i "$PIP_INDEX_URL" \
        -r req-no-agno.txt

# ─── 第二阶段：运行镜像（最小化）───
FROM python:3.12-slim-bookworm AS runtime

ARG DEBIAN_MIRROR

# 替换为指定镜像源，加速 apt 下载
RUN sed -i "s|deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    sed -i "s|deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list 2>/dev/null; \
    apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# 从 builder 层复制已安装的 Python 包（省掉运行时 pip 安装）
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# 创建非 root 用户
RUN groupadd -r nonroot && useradd -r -g nonroot -d /app -s /sbin/nologin nonroot

WORKDIR /app

# ── 复制 agno 框架（本地依赖） ──
COPY agno/libs/agno /app/agno
# 只注册不装依赖（全部依赖已在 builder 阶段预装到 site-packages 了）
RUN cd /app/agno && pip install --no-cache-dir -e . --no-deps

# ── 复制 fmea-agent 全部代码 + 数据 ──
# .dockerignore 会过滤掉 .git / __pycache__ / .venv 等无用文件
COPY fmea-agent /app/fmea-agent

# 创建日志目录（容器启动后运行时写入）
RUN mkdir -p /app/fmea-agent/logs && \
    chown -R nonroot:nonroot /app

USER nonroot

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:7777/health || exit 1

WORKDIR /app/fmea-agent
EXPOSE 7777

# 环境变量
ENV AGNO_PATH=/app/agno
ENV FMEA_HOST=0.0.0.0
ENV FMEA_PORT=7777
ENV FMEA_DB_FILE=tmp/fmea_agent.db
ENV FMEA_DATA_DIR=/app/fmea-agent/data/projects

CMD ["python", "main.py"]
