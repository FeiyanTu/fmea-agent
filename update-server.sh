#!/usr/bin/env bash
# ============================================================
# FMEA-Agent 服务器端一键更新脚本
# 用法: 本地打包后上传到服务器，然后:
#   cd /data/demo/agent-fmea && bash update-server.sh
# 功能: 停服务 → 备份旧代码 → 解压覆盖(保留数据/配置) → 清缓存 → 启动 → 健康检查
# ============================================================
set -e

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$BASE_DIR/fmea-agent"
PKG="$BASE_DIR/fmea-agent-code.tar.gz"
PID_FILE="$APP_DIR/logs/server.pid"

# ── 0. 检查更新包 ──
if [ ! -f "$PKG" ]; then
  echo "❌ 未找到更新包: $PKG"
  echo "   请先在本地执行阶段二打包，scp 上传后再运行本脚本"
  exit 1
fi

# ── 1. 停止旧服务 ──
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  OLD_PID=$(cat "$PID_FILE")
  echo "==> [1/6] 停止旧服务 (PID $OLD_PID)"
  kill "$OLD_PID"
  sleep 2
else
  echo "==> [1/6] 服务未在运行，跳过停止"
fi

# ── 2. 备份旧代码 ──
STAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="$BASE_DIR/backup/$STAMP"
mkdir -p "$BACKUP_DIR"
echo "==> [2/6] 备份旧代码 → $BACKUP_DIR"
cd "$BASE_DIR"
tar -czf "$BACKUP_DIR/code.tar.gz" \
  --exclude='fmea-agent/.venv' \
  --exclude='fmea-agent/__pycache__' \
  --exclude='fmea-agent/logs' \
  --exclude='fmea-agent/tmp' \
  --exclude='fmea-agent/data' \
  --exclude='fmea-agent/knowledge' \
  --exclude='fmea-agent/.env' \
  fmea-agent 2>/dev/null || true
echo "    备份完成"

# ── 3. 解压新代码到 APP_DIR（覆盖 fmea-agent/ 内代码，保留 data/knowledge/.env/.venv）──
echo "==> [3/6] 解压新代码 → $APP_DIR"
tar -xzf "$PKG" -C "$APP_DIR" --strip-components=1

# ── 4. 清理 pyc 缓存 ──
echo "==> [4/6] 清理 Python 缓存"
find "$APP_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$APP_DIR" -name "*.pyc" -delete 2>/dev/null || true

# ── 5. 启动服务 ──
echo "==> [5/6] 启动服务"
cd "$APP_DIR"
nohup .venv/bin/python main.py >> logs/server.log 2>&1 &
echo $! > "$PID_FILE"

# ── 6. 健康检查（重试最多 10 次 × 3 秒 = 30 秒，避免启动慢误报）──
echo "==> [6/6] 健康检查"
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -s http://localhost:7777/health > /dev/null 2>&1; then
    echo "✅ 服务已启动: http://localhost:7777 (第 ${i} 次检查通过)"
    echo "   浏览器记得 Ctrl+Shift+R 强刷"
    exit 0
  fi
  sleep 3
done
echo "⚠️  健康检查未通过（30 秒内未就绪），查看日志:"
tail -20 "$APP_DIR/logs/server.log"
echo "   回滚: tar -xzf $BACKUP_DIR/code.tar.gz -C $APP_DIR --strip-components=1 && 重启"
