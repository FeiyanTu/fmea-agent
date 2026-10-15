# FMEA Agent

> AI 辅助 FMEA 分析平台：把 SOR 需求文档交给系统，AI 引导你一步步完成 DFMEA/PFMEA 七步分析，最终生成可直接使用的 FMEA 报告并导出 Excel。

## 项目简介

FMEA Agent 是一个面向汽车/制造行业的 AI 辅助失效模式与影响分析平台。它基于 LLM（DeepSeek / OpenAI 兼容接口）与多智能体（Multi-Agent）架构，将传统 FMEA 分析中大量重复、耗时的工作（结构拆解、功能梳理、失效链推演、风险评分、优化措施生成）自动化，并内置了步骤审批、工程师指派、飞书通知等团队协作能力。

全程以「对话」方式操作：对 AI 说想做什么，AI 自动执行并把结果展示在工作台中间区域。

## 核心功能

| 能力 | 说明 |
|---|---|
| 自动拆解结构树 | 从 SOR 文档（PDF/Excel）自动提取 系统 → 子系统 → 组件 → 零件 四层结构；BOM 表走「BOM 优先」策略，先确定性解析表格再由 LLM 凝结为结构树 |
| 自动功能分析 | 每个节点功能由多个 AI 专家并发分析，自动绑定相关法规；并识别零部件之间的界面关系（界面作为一种功能，归属到主要零部件） |
| 自动失效分析 | 识别每条功能的失效链（失效影响 → 失效模式 → 失效原因 → 失效控制） |
| 标准化风险评估 | 按公司 S/O/D 评分标准 + AP 表自动计算 RPN 与操作优先级 |
| 自动生成优化措施 | 对高风险项自动生成预防/探测措施、目标完成日期，并指派责任人 |
| 一键出报告 | 汇总全部数据生成 DFMEA 报告，一键导出 Excel |
| 全程可协作 | 步骤确认、审批、工程师指派、飞书卡片通知，全流程留痕 |
| 角色权限 | 项目负责人 / 工程师 / 评审员 / 管理员 四级角色与团队管理 |

## FMEA 七步法

| 步骤 | 名称 | 说明 |
|---|---|---|
| Step 0 | 规划准备 | 创建项目（项目名 = 分析对象） |
| Step 1 | 结构分析 | 生成产品结构树 |
| Step 2 | 功能分析 | 每个节点做什么 |
| Step 3 | 失效分析 | 可能怎么坏、影响什么 |
| Step 4 | 风险评估 | S/O/D 评分、RPN、AP 优先级 |
| Step 5 | 优化措施 | 预防/探测措施 + 责任人 + 完成日期 |
| Step 6 | 文件化 | 汇总报告 + Excel 导出 |

## 技术架构

- **后端**：Python 3.13 + FastAPI + Uvicorn（SSE 流式输出）
- **Agent 框架**：agno（本地源码注册，离线可用）
- **数据库**：PostgreSQL（业务数据 + 用户/角色/审批，`db/migrations/` 提供建表脚本）
- **LLM**：DeepSeek / OpenAI 兼容 API（`FMEA_MODEL_BASE_URL` 可切换）
- **文档解析**：PyMuPDF / pypdf / python-docx（SOR 文档解析）
- **通知通道**：飞书开放平台应用（`im` 卡片消息，按责任人邮箱聚合、一人一条，不依赖邮箱授权码）
- **前端**：原生 HTML + JS（无构建步骤），含登录、工作台、团队、智能体管理页面

### 页面入口

| 路由 | 页面 |
|---|---|
| `/` | 首页 / 工作台入口 |
| `/login` | 登录 |
| `/chat` | FMEA 工作台（核心页面：项目列表 + 七步导航 + 内容区 + AI 对话） |
| `/agent` | 智能体管理 |
| `/team` | 团队管理 |

## 目录结构

```
agent-fmea/
├── fmea-agent/              # 主程序
│   ├── main.py              # 服务入口（FastAPI 应用，python main.py 启动）
│   ├── .env.example         # 环境变量模板（复制为 .env 并填写）
│   ├── requirements.txt     # 依赖清单
│   ├── agents/              # 各步骤 Agent（planning/function/failure/risk/optimization/document）
│   ├── engine/              # LLM 客户端、并发执行、质量自检、工具（fmea_helpers / feishu_push 等）
│   ├── orchestrator/        # 工作流编排、多智能体调度、步骤门禁
│   ├── workflow/            # FMEA 流水线、增量更新
│   ├── models/              # 数据模型（agent_config / approval / failure 等）
│   ├── tools/               # 工具集
│   ├── db/migrations/       # PostgreSQL 建表/变更 SQL
│   ├── deploy/              # systemd 服务、nginx 配置、离线安装脚本
│   ├── config/              # 项目配置（含示例 hev_battery.yaml）
│   ├── restart-linux.sh     # Linux 一键重启（按端口动态找 PID → 优雅停止 → 后台启动 → 健康检查）
│   └── *.html               # 前端页面（login / chat / agent / team / index）
├── Dockerfile               # 镜像构建（镜像内自带 .env，便携部署）
├── docker-compose.yml
├── docker-build.sh          # 构建/导出脚本（--export 导出离线镜像）
├── deploy-fast.sh / deploy-fast.bat   # 快速部署脚本
├── install-linux.sh         # Linux 一键安装
├── update-server.sh         # 服务器代码更新
├── full-requirements.txt    # 完整离线依赖清单
├── QUICK-START-Linux.md     # Linux 极简安装指南
├── DEPLOY.md                # Docker 部署指南
├── DEPLOY-SOP.md            # 完整部署 SOP（Windows → Linux 离线）
└── 更新手册.md              # 日常代码更新手册
```

## 快速开始

### 1. 环境变量

```bash
cd fmea-agent
cp .env.example .env
```

`.env` 主要配置项：

| 变量 | 说明 | 示例 |
|---|---|---|
| `FMEA_MODEL_API_KEY` | LLM API Key | `sk-xxxx` |
| `FMEA_MODEL_NAME` | 模型名称 | `deepseek-chat` / `gpt-4o` |
| `FMEA_MODEL_BASE_URL` | API 地址 | `https://api.deepseek.com` |
| `FMEA_DB_URL` | PostgreSQL 连接串 | `postgresql+psycopg2://user:pass@host:5432/dbname` |
| `FMEA_HOST` | 监听地址 | `0.0.0.0`（容器内必填） |
| `FMEA_PORT` | 监听端口 | `7777` |
| `LOG_LEVEL` | 日志级别 | `INFO` / `DEBUG` |
| `FEISHU_APP_ID` | 飞书应用 App ID（通知通道） | `cli_xxxxxxxx` |
| `FEISHU_APP_SECRET` | 飞书应用 App Secret | `xxxxxxxx` |

> `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 为飞书通知通道凭证，未配置时推送会静默跳过（不影响主流程）。需在飞书开放平台开通 `im:message` 权限，并把收件人纳入应用「可用范围」。

### 2. 安装依赖并启动（本地开发）

```bash
cd fmea-agent
pip install -r requirements.txt

# 初始化数据库：执行 db/migrations/ 下的 SQL 脚本

python main.py
# 浏览器访问 http://localhost:7777
```

> 注意：agno 框架需本地注册（`agno/libs/agno`），`.env` 中可通过 `AGNO_PATH` 指定路径；若缺失会自动探测项目上级目录的 `agno/`。

### 3. Docker 部署（最简单）

```bash
# 编辑 .env 填入 API Key → 构建并启动
chmod +x docker-build.sh
./docker-build.sh

# 验证
curl http://localhost:7777/health
```

镜像内置 `.env`（含 API Key），可通过 `docker save` + `docker load` 直接搬运到任何服务器运行：

```bash
# 本地构建导出
./docker-build.sh --export   # 生成 fmea-agent.tar.gz

# 目标服务器导入运行
docker load < fmea-agent.tar.gz
docker run -d --name fmea-agent --restart unless-stopped -p 7777:7777 fmea-agent:latest
```

### 4. Linux 离线部署

适用于无外网的服务器，详见 `QUICK-START-Linux.md` 与 `DEPLOY-SOP.md`：

1. Windows 上准备离线依赖包（`wheels/` + `full-requirements.txt`）
2. 将整个 `agent-fmea` 文件夹拷到服务器（scp / U 盘）
3. 服务器执行 `chmod +x install-linux.sh && ./install-linux.sh`
4. 配置 `.env`（必改 4 项；如需飞书通知，另填 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`）
5. PostgreSQL 建库（执行 `db/migrations/` SQL）
6. 后台启动：`nohup python main.py >> logs/server.log 2>&1 &`
7. 访问 `http://服务器IP:7777`

## 使用流程（10 分钟跑通）

1. 浏览器访问系统地址（默认 `http://localhost:7777`），用管理员分配的账号登录
2. 进入「FMEA 工作台」，在右侧对话框输入：
   > 「创建一个 FMEA 项目，名称：3_6KW，类型：DFMEA，描述：低压电池包」
3. 等待 AI 创建项目，左侧项目列表出现该项目
4. 从 Step 1 结构分析开始逐级推进（先「初始化项目知识库，SOR 文件夹路径：...」）
5. 最后输入「生成报告」，在报告页面下载 Excel

> 各角色详细操作、审批流程、风险评分标准配置见 `fmea-agent/FMEA-Agent 使用手册.md`。

## 常见问题

- **端口冲突**：确保 7777 端口未被占用，或通过 `-p 其他端口:7777` 映射
- **日志**：`logs/fmea-agent.log`（或 `logs/server.log`），可用 `docker logs -f fmea-agent` 查看容器日志
- **数据持久化**：Docker 使用 named volume，容器删除/重建不会丢失运行数据
- **换模型**：修改 `.env` 中 `FMEA_MODEL_NAME` / `FMEA_MODEL_BASE_URL` / `FMEA_MODEL_API_KEY` 后重启服务
- **飞书通知没发出去**：先确认 `.env` 的 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 已填（未配置时推送静默跳过，只在日志留痕，不影响主流程）；再排查飞书应用的「可用范围」是否覆盖收件人、是否已开通 `im:message` 权限。**发送失败优先怀疑应用可用范围，而不是权限 scope**

## 版本更新（近期）

**v21.0 · 通知通道**
- 工程师通知由 SMTP 邮件改为飞书应用卡片消息：按责任人邮箱聚合、一人一条，不再需要邮箱授权码（`leader_auth_code` 已废弃，前端不再采集）

**v24.2 / v25.0 / v26.2 · 结构树与加载稳定性**
- 结构树中删除的节点会记录并在保存时提交，不再在保存 merge 时从数据库基线「复活」
- 撤销指派的写回键与读取键对齐（统一为 `structure`）
- 工作台数据加载加入请求序号（并发只认最后一次）、超时由 2s 上调至 15s 并加 30s 总看门狗；推送状态查询加 8s 超时与 `AbortController`，修掉面板长期停在「⏳ 加载中…」

**v26.x · 零部件界面关系**
- 功能分析新增零部件间界面关系：界面作为一种功能，归属到主要零部件，并沿用既有功能表 / 推送 / 失效分析链路

**v4.2 · 结构树提取策略**
- 改为「BOM 优先」：先对 BOM 表格做确定性解析，再由单次 LLM 调用凝结为「产品 → 总成 → 零部件」结构树；点击即重新生成覆盖

**界面与部署**
- 团队管理（v24.3 / v24.4）：成员列表展示登录名与初始密码提示；操作列吸右、表格支持横向滚动，窄屏也能看到删除按钮
- FMEA 工作台：左侧项目列表改为撑满侧栏剩余高度、内部滚动，修掉「有项目时仍显示暂无项目」的重复占位
- 新增 `fmea-agent/restart-linux.sh` 一键重启脚本；`deploy-fast.sh` / `update-server.sh` / `DEPLOY-*.md` / `更新手册.md` / `.env.example` 同步

## License

详见 agno 框架 LICENSE 与本仓库说明。
