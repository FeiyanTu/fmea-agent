# FMEA-Agent Linux 离线部署手册（后台运行）

> 适用场景：**服务器完全不能联网**（内网隔离），需要从有网机器搬运全部依赖，部署完成后在**后台常驻运行**。
>
> 与「在线版」的核心差异：
> - agno 不走 pip 联网安装，改为**源码本地注册**（沿袭项目 requirements.txt 既定约定：agno 本地 `--no-deps` 安装）
> - Python 依赖用**离线 wheel 包**安装（`--no-index --find-links`）
> - PostgreSQL / Python 3.12 也需提前离线准备（若系统自带则跳过）
> - 后台运行：**systemd**（推荐，开机自启）/ **nohup**（无 root 备选）

---

## 0. 离线部署总览

```
┌─────────── 有网机器（本地 Windows / 任意有网 Linux）───────────┐
│  ① 打包 fmea-agent 代码（排除 __pycache__/logs/tmp/data/knowledge/.env） │
│  ② 打包 agno 源码（libs/agno + libs/agno_infra）                          │
│  ③ pip download 下载 Linux 版依赖 wheel                                 │
│  ④ （按需）下载 PostgreSQL / Python3.12 离线安装包                        │
└──────────────────────────────┬─────────────────────────────┘
                               ↓ scp / 内网拷贝
┌──────────────────── 服务器（离线 Linux）───────────────────┐
│  1. 安装 PostgreSQL（离线包）→ 建库建用户                          │
│  2. Python 3.12 + venv                                           │
│  3. 离线装 agno 源码（本地注册）→ 离线装 requirements（wheels）   │
│  4. 配置 .env（FMEA_HOST=0.0.0.0）                               │
│  5. systemd / nohup 后台运行                                      │
│  6. 防火墙放行 7777 → 浏览器访问 http://服务器IP:7777             │
└──────────────────────────────────────────────────────────┘
```

**需要准备的离线物料清单：**

| # | 物料 | 来源 | 说明 |
|---|---|---|---|
| 1 | 代码包 `fmea-agent-deploy.tar.gz` | 本地打包 | 排除 `__pycache__/logs/tmp/data/knowledge/.env` |
| 2 | agno 源码包 `agno-src.tar.gz` | `D:\agent-fmea\agno\libs\` | 含 `agno` + `agno_infra` 两个目录 |
| 3 | 依赖 wheel 目录 `wheels/` | 有网机器 `pip download` | **必须是 Linux x86_64 + 服务器 Python 同版本** |
| 4 | PostgreSQL 安装包 | 有网机器/运维 | 服务器已有 PG 可跳过 |
| 5 | Python 3.12 安装包 | 系统自带/运维 | Ubuntu 24.04 自带 3.12 |

---

## 1. 有网机器：准备离线包

> ⚠️ 本节全部在**能上网的机器**上执行。

### 1.1 打包 agno 源码（本地 Windows 执行）

```bash
cd /d/agent-fmea/agno/libs && tar -czf /d/agent-fmea/agno-src.tar.gz agno agno_infra
```

### 1.2 打包 fmea-agent 代码（本地 Windows 执行）

```bash
cd /d/agent-fmea && tar \
  --exclude='fmea-agent/__pycache__' \
  --exclude='fmea-agent/logs' \
  --exclude='fmea-agent/tmp' \
  --exclude='fmea-agent/data' \
  --exclude='fmea-agent/knowledge' \
  --exclude='fmea-agent/.env' \
  -czf fmea-agent-deploy.tar.gz fmea-agent
```

### 1.3 下载 Linux 版依赖 wheel（关键步骤）

**方式 A（推荐）：找一台有网 Linux 机器（或 WSL）执行**——平台天然正确，最省心：

```bash
mkdir -p wheels && cd wheels
# 用与服务器相同版本的 Python（3.12）
pip download -r requirements.txt -d . --only-binary=:all:
```

> 注意：`-r requirements.txt` 的路径要指到项目里的 requirements.txt（`/path/to/fmea-agent/requirements.txt`）。
> 若目标服务器是 ARM64，改用 `pip download -r requirements.txt -d . --only-binary=:all: --platform manylinux2014_aarch64 --python-version 312`。

**方式 B（没有 Linux 时）：Windows 本地指定 Linux 平台下载：**

```bash
cd /d/agent-fmea
mkdir -p wheels
python -m pip download -r requirements.txt -d wheels \
  --only-binary=:all: \
  --platform manylinux2014_x86_64 \
  --python-version 312
```

> ⚠️ Windows 本地下载的 wheel **默认是 Windows 版**（win_amd64），拷到 Linux 装不了！必须带 `--platform manylinux2014_x86_64 --python-version 312`（312 对应服务器 Python 3.12；若服务器用 3.13 则写 313）。
> 若某包报「no matching distribution」（缺少对应 wheel），去其 PyPI 页面确认是否有 manylinux wheel，或改用方式 A。

**补充：agno 源码安装需要 setuptools/wheel，确保 wheels 目录里有：**

```bash
python -m pip download setuptools wheel -d wheels
```

### 1.4 准备 PostgreSQL 离线包（仅服务器没装 PG 时需要）

- **Ubuntu**（在有网同版本 Ubuntu 上）：`apt-get download postgresql postgresql-contrib postgresql-14`（或用 `apt-get install --download-only` 整包下载依赖）
- **CentOS**（在有网同版本 CentOS 上）：`yumdownloader --resolve postgresql-server postgresql-contrib`
- 或使用 Docker 镜像：`docker pull postgres:14 && docker save postgres:14 -o postgres14.tar`，服务器离线 `docker load`（服务器需先装好 docker）

> 最简单方案：**让运维提前把 PG 装好**，或选用自带 PostgreSQL 的服务器镜像。

---

## 2. 上传全部物料到服务器

```bash
scp fmea-agent-deploy.tar.gz agno-src.tar.gz user@服务器IP:/opt/
scp -r wheels user@服务器IP:/opt/wheels
# PG 离线包也一并上传
```

> 内网隔离机器之间，可用 U 盘/内网共享盘拷贝，效果相同。

---

## 3. 服务器：安装 PostgreSQL 并建库

### 3.1 离线安装 PG（若已有则跳过）

```bash
# Ubuntu（用第 1.4 步下载的 .deb 包）
sudo dpkg -i /opt/pg-packages/*.deb
# CentOS（用第 1.4 步下载的 .rpm 包）
sudo yum localinstall -y /opt/pg-packages/*.rpm
sudo systemctl enable --now postgresql
```

### 3.2 建库建用户

```bash
sudo -u postgres psql <<'SQL'
CREATE USER fmea WITH PASSWORD '请改成强密码';
CREATE DATABASE fmea OWNER fmea ENCODING 'UTF8';
GRANT ALL PRIVILEGES ON DATABASE fmea TO fmea;
SQL
```

> 空库部署：**无需建表脚本**，`main.py` 首次启动自动建表并创建默认管理员 `admin/admin123`。

---

## 4. 服务器：Python 3.12 + 虚拟环境

```bash
# 确认 Python 版本（Ubuntu 24.04 自带 3.12；若无，用运维提供的离线安装包装 3.12）
python3.12 --version

cd /opt/fmea-agent
python3.12 -m venv .venv
```

---

## 5. 服务器：离线安装依赖（核心）

```bash
# ① 解压 agno 源码到项目上一级目录（main.py 会自动发现：/opt/agno/libs/agno）
sudo mkdir -p /opt/agno
sudo tar -xzf /opt/agno-src.tar.gz -C /opt/agno
sudo chown -R $(whoami) /opt/agno

# ② 离线本地注册 agno + agno_infra（--no-deps，绝不联网）
cd /opt/agno
/opt/fmea-agent/.venv/bin/pip install --no-deps --no-index --find-links=/opt/wheels ./agno ./agno_infra

# ③ 离线安装全部依赖（agno 已注册，满足 requirements.txt 中的 agno>=2.6.0，不会重复找）
/opt/fmea-agent/.venv/bin/pip install --no-index --find-links=/opt/wheels -r /opt/fmea-agent/requirements.txt

# ④ 验证
cd /opt/fmea-agent
.venv/bin/python -c "import agno; from agno.os import AgentOS; from agno.os.interfaces.agui import AGUI; print('agno', agno.__version__, 'OK')"
```

> **为什么 agno 放 `/opt/agno/libs/agno`**：`main.py` 的 `_find_agno_path()` 第一优先查找 `项目上一级目录/agno/libs/agno`（即 `/opt/agno/libs/agno`），自动命中，无需配置 `AGNO_PATH`。
> 若验证报 `ModuleNotFoundError: agno.os`，说明 agno_infra 没装上，重跑第 ② 步。

---

## 6. 配置 .env

```bash
cd /opt/fmea-agent
cp .env.example .env
vim .env
```

```ini
FMEA_MODEL_NAME=deepseek-chat
FMEA_MODEL_BASE_URL=https://api.deepseek.com
FMEA_MODEL_API_KEY=sk-xxxxxxxx

# PostgreSQL（本机）
FMEA_DB_URL=postgresql+psycopg2://fmea:强密码@localhost:5432/fmea
URL_AGENT_DB_HOST=localhost
URL_AGENT_DB_PORT=5432
URL_AGENT_DB_NAME=fmea
URL_AGENT_DB_USER=fmea
URL_AGENT_DB_PASSWORD=强密码

# 服务（0.0.0.0 必须，外部才可访问）
FMEA_HOST=0.0.0.0
FMEA_PORT=7777
LOG_LEVEL=INFO

# 可选：邮件通知
# FMEA_NOTIFY_SENDER_EMAIL=xxx@qq.com
# FMEA_NOTIFY_SENDER_AUTH=SMTP授权码
# FMEA_PUBLIC_URL=http://服务器IP:7777
```

---

## 7. 后台运行

### 7.1 systemd 守护（推荐：开机自启 + 崩溃自动重启）

```bash
# 编辑服务文件里的 User 为实际运行用户
sudo vim /opt/fmea-agent/deploy/fmea-agent.service

sudo cp /opt/fmea-agent/deploy/fmea-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fmea-agent
```

常用命令：

```bash
systemctl status fmea-agent      # 状态
systemctl restart fmea-agent     # 重启（改代码后必用）
journalctl -u fmea-agent -f      # 实时日志
```

服务文件要点（`deploy/fmea-agent.service`）：`WorkingDirectory=/opt/fmea-agent`、`ExecStart=/opt/fmea-agent/.venv/bin/python main.py`、`Restart=on-failure`。

### 7.2 nohup 后台运行（无 root / 不想用 systemd 时）

```bash
cd /opt/fmea-agent
# 后台启动，日志写入 logs/server.log
nohup .venv/bin/python main.py >> logs/server.log 2>&1 &
echo $! > logs/server.pid   # 记录 PID

# 查看运行状态与日志
cat logs/server.pid | xargs ps -p        # 进程是否存活
tail -f logs/server.log                  # 实时日志

# 停止
kill $(cat logs/server.pid)

# 开机自启（可选）：将启动命令追加到 /etc/rc.local 或 crontab -e：
# @reboot cd /opt/fmea-agent && nohup .venv/bin/python main.py >> logs/server.log 2>&1 &
```

> 提示：nohup 方式进程在**注销 SSH 后依然运行**；但服务器重启后不会自动拉起（除非配置 crontab @reboot），且进程崩溃不会自动重启——生产建议用 systemd。

---

## 8. 验证与防火墙

```bash
# 防火墙放行 7777
sudo firewall-cmd --permanent --add-port=7777/tcp && sudo firewall-cmd --reload   # CentOS
# 或
sudo ufw allow 7777/tcp                                                                # Ubuntu

# 服务健康检查（服务器本机）
curl -s http://localhost:7777/health      # 期望 200

# WeKnora 同内网可达性（结构树/功能分析依赖）
curl -s -o /dev/null -w "%{http_code}" http://10.180.17.11:81/api/v1/knowledge-bases   # 有响应即通
```

浏览器访问：**`http://服务器IP:7777/`**，用 `admin/admin123` 登录（首次登录后立即改密码）。

**验收清单：** 健康检查 200 → 页面可打开 → 登录成功 → 数据库出现 users/projects/fmea_step_assignee_data 等表 → 聊天框提问有回复 → 结构分析正常。

---

## 9. 离线部署常见问题

| 现象 | 处理 |
|---|---|
| `pip` 提示找不到某包（no matching distribution / Could not find a version） | wheels 目录缺包：回有网机器 `pip download <包名> --platform manylinux2014_x86_64 --python-version 312` 补下后重传 |
| 装 agno 源码报构建错误 | wheels 里缺 setuptools/wheel，用 1.3 补充命令下载后重装 |
| `ModuleNotFoundError: agno.os` | agno_infra 未注册，重跑第 5 步 ② |
| 装包提示「Looking in links: ...」但卡住/失败 | 确认所有命令都带 `--no-index`（禁网），且 wheel 平台与 Python 版本匹配 |
| 后台进程跑一会儿消失 | 换 systemd（Restart=on-failure 自动拉起）；nohup 版确认 `logs/server.log` 里有无报错 |
| 外部访问不了 | ① `.env` 的 `FMEA_HOST` 是否为 `0.0.0.0`；② 防火墙 7777 是否放行 |
| 服务器重启后服务没了 | systemd 已 `enable` 则自动拉起；nohup 版需配 `crontab @reboot`（见 7.2） |
| PostgreSQL 连不上 | 确认 PG 已装、服务已启（`systemctl status postgresql`）、密码正确 |

---

## 附录：离线安装一键脚本

`deploy/offline_install.sh`（随项目提供）：把第 4~7 章的命令串成脚本，上传后在服务器执行。**使用前必须修改脚本顶部的变量**（`.venv` 路径、wheel 目录、数据库密码等）。

---

*本手册按「Linux 全离线 + 空库 + 同内网 WeKnora + 后台运行」编写；wheel 平台版本务必与服务器 Python 保持一致。*
