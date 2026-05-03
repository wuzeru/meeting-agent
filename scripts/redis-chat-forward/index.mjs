#!/usr/bin/env node
/**
 * 订阅 Vexa Bot 写入的 Redis 频道 va:meeting:*:chat，把「非 Bot 发送」的聊天
 * 转发到 Hermes Bridge POST /api/chat-in。
 *
 * 环境变量：
 *   REDIS_URL              默认 redis://127.0.0.1:6379
 *   VEXA_API_BASE          默认 http://localhost:8056
 *   VEXA_API_KEY           必填，用于 GET /bots/id/{id} 解析 native_meeting_id
 *   BRIDGE_CHAT_FORWARD_URL 默认 http://127.0.0.1:8765/api/chat-in
 *   BRIDGE_CHAT_WEBHOOK_SECRET 可选，须与 bridge .env 一致
 *   CHAT_FORWARD_DEBUG     设为 true/1/on 时打印每条 Redis 消息与跳过原因
 */
import { createClient } from "redis";

const REDIS_URL = process.env.REDIS_URL ?? "redis://127.0.0.1:6379";
const VEXA_API_BASE = (process.env.VEXA_API_BASE ?? "http://localhost:8056").replace(
  /\/$/,
  ""
);
const VEXA_API_KEY = process.env.VEXA_API_KEY ?? "";
const BRIDGE_URL =
  process.env.BRIDGE_CHAT_FORWARD_URL ?? "http://127.0.0.1:8765/api/chat-in";
const BRIDGE_SECRET = process.env.BRIDGE_CHAT_WEBHOOK_SECRET ?? "";
const DEBUG = /^(1|true|yes|on)$/i.test(
  String(process.env.CHAT_FORWARD_DEBUG ?? "").trim()
);

const nativeCache = new Map();

function internalIdFromChannel(channel) {
  const s = String(channel ?? "");
  const m = /^va:meeting:(\d+):chat$/.exec(s);
  return m ? parseInt(m[1], 10) : null;
}

function pickInternalMeetingId(j, channel) {
  const mid = j?.meeting?.id ?? j?.meeting_id ?? j?.meetingId;
  if (mid != null && mid !== "") {
    const n = typeof mid === "number" ? mid : parseInt(String(mid), 10);
    return Number.isFinite(n) ? n : null;
  }
  return internalIdFromChannel(channel);
}

function isFromBotPayload(payload) {
  if (!payload || typeof payload !== "object") return false;
  const v = payload.is_from_bot ?? payload.isFromBot;
  return Boolean(v);
}

async function fetchNativeMeetingId(internalId) {
  const hit = nativeCache.get(internalId);
  if (hit) return hit;
  const r = await fetch(`${VEXA_API_BASE}/bots/id/${internalId}`, {
    headers: { "X-API-Key": VEXA_API_KEY },
  });
  if (!r.ok) {
    let body = "";
    try {
      body = (await r.text()).slice(0, 300);
    } catch {
      /* ignore */
    }
    console.warn(
      `[chat-forward] GET /bots/id/${internalId} HTTP ${r.status}${body ? ` body=${body}` : ""}`
    );
    return null;
  }
  const j = await r.json();
  const nat = j?.native_meeting_id ?? j?.nativeMeetingId;
  if (typeof nat === "string" && nat.trim()) {
    nativeCache.set(internalId, nat.trim());
    return nat.trim();
  }
  console.warn(
    `[chat-forward] GET /bots/id/${internalId} ok but no native_meeting_id in JSON`
  );
  return null;
}

async function postBridge({ text, nativeMeetingId, messageId }) {
  const body = {
    text,
    native_meeting_id: nativeMeetingId,
    message_id: messageId,
  };
  const headers = { "Content-Type": "application/json" };
  if (BRIDGE_SECRET) headers["Authorization"] = `Bearer ${BRIDGE_SECRET}`;
  const res = await fetch(BRIDGE_URL, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const t = await res.text();
    console.warn(`[chat-forward] bridge HTTP ${res.status}: ${t.slice(0, 200)}`);
    return false;
  }
  console.log(
    `[chat-forward] POST bridge ok native=${nativeMeetingId} chars=${text.length}`
  );
  return true;
}

async function main() {
  if (!VEXA_API_KEY) {
    console.error("missing VEXA_API_KEY");
    process.exit(1);
  }

  const sub = createClient({ url: REDIS_URL });
  sub.on("error", (err) => console.error("[chat-forward] Redis", err));
  await sub.connect();

  await sub.pSubscribe("va:meeting:*:chat", async (message, channel) => {
    try {
      let j;
      try {
        j = JSON.parse(message);
      } catch {
        if (DEBUG) console.log("[chat-forward] skip non-JSON", String(message).slice(0, 120));
        return;
      }
      if (DEBUG) {
        console.log(
          `[chat-forward] redis channel=${channel} type=${j?.type ?? "?"} keys=${Object.keys(j).join(",")}`
        );
      }
      if (j.type !== "chat.new_message") return;

      const internalId = pickInternalMeetingId(j, channel);
      const text = j.payload?.text;
      if (isFromBotPayload(j.payload)) {
        if (DEBUG) console.log("[chat-forward] skip bot message");
        return;
      }
      if (internalId == null || !String(text ?? "").trim()) {
        if (DEBUG) {
          console.log(
            `[chat-forward] skip empty internalId=${internalId} text=${Boolean(String(text ?? "").trim())}`
          );
        }
        return;
      }

      const native = await fetchNativeMeetingId(internalId);
      if (!native) return;

      const messageId = `redis-${internalId}-${j.payload?.timestamp ?? ""}-${String(text).slice(0, 64)}`;
      await postBridge({
        text: String(text),
        nativeMeetingId: native,
        messageId,
      });
    } catch (e) {
      console.error("[chat-forward] handler error", e);
    }
  });

  console.log(
    `[chat-forward] PSUBSCRIBE va:meeting:*:chat  redis=${REDIS_URL}  vexa=${VEXA_API_BASE}  bridge=${BRIDGE_URL}  debug=${DEBUG}`
  );
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
