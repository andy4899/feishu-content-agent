# 飞书内容生成 Agent · 部署指南

## 一、飞书应用权限配置

在 open.feishu.cn 你的应用中，进入「权限管理」添加以下权限：

**消息权限：**
- `im:message` — 读取消息
- `im:message:send_as_bot` — 发送消息

**文档权限：**
- `docx:document` — 读写飞书文档
- `drive:drive` — 访问云空间（存放文档）

**事件订阅：**
进入「事件订阅」→ 添加 `im.message.receive_v1`

**请求 URL（部署后填入）：**
`https://你的Railway域名.railway.app/webhook`

---

## 二、环境变量（Railway Variables）

| 变量名 | 说明 |
|--------|------|
| `FEISHU_APP_ID` | 飞书应用 App ID |
| `FEISHU_APP_SECRET` | 飞书应用 App Secret |
| `FEISHU_FOLDER_TOKEN` | 文档存储目录 token（可选，留空存根目录）|
| `ANTHROPIC_API_KEY` | Anthropic API Key |
| `CLAUDE_MODEL` | 默认 `claude-sonnet-4-6`，改为 `claude-opus-4-8` 更强 |

---

## 三、Railway 部署步骤

```bash
# 1. 进入项目目录
cd ~/Desktop/feishu-content-agent

# 2. 初始化 git（如未初始化）
git init && git add . && git commit -m "init"

# 3. 登录并部署
railway login
railway init        # 选择 New Project
railway up          # 上传代码

# 4. 添加环境变量
railway variables set FEISHU_APP_ID=cli_xxx
railway variables set FEISHU_APP_SECRET=xxx
railway variables set ANTHROPIC_API_KEY=sk-ant-xxx

# 5. 获取部署域名
railway domain
```

---

## 四、使用方式（飞书机器人消息）

| 发送内容 | 效果 |
|---------|------|
| `法考 A 1` | 从痛点池自动取考点，赛道A，策略1种草 |
| `法考 B 3` | 赛道B，策略3鼓励 |
| `小红书 【ADHD启动困难】` | ADHD话题，生成4个标题供选择 |
| `小红书 《你为什么动不了》` | 已有标题，直接生成正文+图片提示词 |
| `劳动法 【试用期被辞退】 A 是` | 员工端，正文带出知识库 |
| `劳动法 【考勤管理漏洞】 B 否` | 企业端，纯方法不提产品 |

**多轮对话：**
1. 发送指令 → Bot 返回4个标题
2. 回复 `1` / `2` / `3` / `4` 或 `用你推荐的`
3. Bot 生成正文+图片提示词+质检清单，自动创建飞书文档并发链接

---

## 五、痛点池管理（法考专用）

法考默认内置30条考点，用完后自动 Claude 补充。

手动批量补充（POST 请求）：
```bash
curl -X POST https://你的域名.railway.app/admin/refill-pool \
  -H "Content-Type: application/json" \
  -d '{"topics": ["考点1", "考点2", ...]}'
```

查看当前状态：
```bash
curl https://你的域名.railway.app/health
```

---

## 六、获取 FEISHU_FOLDER_TOKEN（可选）

在飞书云空间中，进入你想存放文档的文件夹，URL 中 `/folder/` 后的字符串即为 token。
例：`https://xxx.feishu.cn/drive/folder/AbCdEfGh` → token 为 `AbCdEfGh`
