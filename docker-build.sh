#!/usr/bin/env bash
# ============================================================
# FMEA Agent — Docker 镜像构建 & 运行 & 导出脚本
#
# 场景：在一台服务器上构建，导出镜像到离线服务器运行
#
# 用法:
#   ./docker-build.sh              # 构建 + 启动
#   ./docker-build.sh --no-cache   # 重新构建（不使用缓存）
#   ./docker-build.sh --export     # 仅构建 + 导出为 tar.gz，不启动
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

NO_CACHE=""
MODE="run"

for arg in "$@"; do
  case "$arg" in
    --no-cache) NO_CACHE="--no-cache" ;;
    --export)   MODE="export" ;;
    --help)
      echo "用法: $0 [--no-cache] [--export]"
      echo "  --no-cache   重新构建（不使用 Docker 缓存）"
      echo "  --export     仅构建 + 导出为 fmea-agent.tar.gz，不启动容器"
      exit 0
      ;;
  esac
done

command -v docker >/dev/null 2>&1 || { echo "❌ 需要安装 Docker"; exit 1; }

# ─── 1. 检查 .env ───
if [ ! -f .env ]; then
  echo "⚠️  当前目录没有 .env 文件，将使用镜像内置的默认配置"
  echo "   如需自定义，请创建 .env 后重新构建"
  echo ""
fi

# ─── 2. 构建镜像 ───
echo "============================================================"
echo "  🔨 构建 FMEA Agent 镜像..."
echo "  📦 .env 已打包进镜像，导到任何服务器直接 docker run 即可"
echo "============================================================"
echo ""

docker build $NO_CACHE -t fmea-agent:latest .

echo ""
echo "✅ 镜像构建完成: fmea-agent:latest ($(docker images --format '{{.Size}}' fmea-agent:latest))"
echo ""

# ─── 3. 仅导出模式 ───
if [ "$MODE" = "export" ]; then
  echo "📦 导出镜像为 fmea-agent.tar.gz ..."
  docker save fmea-agent:latest | gzip > fmea-agent.tar.gz
  echo "✅ 导出完成: $(ls -lh fmea-agent.tar.gz | awk '{print $5}')"
  echo ""
  echo "在其他服务器上导入:  docker load < fmea-agent.tar.gz"
  echo "启动容器:            docker run -d --name fmea-agent --restart unless-stopped -p 7777:7777 fmea-agent:latest"
  exit 0
fi

# ─── 4. 清理旧容器 ───
if docker ps -a --format '{{.Names}}' | grep -q '^fmea-agent$'; then
  echo "🧹 停止并移除旧容器..."
  docker stop fmea-agent 2>/dev/null || true
  docker rm fmea-agent 2>/dev/null || true
fi

# ─── 5. 启动容器 ───
# 注意: .env 已在镜像内部，main.py 启动时会自动加载
# 外部 .env 仅用于覆盖默认值（通过 -e 传入的优先级高于 .env 文件）
echo "🚀 启动容器..."

# 数据用 Docker named volume（首次启动自动从镜像预填，不覆盖镜像内基础数据）
# 日志用 bind mount（纯运行时产生，方便查看）

DOCKER_ENV_ARGS=()
if [ -f .env ]; then
  while IFS='=' read -r key value; do
    [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
    key="$(echo "$key" | xargs)"
    [[ -z "$key" || ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && continue
    DOCKER_ENV_ARGS+=("-e" "${key}=${value}")
  done < .env
fi

docker run -d \
  --name fmea-agent \
  --restart unless-stopped \
  -p 7777:7777 \
  "${DOCKER_ENV_ARGS[@]}" \
  -v fmea_data:/app/fmea-agent/data \
  -v fmea_logs:/app/fmea-agent/logs \
  -v fmea_tmp:/app/fmea-agent/tmp \
  -v fmea_reports:/app/fmea-agent/reports \
  fmea-agent:latest

echo ""
echo "============================================================"
echo "  ✅ FMEA Agent 已启动！"
echo "  🌐 访问地址: http://localhost:7777/chat"
echo "  📋 查看日志: docker logs -f fmea-agent"
echo "  ⏹️  停止服务: docker stop fmea-agent"
echo ""
echo "  📦 导出镜像到其他服务器:"
echo "      docker save fmea-agent:latest | gzip > fmea-agent.tar.gz"
echo "      然后 scp 到目标服务器: docker load < fmea-agent.tar.gz"
echo "      docker run -d --name fmea-agent --restart unless-stopped -p 7777:7777 fmea-agent:latest"
echo "============================================================"
