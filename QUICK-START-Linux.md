# FMEA-Agent 极简安装（Linux）—— 整个文件夹拷贝版

## 你需要做的（就 3 件事）

1. **Windows 上**：准备离线依赖包（1 条命令）
2. **拷贝**：把 `D:\agent-fmea` 整个文件夹放到服务器
3. **服务器上**：跑一个脚本 + 配 .env + 后台启动

> 为什么不能直接用拷过去的 `.venv`：Windows 的 Python 虚拟环境是 Windows 二进制，Linux 跑不了，**必须在服务器上重建一次**（一次性，由脚本自动完成，以后不再需要）。

---

## 前提

- 服务器：Linux x86_64，**Python 3.12**（Ubuntu 24.04 自带；没有就让运维装或直接换 Ubuntu 24.04 镜像）
- **PostgreSQL 已安装**（没有就让运维装一下，或选自带 PG 的镜像）
- 拷贝前建议先在 Windows 上删掉没用的 Windows 虚拟环境（省流量）：
  ```bash
  rm -rf /d/agent-fmea/agno/.venv
  ```

---

## 第 1 步（Windows 上，只做一次）：准备离线依赖包

```bash
cd /d/agent-fmea
python -m pip download -r fmea-agent/requirements.txt -d wheels \
  --only-binary=:all: \
  --platform manylinux2014_x86_64 \
  --python-version 312
```

- 生成一个 `wheels/` 文件夹（放在 agent-fmea 里面，拷贝时自动带上）
- 服务器**能联网的话跳过这步**，脚本会自动走在线安装
- 如果报「no matching distribution」缺某个包，就把命令里的包名单独补：`python -m pip download <包名> -d wheels --platform manylinux2014_x86_64 --python-version 312`

## 第 2 步：把整个文件夹拷到服务器

```bash
# 方式一：scp
scp -r /d/agent-fmea user@服务器IP:/opt/
# 方式二：U 盘 / 内网共享盘拷贝到 /opt/agent-fmea
```

## 第 3 步：服务器上执行一键安装脚本

```bash
ssh user@服务器IP
cd /opt/agent-fmea
bash install-linux.sh
```

脚本自动完成：重建 venv → 本地注册 agno（用文件夹里自带的源码）→ 安装全部依赖 → 生成 .env 模板。看到 `agno 2.6.11 OK` 即成功。

## 第 4 步：配置 .env（必改 4 项）

```bash
cd /opt/agent-fmea/fmea-agent
vim .env
```

```ini
FMEA_MODEL_API_KEY=sk-你的key        # ① DeepSeek key
FMEA_DB_URL=postgresql+psycopg2://fmea:数据库密码@localhost:5432/fmea   # ② 数据库连接
URL_AGENT_DB_PASSWORD=数据库密码      # ③ 与上面一致
FMEA_HOST=0.0.0.0                    # ④ 必须 0.0.0.0，外部才能访问
```

## 第 5 步：建库（PostgreSQL 里执行一次）

```bash
sudo -u postgres psql -c "CREATE USER fmea WITH PASSWORD '数据库密码'; CREATE DATABASE fmea OWNER fmea ENCODING 'UTF8';"
```

> 建表不用管，首次启动自动创建。

## 第 6 步：后台启动

```bash
cd /opt/agent-fmea/fmea-agent
nohup .venv/bin/python main.py >> logs/server.log 2>&1 &
echo $! > logs/server.pid
tail -f logs/server.log
```

看到 `✅ 已创建默认管理员账号: admin / admin123` 就是成功了，`Ctrl+C` 退出日志（不影响后台进程）。

## 第 7 步：访问

浏览器打开 **`http://服务器IP:7777/`**，账号 `admin` / `admin123`（登录后立即改密码）。

防火墙放行（二选一）：
```bash
sudo ufw allow 7777/tcp                                    # Ubuntu
sudo firewall-cmd --permanent --add-port=7777/tcp && sudo firewall-cmd --reload   # CentOS
```

---

## 日常运维命令

```bash
tail -f /opt/agent-fmea/fmea-agent/logs/server.log   # 看日志
kill $(cat /opt/agent-fmea/fmea-agent/logs/server.pid)  # 停止
# 重启 = 停止后再跑第 6 步
```

**开机自启（可选）**：`crontab -e` 加一行
```
@reboot cd /opt/agent-fmea/fmea-agent && nohup .venv/bin/python main.py >> logs/server.log 2>&1 &
```

---

## 常见问题

| 现象 | 处理 |
|---|---|
| 启动报 `ModuleNotFoundError: agno.os` | agno_infra 没注册上，重跑 `bash install-linux.sh` |
| pip 报找不到某个包 | wheels 缺包，Windows 上补下后重新拷贝 wheels 目录 |
| 外部访问不了 | ① `FMEA_HOST` 是否 `0.0.0.0`；② 防火墙 7777 是否放行 |
| 改了代码/页面不生效 | 重启服务；浏览器 **Ctrl+Shift+R** 强制刷新 |
| 服务器重启后服务没了 | 配置上面那行 crontab 开机自启 |
