#!/usr/bin/env bash
# ============================================================
# FMEA-Agent Linux 一键安装脚本
# 位置：D:\agent-fmea\install-linux.sh（整个文件夹拷贝时自动带上）
# 用法：cd /opt/agent-fmea && bash install-linux.sh
# 功能：重建 venv → 本地注册 agno → 安装依赖 → 生成 .env 模板
# 说明：自动检测是否离线；有 wheels/ 目录走离线安装，否则走在线安装
# ============================================================
set -e

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$BASE_DIR/fmea-agent"
AGNO_DIR="$BASE_DIR/agno"
WHEELS_DIR="$BASE_DIR/wheels"

# 1) 检查 Python 版本
echo "==> [1/6] 检查 Python"
PY_BIN="python3.12"
command -v "$PY_BIN" >/dev/null 2>&1 || PY_BIN="python3"
$PY_BIN --version

# 2) 创建虚拟环境（--clear 清空旧残留，避免 site-packages 污染）
echo "==> [2/6] 创建虚拟环境 (.venv)"
cd "$APP_DIR"
$PY_BIN -m venv --clear .venv
VENV_PIP="$APP_DIR/.venv/bin/pip"
VENV_PY="$APP_DIR/.venv/bin/python"

# 2.5) 先装 setuptools + wheel（agno 源码注册构建需要，并修复 _distutils_hack 缺失）
echo "==> [2.5/6] 安装 setuptools / wheel"
if [ -d "$WHEELS_DIR" ] && [ "$(ls -A "$WHEELS_DIR" 2>/dev/null)" ]; then
    $VENV_PIP install --no-index --find-links="$WHEELS_DIR" setuptools wheel \
        || $VENV_PIP install setuptools wheel
else
    $VENV_PIP install setuptools wheel
fi

# 3) 本地注册 agno（用文件夹自带的源码，离线可用）
echo "==> [3/6] 注册 agno 源码"
OFFLINE_ARGS="--no-deps --no-index --find-links=$WHEELS_DIR"
if [ -d "$WHEELS_DIR" ] && [ "$(ls -A "$WHEELS_DIR" 2>/dev/null)" ]; then
    $VENV_PIP install $OFFLINE_ARGS "$AGNO_DIR/libs/agno" "$AGNO_DIR/libs/agno_infra" \
        || $VENV_PIP install --no-deps "$AGNO_DIR/libs/agno" "$AGNO_DIR/libs/agno_infra"
else
    $VENV_PIP install --no-deps "$AGNO_DIR/libs/agno" "$AGNO_DIR/libs/agno_infra"
fi

# 4) 安装项目依赖
echo "==> [4/6] 安装项目依赖"
if [ -d "$WHEELS_DIR" ] && [ "$(ls -A "$WHEELS_DIR" 2>/dev/null)" ]; then
    $VENV_PIP install --no-index --find-links="$WHEELS_DIR" -r requirements.txt \
        || $VENV_PIP install -r requirements.txt
else
    $VENV_PIP install -r requirements.txt
fi

# 5) 验证
echo "==> [5/6] 验证"
cd "$APP_DIR"
$VENV_PY -c "import agno; from agno.os import AgentOS; from agno.os.interfaces.agui import AGUI; print('agno', agno.__version__, 'OK')"

# 6) 生成 .env 模板
echo "==> [6/6] 生成 .env 模板"
if [ ! -f .env ]; then
    cp .env.example .env
    echo "    已生成 .env，请编辑以下 4 项后启动："
    echo "    · FMEA_MODEL_API_KEY      DeepSeek key"
    echo "    · FMEA_DB_URL             数据库连接串"
    echo "    · URL_AGENT_DB_PASSWORD   数据库密码"
    echo "    · FMEA_HOST               改为 0.0.0.0"
else
    echo "    .env 已存在，跳过"
fi

echo ""
echo "==============================================="
echo " ✅ 安装完成！接下来："
echo "    1) vim $APP_DIR/.env  配置 4 项必填"
echo "    2) 建库: sudo -u postgres psql -c \"CREATE USER fmea WITH PASSWORD '密码'; CREATE DATABASE fmea OWNER fmea ENCODING 'UTF8';\""
echo "    3) 启动: cd $APP_DIR && nohup .venv/bin/python main.py >> logs/server.log 2>&1 &"
echo "    4) 访问: http://服务器IP:7777/  (admin/admin123)"
echo "==============================================="
