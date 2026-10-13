#!/usr/bin/env bash
# ============================================================
# FMEA-Agent 本地一键更新脚本（在 Windows 本地运行，最快方式）
# 用法: bash deploy-fast.sh
# 功能: 打包代码 → 上传服务器 → 远端自动 停服/备份/覆盖/启动/验证
# 前置: 服务器上已有 update-server.sh（本脚本每次会自动同步最新版）
# ============================================================
set -e

# ────────── 变量区（按实际修改）──────────
SERVER_USER="appuser"          # 服务器用户名
SERVER_HOST="10.180.17.11"      # ← 改成你的服务器 IP
SERVER_DIR="/data/demo/agent-fmea"   # 服务器项目根目录
# ────────────────────────────────────────

PKG="/d/agent-fmea/fmea-agent-code.tar.gz"
LOCAL_UPDATE_SH="/d/agent-fmea/update-server.sh"

echo "==> [1/4] 本地打包代码（自动排除 .venv/data/knowledge/.env 等危险目录）"
cd /d/agent-fmea
rm -f fmea-agent-code.tar.gz
tar \
  --exclude='fmea-agent/.venv' \
  --exclude='fmea-agent/__pycache__' \
  --exclude='fmea-agent/logs' \
  --exclude='fmea-agent/tmp' \
  --exclude='fmea-agent/data' \
  --exclude='fmea-agent/knowledge/*/*' \
  --exclude='fmea-agent/.env' \
  --exclude='fmea-agent/.envbat' \
  -czf fmea-agent-code.tar.gz fmea-agent
# ⚠️ knowledge/ 里既有 Python 包（weknora_client.py / file_converter.py 等，必须上传），
#    又有各项目的知识库数据（12V电池-weknora/ 等，必须排除）。
#    所以只能排除 "knowledge/*/*"（子目录内容），不能写成 --exclude='fmea-agent/knowledge'
#    ——那样会把 knowledge 包整个漏掉，服务器启动直接 ModuleNotFoundError。
echo "    打包完成: $(du -h fmea-agent-code.tar.gz | cut -f1)"

echo "==> [2/4] 上传到服务器 (${SERVER_USER}@${SERVER_HOST})"
scp "$PKG" "$LOCAL_UPDATE_SH" "${SERVER_USER}@${SERVER_HOST}:${SERVER_DIR}/"

echo "==> [3/4] 服务器自动更新（停服→备份→覆盖→清缓存→启动→健康检查）"
ssh "${SERVER_USER}@${SERVER_HOST}" "cd ${SERVER_DIR} && bash update-server.sh"

echo "==> [4/4] 更新完成！"
echo "    浏览器打开 http://${SERVER_HOST}:7777/ ，Ctrl+Shift+R 强刷"
