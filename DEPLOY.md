# FMEA Agent — Docker 部署指南

## 部署方式

**镜像自带 .env（含 API Key），构建后可直接导出到任何服务器运行，无需额外配置。**

---

### 方式一：服务器上直接构建（最简单）

```bash
# 1. 将项目传送到服务器（或 git clone）
# 2. 编辑 .env 填入 API Key
# 3. 构建并启动
chmod +x docker-build.sh
./docker-build.sh
```

---

### 方式二：本地构建，导出镜像到离线服务器（推荐跨网传输）

在**能联网的机器**上构建并导出：

```bash
# 1. 编辑 .env 填入 API Key
# 2. 构建并导出（--export 只构建不启动）
chmod +x docker-build.sh
./docker-build.sh --export
# 生成 fmea-agent.tar.gz

# 3. 传到目标服务器
scp fmea-agent.tar.gz user@target-server:/opt/
```

在**目标服务器**上导入运行：

```bash
# 1. 导入镜像
docker load < /opt/fmea-agent.tar.gz

# 2. 直接启动（无需 .env、无需项目文件）
docker run -d --name fmea-agent --restart unless-stopped -p 7777:7777 fmea-agent:latest

# 3. 验证
curl http://localhost:7777/health
```

---

### 方式三：重新构建（覆盖 .env）

如果 API Key 变化，只需修改 .env 后重新构建即可：

```bash
# 编辑 .env → 填入新的 API Key
./docker-build.sh --no-cache
```

---

## 环境变量说明

所有配置在 `.env` 文件中设置（构建时打包进镜像）：

| 变量名 | 说明 | 示例 |
|---|---|---|
| `FMEA_MODEL_API_KEY` | LLM API Key | `sk-xxxx` |
| `FMEA_MODEL_NAME` | 模型名称 | `deepseek-chat`, `gpt-4o` |
| `FMEA_MODEL_BASE_URL` | API 地址 | `https://api.deepseek.com` |
| `FMEA_HOST` | 监听地址（容器内必须为 `0.0.0.0`） | `0.0.0.0` |
| `FMEA_PORT` | 监听端口 | `7777` |
| `LOG_LEVEL` | 日志级别 | `INFO`, `DEBUG` |

---

## 常用命令

```bash
# 查看运行状态
docker ps | grep fmea-agent

# 查看实时日志
docker logs -f fmea-agent

# 停止/启动/重启
docker stop fmea-agent
docker start fmea-agent
docker restart fmea-agent

# 进入容器内部
docker exec -it fmea-agent /bin/bash

# 彻底删除
docker rm -f fmea-agent
```

---

## 注意事项

1. **镜像便携**：`.env` 包含在镜像内，`docker save` → `scp` → `docker load` → `docker run` 即可运行，无需额外文件
2. **基础数据在镜像内**：构建时 `data/`、`tmp/` 等目录已被打包进镜像（含项目文件等基础数据）
3. **首次启动自动保留基础数据**：使用 Docker named volume，首次启动时镜像内的基础数据会自动拷贝到 volume 中，不会被清空
4. **数据持久化**：应用运行中新产生的数据会持久化在 volume 中，容器删除/重建不会丢失
5. **端口冲突**：确保服务器 7777 端口未被占用，或通过 `-p 其他端口:7777` 映射
6. **网络需求**：运行时需联网调用 LLM API
7. **镜像源**：默认使用清华镜像源（pip + apt 双加速），海外服务器构建时加：
4. **网络需求**：运行时需联网调用 LLM API
5. **镜像源**：默认使用清华镜像源（pip + apt 双加速），海外服务器构建时加：
   ```bash
   docker build \
     --build-arg PIP_INDEX_URL=https://pypi.org/simple \
     --build-arg DEBIAN_MIRROR=deb.debian.org \
     -t fmea-agent:latest .
   ```
