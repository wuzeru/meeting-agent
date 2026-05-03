# 实施方案：Meet 聊天 → Hermes Bridge

面向：**自建 Docker Vexa（`vexaai/vexa-lite` 或 compose）+ 宿主机 / 同网络 Hermes Bridge**。  
背景原理见文末；**请先按下面阶段执行**。

---

## 阶段 0：Bridge 自检（约 5 分钟）

1. `.env` 设 **`BRIDGE_CHAT_WEBHOOK_SECRET`**（随机字符串）。
2. 重启 **`bridge.py`**。
3. 手测：

```bash
curl -sS -X POST "http://127.0.0.1:8765/api/chat-in" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <同上密钥>" \
  -d '{"text":"手动测试","native_meeting_id":"你的Meet会议码"}'
```

期望：**HTTP 202**，`bridge.log` 出现 **`[chat-in] -> cursor`**。

---

## 阶段 1：选一条路（二选一）

| 路线 | 适用 | 复杂度 |
|------|------|--------|
| **路线 A — 改上游 Bot（HTTP）** | 你愿意 fork **[Vexa-ai/vexa](https://github.com/Vexa-ai/vexa)**、重建镜像 | 中（维护 fork） |
| **路线 B — Redis 转发（compose 已接）** | **默认**：`docker-compose.yml` 里的 **`chat-forward`** 服务 | 低 |

官方 **`PUT /user/webhook`** **不包含**聊天事件，不能单靠配置替代这两条路。

---

## 路线 B（已实施）：`chat-forward` + `network_mode: service:vexa`

**依据**：上游 **`chat.ts`** 对 **`va:meeting:<内部id>:chat`** 发布 **`chat.new_message`**。

**本仓库改动**：

- **`docker-compose.yml`** 增加服务 **`chat-forward`**：`network_mode: service:vexa`，用 **`redis://127.0.0.1:6379`**、**`http://127.0.0.1:8056`** 访问 Lite 内置 Redis 与 API。
- **`vexa`** 增加 **`extra_hosts: host.docker.internal`**，便于 **`POST http://host.docker.internal:8765/api/chat-in`** 到宿主机 Bridge。
- 脚本：**`scripts/redis-chat-forward/index.mjs`**。

### `.env` 必配

| 变量 | 说明 |
|------|------|
| **`VEXA_API_KEY`** | 与 bridge 调 Vexa 的 Key 相同（你已有）。 |
| **`BRIDGE_CHAT_WEBHOOK_SECRET`** | 与 bridge `.env` 里同名变量一致，否则 bridge 返回 **401**。 |

可选（compose 已设默认值，一般不必改）：**`BRIDGE_CHAT_FORWARD_URL`**、**`CHAT_FORWARD_REDIS_URL`**、**`CHAT_FORWARD_VEXA_API_BASE`**。

### 启动与排障

```bash
docker compose up -d
docker logs -f vexa-chat-forward
```

期望日志：`PSUBSCRIBE va:meeting:*:chat`；Meet 里发非 Bot 聊天后，**`bridge.log`** 出现 **`[chat-in]`**。

**Meet 聊天没反应时**（`bridge.log` 里始终没有 `[chat-in]`）：

1. 确认 **`chat-forward` 在跑**：`docker ps` 里有 **`vexa-chat-forward`**，`docker logs vexa-chat-forward` 有订阅成功那一行。
2. `.env` 里 **`VEXA_API_KEY`** 与派 Bot 用的 Key 一致；若日志里 **`GET /bots/id/… HTTP 401/403`**，说明 Key 不对。
3. 宿主机 Bridge 已启动，**`BRIDGE_CHAT_WEBHOOK_SECRET`** 与 bridge `.env` 一致（否则会 **`bridge HTTP 401`**）。
4. 临时在 `.env` 加 **`CHAT_FORWARD_DEBUG=true`**，再 **`docker compose up -d chat-forward`**，发一条 Meet 聊天后看转发器是否收到 **`chat.new_message`**、是否 **`POST bridge ok`**。

若 Redis 连不上：Lite 镜像若把 Redis 绑在 **非 127.0.0.1**，可把 **`CHAT_FORWARD_REDIS_URL`** 改成实际地址（一般不必）。

### 仅宿主机跑脚本（不用 compose 时）

```bash
cd scripts/redis-chat-forward && npm install
export VEXA_API_KEY='…' VEXA_API_BASE='http://localhost:8056' REDIS_URL='redis://127.0.0.1:6379' \
  BRIDGE_CHAT_FORWARD_URL='http://127.0.0.1:8765/api/chat-in' BRIDGE_CHAT_WEBHOOK_SECRET='…'
node index.mjs
```

（需自行把 **Redis 6379** 映射到宿主机，或能访问到 Redis 的网络位置。）

---

## 路线 A：在 `chat.ts` 里直接 POST Bridge（推荐长期）

**依据**：上游 **`services/vexa-bot/core/src/services/chat.ts`**，`onNewMessage` 已聚合每条聊天。

**步骤概要**

1. Fork / clone：`git clone https://github.com/Vexa-ai/vexa.git`
2. 给 **`MeetingChatService`** 增加字段 **`nativeMeetingIdStr: string`**，在实例化该类的代码路径把 Meet **`native_meeting_id`**（`xxx-yyyy-zzz`）传进来（在上游 bot 启动流程里通常已有）。
3. 在 **`onNewMessage`** 内、过滤 **`isFromBot`** 之后，若配置了环境变量则 **`fetch`**：
   - URL：**`BRIDGE_CHAT_FORWARD_URL`**（例如 `http://host.docker.internal:8765/api/chat-in`）
   - Header：**`Authorization: Bearer <BRIDGE_CHAT_WEBHOOK_SECRET>`**
   - Body：`{ "text", "native_meeting_id", "message_id" }`
4. 参考粘贴片段：**`patches/vexa-chat-forward-snippet.ts.txt`**（粘贴前删掉注释外壳，并按上游类型补齐字段）。
5. 重建 **`vexa-bot`** / **`vexa-lite`** 镜像，在 compose 里改为用你的镜像。
6. Bot 容器环境变量示例：

```yaml
environment:
  BRIDGE_CHAT_FORWARD_URL: "http://host.docker.internal:8765/api/chat-in"
  BRIDGE_CHAT_WEBHOOK_SECRET: "${BRIDGE_CHAT_WEBHOOK_SECRET}"
```

（与当前 `asr-shim` 用 **`host.docker.internal`** 访问宿主机 Bridge 一致。）

---

## 阶段 2：联调与会话对齐

1. **派 Bot**，确保 **`native_meeting_id`** 与 Bridge **`current_meeting_id`** 一致（面板发送 Bot 或 `/api/set-meeting`）。
2. 在 Meet **聊天框发一句**（可先带 **`@小明`**，视 **`HERMES_CHAT_REQUIRE_MENTION`** 而定）。
3. 看 **`bridge.log`**：**`[chat-in]`** → Cursor → TTS/chat。

---

## 阶段 3：安全与稳定性

- **`BRIDGE_CHAT_WEBHOOK_SECRET`** 必填于公网可达的 Bridge。
- 若曾把 **Redis 映射到宿主机**，请仅绑定 **`127.0.0.1`**，勿对公网开放。
- 路线 A 失败时 **`fetch` 勿抛断主流程**（snippet 已 `.catch` 思路）。

---

## 附录：为什么不是「配一个 Webhook」就行？

上游 **`features/webhooks/README.md`**：`PUT /user/webhook` 只推送 **会议生命周期**，**没有**「聊天消息」事件。聊天在 **`vexa-bot` → Redis**（见 **`services/vexa-bot/core/src/services/chat.ts`**）。

---

## 附录：本仓库已有能力

- Bridge **`POST /api/chat-in`**、`BRIDGE_CHAT_WEBHOOK_SECRET`、`HERMES_CHAT_BOT_ALIASES` 等（见 **`bridge.py`**）。
