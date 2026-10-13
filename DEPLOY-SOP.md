# FMEA-Agent 完整部署 SOP（本地 Windows → 服务器 Linux 离线）

> 适用环境（已实测确认）：
> - 本地：Windows（有网），代码在 `D:\agent-fmea`
> - 服务器：Linux，Python 3.13（conda base），**离线**，项目在 `/data/demo/agent-fmea`
> - 依赖离线包：`D:\agent-fmea\wheels`（156 个 manylinux cp313 wheel）＋ `full-requirements.txt`
> - agno：源码本地注册（`agno/libs/agno` + `agno/libs/agno_infra`），已装好
> - 服务：nohup 后台运行，日志 `logs/server.log`，PID 在 `logs/server.pid`

---

## 总览

```
阶段一  本地准备依赖（仅依赖变化时执行）
   └─ 更新 full-requirements.txt → pip download 补新 wheel → 上传 → 服务器补装
阶段二  本地打包代码（每次更新都执行）
   └─ tar 打包 fmea-agent（排除 数据/配置/环境）
阶段三  上传到服务器
阶段四  服务器解压覆盖 + 重启 + 验证
```

> 日常代码更新：**只需阶段二 → 三 → 四**（约 2 分钟）。
> 改了依赖（新增 import 第三方库 / 改了 requirements.txt）：才需要阶段一。

---

## 阶段一：本地准备依赖（仅依赖变化时）

**什么时候需要**：`requirements.txt` 变了，或代码里新增了 `import` 第三方包。

### 1. 重新生成完整依赖清单（以本地可运行环境为准）

```bash
cd /d/agent-fmea
D:/agent-fmea/agno/.venv/Scripts/python.exe -m pip freeze \
  | grep -vE "^-e |^agno(-infra)? @|^pip==" > full-requirements.txt
printf "setuptools\nwheel\n" >> full-requirements.txt
```

> 排除项说明：`-e`/`agno@` 是本地源码注册的 agno（服务器走源码，不装）；`pip` 不需要；`setuptools/wheel` 是 agno 源码构建需要。

### 2. 全量下载（或增量补）Linux wheel

```bash
cd /d/agent-fmea
python -m pip download -r full-requirements.txt -d wheels \
  --only-binary=:all: \
  --platform manylinux2014_x86_64 \
  --python-version 313
```

> ⚠️ `--python-version 313` 必须对应服务器 Python（3.13）。缺包就报错、单独 `pip download <包名> ...` 补。
> 已知坑：`greenlet` 最高 3.2.5（3.5.1 无 cp313 版）；`uvloop`（uvicorn[standard] 依赖，仅 Linux 需要）需单独补：
> `python -m pip download uvloop --only-binary=:all: --platform manylinux2014_x86_64 --python-version 313 -d wheels`

### 3. 上传并到服务器补装

```bash
scp -r /d/agent-fmea/wheels appuser@服务器IP:/data/demo/agent-fmea/
scp /d/agent-fmea/full-requirements.txt appuser@服务器IP:/data/demo/agent-fmea/
```

```bash
# 服务器上
cd /data/demo/agent-fmea/fmea-agent
.venv/bin/pip install --no-index --find-links=/data/demo/agent-fmea/wheels -r /data/demo/agent-fmea/full-requirements.txt
```

---

## 阶段二：本地打包代码（每次更新必做）

```bash
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
  -czf fmea-agent-code.tar.gz fmea-agent
```

> **排除清单是重点，缺一不可**：
> - `data/`、`knowledge/`：服务器上的业务数据 / 知识库，覆盖 = 丢数据
> - `.env`：服务器专属配置（数据库密码、key），覆盖 = 连不上库
> - `.venv`、`logs/`、`tmp/`、`__pycache__`：服务器环境与运行产物
>
> 结果只包含代码：`*.py`、`*.html`、`static/`、`deploy/` 等，约 1~3 MB。

---

## 阶段三：上传

```bash
scp /d/agent-fmea/fmea-agent-code.tar.gz appuser@服务器IP:/data/demo/agent-fmea/
```

---

## 阶段四：服务器更新 + 重启（核心）

### 方式一：一键脚本（推荐）

```bash
ssh appuser@服务器IP
cd /data/demo/agent-fmea
bash update-server.sh
```

脚本自动完成：停旧服务 → 备份旧代码 → 解压覆盖（不动数据/配置）→ 清缓存 → 启动 → 健康检查。脚本见项目根 `update-server.sh`。

### 方式二：手动执行（不依赖脚本）

```bash
cd /data/demo/agent-fmea

# ① 停旧服务
kill $(cat fmea-agent/logs/server.pid) 2>/dev/null && echo "已停止" || echo "服务未在运行"

# ② 解压覆盖（自动保留 data/knowledge/.env/.venv）
tar -xzf fmea-agent-code.tar.gz --strip-components=1

# ③ 清理 pyc 缓存（避免旧字节码）
find fmea-agent -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find fmea-agent -name "*.pyc" -delete 2>/dev/null || true

# ④ 启动
cd fmea-agent
nohup .venv/bin/python main.py >> logs/server.log 2>&1 &
echo $! > logs/server.pid

# ⑤ 验证
sleep 3
curl -s http://localhost:7777/health && echo "  <- 服务正常" || echo "!! 健康检查失败"
tail -5 logs/server.log
```

### 验证（更新后必做）

| 检查项 | 命令/操作 | 期望 |
|---|---|---|
| 健康检查 | `curl -s http://localhost:7777/health` | 200 |
| 日志无异常 | `tail -20 logs/server.log` | 无 Traceback |
| 页面新代码生效 | 浏览器 **Ctrl+Shift+R** 强刷 | 看到新功能 |
| AI 正常 | 聊天框问一句话 | 有回复 |

---

## 回滚（更新出问题时）

```bash
cd /data/demo/agent-fmea
# ① 看有哪些备份
ls backup/

# ② 用最新备份恢复代码（备份在阶段四自动生成：backup/时间戳/code.tar.gz）
kill $(cat fmea-agent/logs/server.pid) 2>/dev/null
tar -xzf backup/最新时间戳/code.tar.gz --strip-components=1
cd fmea-agent && nohup .venv/bin/python main.py >> logs/server.log 2>&1 &
echo $! > logs/server.pid
sleep 3 && curl -s http://localhost:7777/health
```

---

## 首次部署（还没装过时）

按 `QUICK-START-Linux.md`：传整个文件夹 → `bash install-linux.sh` → 配 .env → 建库 → 启动。
首次部署后，后续更新一律走本 SOP 的阶段二~四。

---

## 常见问题

| 现象 | 处理 |
|---|---|
| 更新后页面还是旧功能 | 浏览器 **Ctrl+Shift+R** 强刷；仍不行查日志是否启动失败 |
| 更新后 500 / 启动报 Traceback | `tail -50 logs/server.log` 看缺什么；若缺第三方包 → 阶段一补依赖 |
| 误操作覆盖了 data/.env | 用 `backup/时间戳/code.tar.gz` 只能恢复代码；**data/.env 无备份**（设计如此，务必排除干净） |
| 健康检查失败 | 看日志；常见：端口被占（`kill $(cat logs/server.pid)` 后重启）、.env 被覆盖（检查 FMEA_DB_URL/FMEA_HOST） |
| 服务器重启后服务没了 | 手动重新启动，或配置 crontab：`@reboot cd /data/demo/agent-fmea/fmea-agent && nohup .venv/bin/python main.py >> logs/server.log 2>&1 &` |
