#!/usr/bin/env bash
# ============================================================
# FMEA-Agent 离线安装脚本（在服务器上执行）
# 前置条件（已按 DEPLOY-Linux-离线版.md 准备并上传）：
#   1. fmea-agent 代码已解压到 $APP_DIR
#   2. agno 源码已解压到 $AGNO_DIR（含 agno / agno_infra）
#   3. wheels/ 离线依赖目录已上传（Linux 平台 + 与服务器 Python 同版本）
#   4. PostgreSQL 已安装（本脚本会提示建库）
# 用法: bash offline_install.sh
# ============================================================
set -e

# ────────── 变量区（按实际环境修改） ──────────
APP_DIR="/opt/fmea-agent"          # 项目代码目录
AGNO_DIR="/opt/agno"               # agno 源码目录（tar 解压目标）
WHEELS_DIR="/opt/wheels"           # 离线 wheel 目录
PYTHON_BIN="python3.12"            # 服务器 Python 可执行名
DB_USER="fmea"                     # 数据库用户
DB_NAME="fmea"                     # 数据库名
# ─────────────────────────────────────────────

echo "==> [1/6] 检查 Python 版本"
$PYTHON_BIN --version

echo "==> [2/6] 创建虚拟环境 (.venv)"
cd "$APP_DIR"
$PYTHON_BIN -m venv .venv
VENV_PY="$APP_DIR/.venv/bin/python"
VENV_PIP="$APP_DIR/.venv/bin/pip"
echo "    venv 已创建: $VENV_PY"

echo "==> [3/6] 离线本地注册 agno + agno_infra"
cd "$AGNO_DIR"
$VENV_PIP install --no-deps --no-index --find-links="$WHEELS_DIR" ./agno ./agno_infra
echo "    agno 注册完成"

echo "==> [4/6] 离线安装 requirements（--no-index 禁网）"
$VENV_PIP install --no-index --find-links="$WHEELS_DIR" -r "$APP_DIR/requirements.txt"
echo "    依赖安装完成"

echo "==> [5/6] 验证 agno.os / AGUI 可导入"
cd "$APP_DIR"
$VENV_PY -c "import agno; from agno.os import AgentOS; from agno.os.interfaces.agui import AGUI; print('agno', agno.__version__, 'OK')"

echo "==> [6/6] 剩余手动步骤（见手册第 6/7 章）"
echo "  ┌─────────────────────────────────────────────────────────┐"
echo "  │ 1. 配置 .env:  cp .env.example .env 后编辑                │"
echo "  │    · FMEA_DB_URL / URL_AGENT_DB_*（库名 $DB_NAME，用户 $DB_USER）│"
echo "  │    · FMEA_MODEL_API_KEY · FMEA_HOST=0.0.0.0              │"
echo "  │ 2. 建库（PG 未建时）:                                      │"
echo "  │    sudo -u postgres psql -c \"CREATE USER $DB_USER WITH PASSWORD '改强密码';\"   │"
echo "  │    sudo -u postgres psql -c \"CREATE DATABASE $DB_NAME OWNER $DB_USER ENCODING 'UTF8';\" │"
echo "  │ 3. 后台运行（二选一）:                                      │"
echo "  │    · systemd: sudo cp deploy/fmea-agent.service /etc/systemd/system/ && sudo systemctl enable --now fmea-agent │"
echo "  │    · nohup:   nohup .venv/bin/python main.py >> logs/server.log 2>&1 &   │"
echo "  └─────────────────────────────────────────────────────────┘"

echo ""
echo "✅ 离线安装完成！浏览器访问 http://服务器IP:7777/  （默认账号 admin/admin123）"
