# CLAUDE.md

meeting-agent 是一个自托管的 AI 会议助手系统（Vexa Lite），将 Vexa（会议机器人平台）与 Hermes/Cursor agent（AI 命令行客户端）桥接起来，支持 Google Meet 自动加入、语音识别 (ASR)、文本转语音 (TTS)、AI 对话与会议聊天转发。

## 技术栈

| 组件 | 语言 | 说明 |
|------|------|------|
| bridge.py | Python 3 | 核心桥接服务器，HTTP API + TTS + Agent 子进程调用 |
| asr-shim/ | Node.js 20+ | Doubao ASR 2.0 语音转写代理（OpenAI 兼容 API） |
| scripts/redis-chat-forward/ | Node.js 22 | Redis 订阅者，转发 Meet 聊天到 bridge |
| vexa-lite | Docker | 会议机器人平台（vexaai/vexa-lite:latest） |
| PostgreSQL | Docker | Vexa 数据库（postgres:16） |

## 常用命令

```bash
# 启动桥接
./start-bridge.sh                       # 从 .env 加载配置，启动 bridge.py（后台）
python3 bridge.py                       # 直接运行（前台）

# ASR Shim
cd asr-shim && npm run start            # 启动语音转写服务（端口 8787）
cd asr-shim && npm run dev              # 热重载开发模式
cd asr-shim && npm run preflight        # Doubao ASR 连接性检查

# Docker 服务
docker compose up -d                    # 启动所有服务
docker compose up -d chat-forward       # 仅启动聊天转发
docker compose up -d asr-shim           # 仅启动 ASR 代理
docker logs -f vexa-chat-forward        # 查看聊天转发日志

# 测试
./scripts/test-cursor-agent-resume.sh   # Cursor agent 会话续订测试
```

## 项目结构

```
bridge.py               # 核心：HTTP 服务器（端口 8765），Vexa ↔ Agent 桥接
session_policy.py       # 会话标识策略（new/resume/resume_ttl）
start-bridge.sh         # 桥接启动脚本
docker-compose.yml      # 4 服务编排（postgres/vexa/chat-forward/asr-shim）

asr-shim/
├── src/server.js       # ASR 服务主程序（Doubao WebSocket 二进制协议）
├── src/preflight.js    # ASR 连接性诊断工具
├── Dockerfile          # 基于 node:20-bookworm-slim + ffmpeg
└── .env.example        # VOLC_APP_KEY, VOLC_ACCESS_KEY 等
scripts/
├── redis-chat-forward/ # Redis 聊天转发器（订阅 vexa 频道 → bridge API）
└── test-cursor-agent-resume.sh
docs/
├── vexa-chat-to-bridge.md          # 聊天转发技术文档
└── 会议Agent讨论总结.md             # 产品讨论纪要
patches/
└── vexa-chat-forward-snippet.ts.txt # 上游 Vexa 聊天转发代码片段
workspace/              # Agent 工作空间（可持久化文件）
```

## 环境变量

配置通过 `.env` 文件（与 `start-bridge.sh` 同目录），关键变量：

| 变量 | 说明 |
|------|------|
| `VEXA_API_BASE` / `VEXA_API_KEY` | Vexa 平台连接 |
| `PLATFORM` / `MEETING_ID` | 目标会议平台和会议码 |
| `HERMES_BIN` / `HERMES_MODEL` / `HERMES_PROVIDER` | AI 模型 CLI 路径与配置 |
| `HERMES_PRIMER` | Agent 系统提示词 |
| `HERMES_TIMEOUT_S` | Agent 单次调用超时（秒） |
| `HERMES_WAKE_WORDS` | 唤醒词列表，触发后捕捉语音 |
| `HERMES_AUTO_REPLY` | VAD（语音活动检测）后自动回复 |
| `HERMES_SESSION_POLICY` | 会话策略：new / resume / resume_ttl |
| `TTS_PROVIDER` | TTS 引擎：piper / doubao |
| `DOUBAO_APPID` / `DOUBAO_ACCESS_TOKEN` / `DOUBAO_VOICE` | 豆包 TTS 配置 |
| `BRIDGE_CHAT_WEBHOOK_SECRET` | 聊天 webhook 签名密钥 |
| `CURSOR_AGENT_WORKSPACE` | Cursor agent 工作空间路径 |

完整变量列表见 `bridge.py` 顶部注释。

## 系统架构

```
用户浏览器/CLI → bridge.py (8765) ←→ Hermes/Cursor CLI (AI)
                     ↕ HTTP API
                vexa-lite Docker ←→ PostgreSQL
                     ↕ Redis pub/sub
       redis-chat-forward → bridge.py → AI 回复 + TTS + 聊天
```
```
         asr-shim (8787, Doubao ASR 2.0) → POST /tx → bridge.py
```

### Bridge 主要端点

| 端点 | 说明 |
|------|------|
| `GET /` `/panel` | Web UI：派 Bot 入会 |
| `GET /bots-admin` | Bot 管理页面 |
| `POST /say` | AI 对话 + TTS + 聊天回复 |
| `POST /raw` | 直接 TTS 朗读（不走 AI） |
| `POST /api/chat-in` | 会议聊天 webhook |
| `POST /api/vexa/bots` | 创建/管理 Bot |
| `POST /tx` | 接收 ASR 转录文本 |
| `GET /health` | 健康检查 |

## 代码风格

- **Python**（bridge.py, session_policy.py）：仅标准库，无第三方依赖；函数单一职责；常量集中定义
- **Node.js**（asr-shim, chat-forward）：ESM 模块；依赖最小化
- 不留死代码、注释掉的代码、调试 print/console.log
- 变量名自解释，避免缩写（领域通用除外）

## 添加新功能

- **Bridge 新端点**：在 `bridge.py` 中新增 handler，注意线程安全
- **新 Docker 服务**：在 `docker-compose.yml` 中新增 service 定义，配置环境变量和网络
- **新脚本**：放入 `scripts/` 目录，注明用途和用法

## 开发计划

详见 `plan/` 目录。

## Git 协作

1. 每次只改当前 Round 负责的文件
2. 改文件前先 `git status`
3. 发现文件被其他分支改过 → 停下来问
4. commit 写清楚：Round N - 做了啥
5. 遇到冲突 → 不要自己随便选 → 停下来报告

## Issue 创建规则

创建前先回答三个问题：

1. **一个周末能做完吗？** → 否 → 拆分
2. **验收标准清晰吗？** → 否 → 明确后再创建
3. **涉及几个模块？** → 超过 3 个 → 先拆分

普通任务模板：`.github/ISSUE_TEMPLATE/feature_request.md`，必须包含：
- 描述（一句话）
- 功能范围
- 涉及文件
- 验收标准
- 核心技术方案

### Epic 惯例

跨度大、需拆 3+ 子 issue 的任务，创建 Epic 汇总：

**命名**：`Epic · <主题>`

**Epic 模板**：`.github/ISSUE_TEMPLATE/epic_request.md`，必须包含：
- Overview / 背景
- Child Issues（task list：`- [ ] #XX`）
- 验收标准（引用子 issue）
- 非目标（明确不做的事）
- 优先级

**子 issue 模板**：同 `feature_request.md`，额外在 body 末尾加 `## Parent\n\n#<Epic编号>`。

### 子 issue 关联（GitHub Sub-issues API）

GitHub GraphQL 不支持写 Parent issue 字段，必须用 REST API 建立层级：

```bash
# 1. 获取子 issue 的数据库 ID（不是编号！）
gh api repos/{owner}/{repo}/issues/{number} --jq '.id'

# 2. 挂到父 issue 下
curl -X POST "https://api.github.com/repos/{owner}/{repo}/issues/{parent_number}/sub_issues" \
  -H "Authorization: Bearer $(gh auth token)" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -d '{"sub_issue_id": <database_id>}'
```

常见错误：
- 传 issue 编号 → 404（需传数据库 ID）
- GraphQL `updateProjectV2ItemFieldValue` + `parent_issue` → 不支持 mutation
- Board 上的 Parent issue 字段无法通过 API 设置，仅做手动引用

## GitHub Projects 集成

每次开发前查询 Project 状态：

```bash
# 查看 Ready issues（可认领）
node scripts/project-status.mjs

# 查看所有 issues
node scripts/project-status.mjs --all

# 认领 issue
node scripts/project-status.mjs --claim <number>

# 更新状态
node scripts/project-status.mjs --status <number> "In Progress"
```

**开发流程**：
1. 查询 `Ready` 状态的 issues
2. 认领（`--claim`）一个 issue
3. 开发完成后更新状态为 `In Review` 或 `Done`
4. 使用 `git commit` 提交，PR 参考 issue 编号

## 通知配置

需要用户介入时，通过飞书发送消息：

```bash
lark-cli im +messages-send --user-id ou_e5780b33c8dc235a1aefb40bd2ac11ab --text "<消息内容>" --as bot
```

适用场景：代码有疑问、遇到阻塞问题、功能开发完成需验收、分支已合并等你 review。
