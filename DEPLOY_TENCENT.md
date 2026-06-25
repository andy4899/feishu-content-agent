# 腾讯云部署指南（轻量服务器 + Docker + Caddy）

把飞书内容生成 Agent 从 Railway 迁到腾讯云。飞书机器人是常驻 webhook 服务，国内轻量服务器访问飞书 API 延迟低、无额度限制。

## 架构

```
飞书 ──HTTPS──> Caddy(:443, 自动证书) ──> app(:8000, uvicorn main:app)
                                              │
                                         ./data (痛点池持久化)
```

- **Caddy**：自动 HTTPS（Let's Encrypt），反代到 app
- **app**：FastAPI 飞书机器人，uvicorn 跑在容器里
- **./data**：痛点池 JSON 持久化卷，重启/重新部署不丢选题

---

## 前置准备（你需要先有）

1. **腾讯云轻量应用服务器**（推荐 Ubuntu 22.04 / 24.04，2 核 2G 起步够用，约 ¥24-68/月）
   - 控制台 → 防火墙，开放端口 **80、443**（Caddy 用）；SSH 的 22 默认开
2. **一个域名**（已备案的域名可直接用 80/443；未备案域名只能用非标端口如 8443，飞书也支持）
   - 域名 DNS A 记录解析到服务器公网 IP
3. **飞书应用凭证**（飞书开放平台 → 你的应用 → 凭证与基础信息）
   - `FEISHU_APP_ID`、`FEISHU_APP_SECRET`
4. **GLM API Key**（智谱开放平台 → API keys），以及 Anthropic 兼容端点地址

---

## 部署步骤

### 1. SSH 登服务器，装 Docker

```bash
ssh ubuntu@你的服务器IP

# 装 Docker + Compose 插件（Ubuntu）
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker   # 立即生效，免重新登录

docker --version && docker compose version   # 确认装好
```

### 2. 拉代码

```bash
cd ~
git clone https://github.com/andy4899/feishu-content-agent.git
cd feishu-content-agent
```

### 3. 配环境变量

```bash
cp .env.example .env
nano .env   # 填入真实凭证
```

填这几项（其余按 .env.example 注释）：
```
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
ANTHROPIC_API_KEY=你的GLM-key
ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic
ANTHROPIC_MODEL=glm-5.1
```

### 4. 配 Caddy（填域名）

```bash
cp Caddyfile.example Caddyfile
nano Caddyfile   # 把 your-domain.com 改成你的域名
```

### 5. 启动

```bash
mkdir -p data          # 痛点池持久化目录
docker compose up -d --build
docker compose logs -f app   # 看启动日志，确认 "Application startup complete"
```

启动后先自测：
```bash
curl http://localhost:8000/health   # 应返回 {"status":"ok",...}
curl https://你的域名/health        # 验证 Caddy + HTTPS（首次会自动签证书，稍等几十秒）
```

### 6. 飞书后台改回调地址

飞书开放平台 → 你的应用 → **事件与回调** → 事件配置：
- 请求地址改成：`https://你的域名/webhook`
- 点验证 → 应返回成功（代码里有 `url_verification` 的 challenge 应答）

> 改完这一步，飞书消息就会打到腾讯云这个新服务。**原 Railway 服务可以同时保留着当备份**，确认新服务稳定后再停 Railway。

### 7. 实测

在飞书给机器人发：
- `帮助` → 看使用说明
- `公众号贴图` → 验证新增的贴图档（自动选题 → 生成）
- `小红书` / `公众号` / `法考` / `劳动法` → 验证各档

---

## 日常运维

```bash
cd ~/feishu-content-agent

# 看日志
docker compose logs -f app

# 更新代码（拉 GitHub 最新 + 重建）
git pull && docker compose up -d --build

# 重启（不重建，痛点池保留）
docker compose restart app

# 停止 / 启动
docker compose down
docker compose up -d
```

痛点池存在 `./data/painpool_*.json`，`down`/重新 `build` 都不丢。

## 常见问题

- **Caddy 证书签不下来**：检查域名 DNS 是否解析到本机、80/443 端口是否在腾讯云防火墙开放。
- **飞书验证 webhook 失败**：确认地址是 `https://你的域名/webhook`（带 `/webhook` 路径），且 `curl https://你的域名/health` 能通。
- **生成报错**：`docker compose logs app` 看异常，多半是 `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` 没填对。
- **飞书 3 秒超时**：代码用 BackgroundTasks 异步处理、webhook 秒回 `code:0`，正常不会超时；若模型响应慢导致后续消息堆积，检查 GLM 接口延迟。
