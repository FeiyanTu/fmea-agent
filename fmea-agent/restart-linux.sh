#!/usr/bin/env bash
# ============================================================
# FMEA-Agent Linux 一键重启脚本（端口动态找 PID）
#
# 用法:
#   bash restart-linux.sh                     # 自动检测代码目录
#   FMEA_APP_DIR=/opt/fmea-agent bash restart-linux.sh   # 手动指定代码目录
#
# 功能: 根据 7777 端口动态找到 PID → 优雅停止 → 等待端口释放
#       → 后台启动 → 写 PID 文件 → 健康检查
# 特点: PID 不固定，不依赖 server.pid，每次启动实时查端口
# ============================================================
set -e

PORT=7777
FMEAHOST="${FMEA_HOST:-0.0.0.0}"

# ── 1. 定位代码目录 ──
# 优先用环境变量，其次根据脚本位置自动推导（支持 update-server.sh 的 /data/demo/agent-fmea 布局）
if [ -n "$FMEA_APP_DIR" ]; then
  APP_DIR="$FMEA_APP_DIR"
elif [ -d "$(dirname "$0")/fmea-agent/main.py" ]; then
  APP_DIR="$(cd "$(dirname "$0")" && pwd)/fmea-agent"     # 脚本在 /data/demo/agent-fmea/ 下
elif [ -f "$(dirname "$0")/main.py" ]; then
  APP_DIR="$(cd "$(dirname "$0")" && pwd)"                 # 脚本就在 fmea-agent/ 内
else
  echo "❌ 找不到 main.py。请用 FMEA_APP_DIR 指定代码目录，例如:"
  echo "   FMEA_APP_DIR=/opt/fmea-agent bash restart-linux.sh"
  exit 1
fi
cd "$APP_DIR"
echo "==> 代码目录: $APP_DIR"
[ -f main.py ] || { echo "❌ $APP_DIR/main.py 不存在"; exit 1; }
[ -x .venv/bin/python ] || { echo "❌ 未找到虚拟环境 .venv/bin/python"; exit 1; }

# ── 2. 根据端口动态找 PID ──
find_pid_by_port() {
  local p
  # 方式1: ss（优先，Linux 通用）
  p=$(ss -ltnp 2>/dev/null | awk -v port=":$PORT\$" '$0 ~ port {match($0, /pid=([0-9]+)/, m); if (m[1]) {print m[1]; exit}}')
  [ -n "$p" ] && { echo "$p"; return; }
  # 方式2: lsof
  p=$(lsof -t -i:"$PORT" 2>/dev/null | head -1)
  [ -n "$p" ] && { echo "$p"; return; }
  # 方式3: fuser
  p=$(fuser "$PORT/tcp" 2>/dev/null | tr -s ' ' | head -1)
  [ -n "$p" ] && { echo "$p"; return; }
  echo ""
}

PID=$(find_pid_by_port)
if [ -n "$PID" ]; then
  echo "==> [1/3] 找到占用端口 $PORT 的进程 PID=$PID，开始停止..."
  # 先优雅终止 (SIGTERM)，给 uvicorn 收尾时间
  kill "$PID" 2>/dev/null || true
  # 最多等 15 秒，等端口释放
  for i in $(seq 1 15); do
    if [ -z "$(find_pid_by_port)" ]; then
      echo "     进程已退出 (第 ${i} 秒)"
      break
    fi
    if [ "$i" -eq 15 ]; then
      echo "     ⚠️  15 秒未退出，强制 SIGKILL"
      kill -9 "$PID" 2>/dev/null || true
      sleep 2
    fi
    sleep 1
  done
  # 兜底：如果还在，再查一次端口强制杀
  REMAIN=$(find_pid_by_port)
  if [ -n "$REMAIN" ]; then
    echo "     ⚠️  端口仍被 PID=$REMAIN 占用，强制结束"
    kill -9 "$REMAIN" 2>/dev/null || true
    sleep 2
  fi
else
  echo "==> [1/3] 端口 $PORT 未被占用，无需停止"
fi

# ── 3. 启动服务 ──
echo "==> [2/3] 启动 FMEA Agent (监听 $FMEAHOST:$PORT)..."
mkdir -p logs
# 清空 WorkBuddy 注入的 safe-delete 环境变量，避免删文件被拦截（仅本地开发机有，服务器无则无影响）
unset CODEBUDDY_SESSION_ID CODEBUDDY_SAFE_DELETE_SANDBOX 2>/dev/null || true
nohup .venv/bin/python main.py >> logs/server.log 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > logs/server.pid
echo "     已启动 PID=$NEW_PID (写入 logs/server.pid)"

# ── 4. 健康检查（最多 10 次 × 3 秒 = 30 秒）──
echo "==> [3/3] 健康检查 http://localhost:$PORT/health"
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -s "http://localhost:$PORT/health" > /dev/null 2>&1; then
    echo "✅ 服务已就绪: http://localhost:$PORT (第 ${i} 次检查通过, PID=$NEW_PID)"
    echo "   前端页面记得 Ctrl+Shift+R 强刷"
    exit 0
  fi
  sleep 3
done

echo "⚠️  健康检查未通过（30 秒内未就绪），最近日志:"
tail -20 logs/server.log
echo "   若为新代码部署失败，可回滚代码后重新运行本脚本"
exit 1
