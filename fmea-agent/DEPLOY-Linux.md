# FMEA-Agent Linux 服务器部署手册

> 目标环境（按本次部署需求确认）：**Linux（Ubuntu/CentOS）· 空库全新部署 · WeKnora 同内网可达 · 服务器可联网安装依赖**
> 已侦查确认：项目代码无硬编码绝对路径（知识库/数据目录基于 `Path(__file__)` 动态计算），拷到任意目录可直接运行；数据库表首次启动自动创建；前端为纯静态页面，无需构建。

---

## 0. 部署总览

```
┌───────────── 服务器 ─────────────┐
│                                  │
│  /opt/fmea-agent/                │  代码 + .env + .venv
│    ├── main.py                   │
│    ├── .env                      │
│    └── .venv/                    │  Python 3.12 虚拟环境
│                                  │
│  PostgreSQL (localhost:5432)     │  库名 fmea，main.py 启动自动建表
│                                  │
│  systemd: fmea-agent.service     │  守护进程
│                                  │
│  访问方式: http://服务器IP:7777   │  直连端口，不走 nginx
│                                  │
├────────── 外部依赖 ──────────────┤
│  DeepSeek API（公网）             │  .env 配置 key
│  WeKnora 内网 10.180.17.11:81    │  同内网，无需改动
└──────────────────────────────────┘
```

**部署清单（要搬到服务器的东西）：**

| 内容 | 说明 |
|---|---|
| 项目代码 `fmea-agent/` | 打包上传（排除 __pycache__/logs/tmp/data/knowledge） |
| Python 依赖 | 服务器联网 `pip install`（含 agno） |
| PostgreSQL | 服务器上新建空库，首次启动自动建表 |
| 配置文件 `.env` | 服务器上新建，改数据库连接 + 填 DeepSeek Key |
| ~~agno 源码~~ | **无需拷贝**：联网 `pip install agno[os,agui]==2.6.11` 后，`main.py` 自动检测并采用 pip 版 |
| ~~历史数据~~ | 空库全新部署，不迁移本地 2 个项目 |
| ~~start.bat~~ | 作废，改用 systemd 守护 |

---

## 1. 准备服务器基础环境

### Ubuntu（22.04/24.04）

```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv python3-pip postgresql postgresql-contrib
```

> Ubuntu 24.04 自带 Python 3.12；若默认 Python 版本不是 3.12，请用 `apt install python3.12` 安装后再用绝对路径。

### CentOS / Rocky Linux（7/8/9）

```bash
# 安装 Python 3.12（EPEL 或源码编译，按发行版选择）
sudo yum install -y epel-release
sudo yum install -y python3.12 python3.12-pip postgresql-server postgresql-contrib
# CentOS 7 的 Postgres 版本较旧，建议官方源装 PG 14+
```

### 防火墙

```bash
# 直连 IP:7777 访问，放行 7777 端口
sudo firewall-cmd --permanent --add-port=7777/tcp && sudo firewall-cmd --reload   # firewalld (CentOS)
# 或
sudo ufw allow 7777/tcp                                                                # ufw (Ubuntu)
```

---

## 2. 创建 PostgreSQL 数据库

```bash
sudo -u postgres psql <<'SQL'
CREATE USER fmea WITH PASSWORD '请改成强密码';
CREATE DATABASE fmea OWNER fmea ENCODING 'UTF8';
GRANT ALL PRIVILEGES ON DATABASE fmea TO fmea;
SQL
```

> 空库部署：**不需要**执行任何建表脚本，`main.py` 首次启动会自动建全部表并创建默认管理员 `admin/admin123`。

---

## 3. 上传项目代码

### 3.1 本地打包（在 Windows 本地执行）

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

> 排除项说明：`__pycache__`/`logs`/`tmp` 可再生；`data`/`knowledge` 是本地旧项目数据（空库不需要）；`.env` 含本地密钥不打包。

### 3.2 上传并解压

```bash
scp fmea-agent-deploy.tar.gz user@服务器IP:/opt/
ssh user@服务器IP
sudo mkdir -p /opt/fmea-agent
sudo tar -xzf /opt/fmea-agent-deploy.tar.gz -C /opt/fmea-agent --strip-components=1
sudo chown -R $(whoami) /opt/fmea-agent
```

---

## 4. 创建虚拟环境并安装依赖

```bash
cd /opt/fmea-agent

# 创建 venv（用 3.12，本地 3.13 验证过，3.11/3.12/3.13 均可）
python3.12 -m venv .venv
source .venv/bin/activate

# 1) 先装 agno 运行所需依赖（requirements.txt 已列出，含 psycopg2/PyMuPDF/fastapi 等）
pip install -r requirements.txt

# 2) 联网安装 agno（关键！注意 extras：os + agui，本项目用到 AgentOS 和 AGUI 界面）
pip install "agno[os,agui]==2.6.11"

# 3) 验证
python -c "import agno; from agno.os import AgentOS; from agno.os.interfaces.agui import AGUI; print('agno', agno.__version__, 'OK')"
```

> **为什么这样装 agno**：`main.py` 的 `_find_agno_path()` 查找顺序是「项目上一级目录的 agno → pip 安装版 → 默认路径」。pip 装好后会自动命中第 2 项，**无需设置 AGNO_PATH，无需拷贝 agno 源码**。
> 若输出报 `ModuleNotFoundError: agno.os`，说明 extras 没装上，执行 `pip install "agno[os,agui]==2.6.11"` 重装。

---

## 5. 配置 .env

```bash
cd /opt/fmea-agent
cp .env.example .env
vim .env
```

必填项（服务器版）：

```ini
# LLM 配置（同内网/公网访问 DeepSeek）
FMEA_MODEL_NAME=deepseek-chat
FMEA_MODEL_BASE_URL=https://api.deepseek.com
FMEA_MODEL_API_KEY=sk-xxxxxxxx            # 服务器可访问 DeepSeek 的 key

# PostgreSQL（服务器本机）
FMEA_DB_URL=postgresql+psycopg2://fmea:强密码@localhost:5432/fmea
URL_AGENT_DB_HOST=localhost
URL_AGENT_DB_PORT=5432
URL_AGENT_DB_NAME=fmea
URL_AGENT_DB_USER=fmea
URL_AGENT_DB_PASSWORD=强密码

# 服务
FMEA_HOST=0.0.0.0                          # 必须 0.0.0.0，外部才能用 IP:7777 访问
FMEA_PORT=7777
LOG_LEVEL=INFO

# 可选：邮件通知
# FMEA_NOTIFY_SENDER_EMAIL=xxx@qq.com
# FMEA_NOTIFY_SENDER_AUTH=SMTP授权码
# FMEA_PUBLIC_URL=http://服务器IP:7777   # 邮件里展示的访问地址
```

> ⚠️ 关键差异：`FMEA_HOST` 本地是 `localhost`，服务器上**必须改为 `0.0.0.0`**，否则外部无法通过 `IP:7777` 访问。

---

## 6. 前台启动验证

```bash
cd /opt/fmea-agent
.venv/bin/python main.py
```

观察日志，确认：

1. 数据库连接成功、自动建表完成
2. 出现 `✅ 已创建默认管理员账号: admin / admin123`（仅首次启动）
3. 服务监听 7777

另开一个终端验证：

```bash
curl -s http://localhost:7777/health
# 期望: HTTP 200

# 验证 WeKnora 同内网可达（结构树/功能分析依赖）
curl -s -o /dev/null -w "%{http_code}" http://10.180.17.11:81/api/v1/knowledge-bases
# 期望: 200 或 401/403（有响应即通）
```

验证通过后 `Ctrl+C` 停掉，进入下一步配置守护进程。

---

## 7. systemd 守护（推荐）

创建服务文件（模板见 `deploy/fmea-agent.service`，已随项目提供）：

```bash
sudo cp /opt/fmea-agent/deploy/fmea-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fmea-agent
```

常用命令：

```bash
systemctl status fmea-agent      # 状态
systemctl restart fmea-agent     # 重启（改完代码后必用）
journalctl -u fmea-agent -f      # 实时日志
```

> 服务文件核心内容：`WorkingDirectory=/opt/fmea-agent`、`ExecStart=/opt/fmea-agent/.venv/bin/python main.py`、`Restart=on-failure`。日志由项目自带 `logs/` 目录落盘。

---

## 8. 访问方式（直连 IP:端口）

部署完成后直接访问：

```
http://服务器IP:7777/
```

- 浏览器直接打开上面的地址即可（无需 nginx、无需域名）
- SOR 文档/评分文件上传、SSE 初始化进度均直接走 uvicorn，无额外限制
- 如需以后加域名、80 端口或 HTTPS，可再补 nginx 反代，模板已备好：`deploy/nginx-fmea.conf`（要点：`client_max_body_size 200m` + `proxy_buffering off`，SSE 必须关缓冲）

---

## 9. 部署后验收清单

| # | 检查项 | 方法 | 期望 |
|---|---|---|---|
| 1 | 服务健康 | `curl -s http://localhost:7777/health` | 200 |
| 2 | 页面可访问 | 浏览器打开 `http://服务器IP:7777/` | 控制台首页 |
| 3 | 默认账号 | 用 `admin/admin123` 登录 | 登录成功（**立即改密码**） |
| 4 | 数据库建表 | `sudo -u postgres psql -d fmea -c '\dt'` | 出现 users/projects/fmea_step_assignee_data 等表 |
| 5 | DeepSeek 可用 | 登录后在聊天框问任意问题 | AI 有回复 |
| 6 | WeKnora 可用 | 创建项目后做结构分析 | 结构树正常生成 |
| 7 | 自动建知识库 | 初始化项目 | knowledge 目录自动创建 |

---

## 10. 代码更新与回滚

### 更新代码

```bash
# 本地重新打包（同第 3 步，含新改动）
# 上传后：
sudo tar -xzf fmea-agent-deploy.tar.gz -C /opt/fmea-agent --strip-components=1
sudo chown -R $(whoami) /opt/fmea-agent
systemctl restart fmea-agent
```

> ⚠️ 更新后浏览器端务必 **Ctrl+Shift+R 强制刷新**（前端有缓存）；若改动了依赖，需重新 `pip install`。

### 备份

```bash
# 数据库（含全部 FMEA 业务数据）
pg_dump -h localhost -U fmea fmea > fmea_backup_$(date +%F).sql

# 配置与数据目录
tar -czf fmea-data-backup.tar.gz /opt/fmea-agent/.env /opt/fmea-agent/data /opt/fmea-agent/knowledge
```

### 回滚

```bash
systemctl stop fmea-agent
# 恢复旧代码包 + 旧 .env 后
systemctl start fmea-agent
```

---

## 11. Linux 常见问题

| 现象 | 处理 |
|---|---|
| `ModuleNotFoundError: agno.os` | extras 没装全，重装 `pip install "agno[os,agui]==2.6.11"` |
| 外部访问不了，但本机 curl 通 | ① `.env` 的 `FMEA_HOST` 未改 `0.0.0.0`，改后重启；② 防火墙未放行 7777 |
| 上传 SOR/评分文件失败 | 直连 7777 无大小限制；若日后走 nginx 需 `client_max_body_size 200m;` |
| PostgreSQL 连不上 | 确认库/用户已建、`FMEA_DB_URL` 密码正确、PG 监听 localhost |
| 重启后数据还在吗 | 在，业务数据全在 PostgreSQL；`data/` 目录只存快照/导出 |
| WeKnora 请求超时 | 服务器与 10.180.17.11:81 网络不通，需走内网/专线 |

---

*本手册按「Linux + 空库 + 同内网 + 可联网」场景编写；若后续改为 Windows Server 或需迁移历史数据，联系开发人员调整。*
