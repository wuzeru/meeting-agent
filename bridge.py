#!/usr/bin/env python3
"""
Vexa <-> Hermes bridge — HTTP trigger mode.

Endpoints:
  POST /say   { "text": "..." }   -> Hermes generates reply, Vexa speaks + chats it
  POST /raw   { "text": "..." }   -> Skip Hermes; speak the literal text
  POST /api/chat-in — Meet 聊天 inbound（见下文），走 Cursor 回复链路
  GET  /  /panel — Meet 派 Bot 页面
  GET /health
  GET /bots-admin — Bot 管理（代理 Vexa）；派 Bot 成功后轮询至入会 active 再问候（HERMES_MEETING_GREETING），豆包 TTS 缓存于 ~/.cache/vexa-bridge/tts-cache。
  GET /api/vexa/bots ， GET /api/vexa/bots/id/{id}
  POST /api/vexa/bots ， DELETE /api/vexa/bots/{platform}/{native_meeting_id}

Multi-turn (Cursor agent): by default pass --resume <native_meeting_id> whenever the
bridge knows the active meeting (send-bot / set-meeting / MEETING_ID), so one Meet room
maps to one CLI session for the whole meeting. Disable with HERMES_CURSOR_RESUME_MEETING_ID=false.

Fall back: captured CLI session_id from stdout may still be stored at
  ~/.cache/vexa-bridge/<session_alias>.session
when meeting-resume is off; session_policy (HERMES_SESSION_POLICY, …) applies then.
HERMES_AGENT_CONTINUE_MODE=after_first adds bare --continue after first reply only when
there is no resume id (no meeting id + no stored session).

Wake deferred TTS: HERMES_WAKE_PENDING_TTS=true → after ack, speak HERMES_WAKE_PENDING_TEXT,
run Cursor with HERMES_ASYNC_CURSOR_TIMEOUT_S (≥ HERMES_TIMEOUT_S), then speak the answer
(truncated to HERMES_WAKE_COMPLETION_TTS_MAX_CHARS for audio; full text still post_chat).
Forces oneshot (ignores HERMES_STREAM) for that reply.

Cursor agent subprocess:
  CURSOR_AGENT_WORKSPACE — if set to an existing directory, subprocess cwd for agent (trust workspace).
  CURSOR_AGENT_FORCE / CURSOR_AGENT_TRUST / CURSOR_AGENT_APPROVE_MCPS — default true; set false/off to omit matching argv flag.

当前 Meet 代码（native_meeting_id）优先来自文件 current_meeting_id（POST /api/send-bot
或 POST /api/vexa/bots 成功、或 POST /api/set-meeting）；若无则回退环境变量 MEETING_ID。

聊天接入：Bridge 无法主动读取 Meet UI；须在 Vexa（或其它上游）配置 Webhook，
在收到会议聊天时将 JSON POST 到 http(s)://<bridge>/api/chat-in：
  {"text":"…","native_meeting_id":"xxx-yyyy-zzz","message_id":"可选去重"}
鉴权（推荐）：BRIDGE_CHAT_WEBHOOK_SECRET + Authorization: Bearer <secret> 或 X-Bridge-Secret。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import quote

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import session_policy


def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"missing env: {name}")
    return v


API_BASE = env("VEXA_API_BASE", "http://localhost:8056")
API_KEY = env("VEXA_API_KEY", required=True)
PLATFORM = env("PLATFORM", "google_meet")
# 可选：无 current_meeting_id 文件时的回退；平时以网页「发送 Bot」为准
MEETING_ID = env("MEETING_ID", "").strip()
# POST /bots 时开启「语音代理」，否则 /speak、/chat 会 404（仅转写模式无此能力）
VEXA_VOICE_AGENT_ENABLED = env("VEXA_VOICE_AGENT_ENABLED", "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
HERMES_BIN = env("HERMES_BIN", "hermes")
HERMES_VOICE = env("HERMES_VOICE", "en_US-amy-medium")
HERMES_TIMEOUT_S = int(env("HERMES_TIMEOUT_S", "120"))
HERMES_MODEL = env("HERMES_MODEL")
HERMES_PROVIDER = env("HERMES_PROVIDER")
# 若设置则固定 Hermes 会话文件名；否则按当前会议代码为 vexa-<meeting_id>
HERMES_SESSION_NAME_ENV = env("HERMES_SESSION_NAME", "").strip()
# Cursor agent：每次调用传 --resume <当前 native_meeting_id>，整场会议共用同一 CLI 会话
HERMES_CURSOR_RESUME_MEETING_ID = env(
    "HERMES_CURSOR_RESUME_MEETING_ID", "true"
).strip().lower() in ("1", "true", "yes", "on")
# 派 Bot 成功后：轮询直到 status=active 再播报问候；设为空字符串可关闭
HERMES_MEETING_GREETING = env(
    "HERMES_MEETING_GREETING",
    "大家好，我是语音助手小明同学，已进入会议，有事可以叫我。",
).strip()
try:
    HERMES_MEETING_GREETING_POLL_S = float(env("HERMES_MEETING_GREETING_POLL_S", "2"))
except ValueError:
    HERMES_MEETING_GREETING_POLL_S = 2.0
try:
    HERMES_MEETING_GREETING_MAX_WAIT_S = float(
        env("HERMES_MEETING_GREETING_MAX_WAIT_S", "600")
    )
except ValueError:
    HERMES_MEETING_GREETING_MAX_WAIT_S = 600.0
try:
    HERMES_MEETING_GREETING_AFTER_ACTIVE_S = float(
        env("HERMES_MEETING_GREETING_AFTER_ACTIVE_S", "1")
    )
except ValueError:
    HERMES_MEETING_GREETING_AFTER_ACTIVE_S = 1.0
HERMES_MEETING_GREETING_POST_CHAT = env(
    "HERMES_MEETING_GREETING_POST_CHAT", "false"
).strip().lower() in ("1", "true", "yes", "on")
# Meet 聊天：由上游在收到会议聊天时 POST 到 /api/chat-in（需在 Vexa 配置 webhook URL）
BRIDGE_CHAT_WEBHOOK_SECRET = env("BRIDGE_CHAT_WEBHOOK_SECRET", "").strip()
HERMES_CHAT_BOT_ALIASES = [
    a.strip()
    for a in env("HERMES_CHAT_BOT_ALIASES", "小明,xiaoming").split(",")
    if a.strip()
]
HERMES_CHAT_REQUIRE_MENTION = env(
    "HERMES_CHAT_REQUIRE_MENTION", "false"
).strip().lower() in ("1", "true", "yes", "on")
try:
    HERMES_CHAT_COOLDOWN_S = float(env("HERMES_CHAT_COOLDOWN_S", "2"))
except ValueError:
    HERMES_CHAT_COOLDOWN_S = 2.0
HERMES_CHAT_MIN_LEN = int(env("HERMES_CHAT_MIN_LEN", "1"))
SYSTEM_PRIMER = env(
    "HERMES_PRIMER",
    "你是 Google Meet 里的语音助手「小明同学」。规则："
    "(1) 用用户说话的语言回复；用户说中文你也说中文； "
    "(2) 输出纯文本，不要 markdown、代码块、列表、emoji； "
    "(3) 不要重复用户的问题，直接给答案； "
    "(4) 当用户要求查询、搜索、获取实时信息（如商品价格、新闻、网页内容）时，"
    "**必须主动调用相应的 skill / tool**（如 taobao-native、web 等）；"
    "不要在没尝试工具前直接说「我做不到 / 我没有访问权限」； "
    "(5) 工具返回结果后，用一两句话简洁口语化总结，控制在 50 字以内； "
    "(6) 闲聊或纯知识问题（如日期、定义）不需要工具时，控制在 30 字以内。",
)
BIND = env("BRIDGE_BIND", "127.0.0.1")
PORT = int(env("BRIDGE_PORT", "8765"))

TTS_PROVIDER = env("TTS_PROVIDER", "piper").lower()

# Volcengine Doubao TTS V3 SSE — https://www.volcengine.com/docs/6561/1598757
DOUBAO_TTS_URL = env(
    "DOUBAO_TTS_URL",
    "https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse",
)
# Accept either DOUBAO_* or VOLC_SPEECH_* env names so users can paste either.
DOUBAO_APPID = env("VOLC_SPEECH_APP_ID") or env("DOUBAO_APPID")
DOUBAO_TOKEN = env("VOLC_SPEECH_ACCESS_KEY") or env("DOUBAO_ACCESS_TOKEN")
DOUBAO_RESOURCE_ID = (
    env("VOLC_TTS_RESOURCE_ID") or env("DOUBAO_RESOURCE_ID") or "seed-tts-1.0"
)
DOUBAO_VOICE = (
    env("VOLC_TTS_SPEAKER") or env("DOUBAO_VOICE") or "BV700_streaming"
)
DOUBAO_FORMAT = env("DOUBAO_FORMAT", "mp3")
DOUBAO_SAMPLE_RATE = int(env("DOUBAO_SAMPLE_RATE", "24000"))
# V3 用 speech_rate / loudness_rate, 范围 [-50, 100], 0 = 默认
DOUBAO_SPEECH_RATE = int(env("DOUBAO_SPEECH_RATE", "0"))
DOUBAO_LOUDNESS_RATE = int(env("DOUBAO_LOUDNESS_RATE", "0"))
DOUBAO_EMOTION = env("DOUBAO_EMOTION")
DOUBAO_EMOTION_SCALE = env("DOUBAO_EMOTION_SCALE")
DOUBAO_TIMEOUT_S = int(env("DOUBAO_TIMEOUT_S", "30"))
DOUBAO_USER_ID = env("DOUBAO_USER_ID", "vexa-bridge")

HERMES_AUTO_REPLY = env("HERMES_AUTO_REPLY", "true").strip().lower() in ("1", "true", "yes", "on")
HERMES_WAKE_WORDS = [
    w.strip()
    for w in env(
        "HERMES_WAKE_WORDS",
        "hermes,Hermes,赫尔墨斯,小赫,小赫尔",
    ).split(",")
    if w.strip()
]
HERMES_AUTO_MIN_LEN = int(env("HERMES_AUTO_MIN_LEN", "2"))
HERMES_AUTO_COOLDOWN_S = float(env("HERMES_AUTO_COOLDOWN_S", "3"))
WAKE_CAPTURE_ENABLED = env("WAKE_CAPTURE_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
WAKE_CAPTURE_SILENCE_S = float(env("WAKE_CAPTURE_SILENCE_S", "1.4"))
WAKE_CAPTURE_MAX_S = float(env("WAKE_CAPTURE_MAX_S", "10"))
WAKE_CAPTURE_MIN_HOLD_S = float(env("WAKE_CAPTURE_MIN_HOLD_S", "2.4"))

STATE_DIR = os.path.expanduser("~/.cache/vexa-bridge")
CURRENT_MEETING_FILE = os.path.join(STATE_DIR, "current_meeting_id")
os.makedirs(STATE_DIR, exist_ok=True)
TTS_CACHE_DIR = os.path.join(STATE_DIR, "tts-cache")
os.makedirs(TTS_CACHE_DIR, exist_ok=True)

_meeting_greet_lock = threading.Lock()
# 已播过问候的键：优先 native_id#bot_id，避免同会议码再次派 Bot 时被静默跳过
_meeting_greeted_keys: set[str] = set()


def _greeting_dedupe_key(native_id: str, bot_id: int | None) -> str:
    n = (native_id or "").strip()
    if bot_id is not None:
        return f"{n}#{bot_id}"
    return n

_chat_in_dedupe_lock = threading.Lock()
_chat_in_seen_at: dict[str, float] = {}

VEXA_HEADERS = {"X-API-Key": API_KEY, "Content-Type": "application/json"}


def get_meeting_id() -> str:
    if os.path.isfile(CURRENT_MEETING_FILE):
        try:
            with open(CURRENT_MEETING_FILE, encoding="utf-8") as f:
                s = f.read().strip()
                if s:
                    return s
        except OSError:
            pass
    return MEETING_ID


def set_current_meeting_id(native_id: str) -> None:
    native_id = (native_id or "").strip()
    if not native_id:
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(CURRENT_MEETING_FILE, "w", encoding="utf-8") as f:
        f.write(native_id)


def hermes_session_key() -> str:
    if HERMES_SESSION_NAME_ENV:
        return HERMES_SESSION_NAME_ENV
    mid = get_meeting_id()
    return f"vexa-{mid}" if mid else "vexa"


def session_state_path() -> str:
    return os.path.join(STATE_DIR, f"{hermes_session_key()}.session")


def parse_google_meet_native_id(raw: str) -> str:
    """Extract Meet native_meeting_id from a URL or return stripped code if already bare."""
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty")
    m = re.search(r"meet\.google\.com/([a-z0-9\-]+)", s, re.I)
    if m:
        return m.group(1).split("?")[0].split("/")[0].strip()
    if re.fullmatch(r"[a-z0-9\-]+", s, re.I):
        return s
    raise ValueError("not a meet url or code")


def http_call(method, path, body=None, timeout_s=15):
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method, headers=VEXA_HEADERS)
    try:
        with request.urlopen(req, timeout=timeout_s) as r:
            payload = r.read()
            if not payload:
                return r.status, {}
            try:
                return r.status, json.loads(payload)
            except json.JSONDecodeError:
                return r.status, {"_raw": payload.decode(errors="replace")}
    except error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        try:
            parsed = json.loads(err_body) if err_body else {}
        except json.JSONDecodeError:
            parsed = {"error": err_body}
        return e.code, parsed if isinstance(parsed, dict) else {"error": err_body}
    except Exception as e:
        return 0, {"error": repr(e)}


def _vexabot_id_from_payload(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    v = payload.get("id")
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _vexabot_get_by_id(bot_id: int) -> dict | None:
    status, resp = http_call("GET", f"/bots/id/{bot_id}", None, timeout_s=45)
    if status == 200 and isinstance(resp, dict):
        return resp
    return None


def _vexabot_status_for_meeting(native_id: str) -> str | None:
    nid = (native_id or "").strip()
    if not nid:
        return None
    status, resp = http_call("GET", "/bots", None, timeout_s=45)
    if status != 200 or not isinstance(resp, dict):
        return None
    for m in resp.get("meetings") or []:
        if (m.get("native_meeting_id") or "").strip() == nid:
            s = (m.get("status") or "").strip()
            return s or None
    return None


def _bot_join_status(native_id: str, bot_id: int | None) -> str | None:
    if bot_id is not None:
        rec = _vexabot_get_by_id(bot_id)
        if rec and (rec.get("status") or "").strip():
            return str(rec["status"]).strip()
    return _vexabot_status_for_meeting(native_id)


def _wait_until_bot_active(native_id: str, bot_id: int | None) -> bool:
    """Poll Vexa until this bot is active (admitted). False on timeout / failed / meeting change."""
    deadline = time.time() + max(1.0, HERMES_MEETING_GREETING_MAX_WAIT_S)
    poll = max(0.5, HERMES_MEETING_GREETING_POLL_S)
    while time.time() < deadline:
        if get_meeting_id() != native_id:
            log(
                f"[greeting] abort wait (meeting mismatch: expected {native_id!r}, "
                f"current {get_meeting_id()!r})"
            )
            return False
        st = _bot_join_status(native_id, bot_id)
        if st:
            low = st.lower()
            if low == "active":
                log("[greeting] bot active, playing greeting")
                return True
            if low == "failed":
                log(f"[greeting] bot status=failed, skip greeting")
                return False
            if low in ("completed", "stopped", "cancelled"):
                log(f"[greeting] bot status={st!r}, skip greeting")
                return False
        time.sleep(poll)
    log("[greeting] timeout waiting for bot active, skip greeting")
    return False


def synth_doubao_bytes(text, audio_format=None):
    """Call Volcengine Doubao TTS V3 SSE, return (audio_bytes, format) or raise."""
    if not (DOUBAO_APPID and DOUBAO_TOKEN):
        raise RuntimeError("doubao appid/token not configured")
    fmt = (audio_format or DOUBAO_FORMAT).strip().lower() or DOUBAO_FORMAT

    audio_params = {
        "format": fmt,
        "sample_rate": DOUBAO_SAMPLE_RATE,
        "speech_rate": DOUBAO_SPEECH_RATE,
        "loudness_rate": DOUBAO_LOUDNESS_RATE,
    }
    if DOUBAO_EMOTION:
        audio_params["emotion"] = DOUBAO_EMOTION
        if DOUBAO_EMOTION_SCALE:
            try:
                audio_params["emotion_scale"] = float(DOUBAO_EMOTION_SCALE)
            except ValueError:
                pass

    payload = {
        "user": {"uid": DOUBAO_USER_ID},
        "req_params": {
            "text": text,
            "speaker": DOUBAO_VOICE,
            "audio_params": audio_params,
        },
    }

    headers = {
        "X-Api-App-Id": DOUBAO_APPID,
        "X-Api-Access-Key": DOUBAO_TOKEN,
        "X-Api-Resource-Id": DOUBAO_RESOURCE_ID,
        "X-Api-Request-Id": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(DOUBAO_TTS_URL, data=data, method="POST", headers=headers)

    audio_chunks = []  # list[bytes] - raw decoded audio bytes per SSE frame
    with request.urlopen(req, timeout=DOUBAO_TIMEOUT_S) as r:
        buffer = b""
        while True:
            chunk = r.read(4096)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line or line.startswith(b"event:") or line.startswith(b":"):
                    continue
                if not line.startswith(b"data:"):
                    continue
                raw = line[5:].strip()
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue

                code = obj.get("code", 0)
                if code == 20000000:  # end of stream
                    continue
                if code != 0:
                    raise RuntimeError(
                        f"doubao v3 sse code={code} msg={obj.get('message')!r}"
                    )
                payload_b64 = obj.get("data")
                if payload_b64:
                    try:
                        audio_chunks.append(base64.b64decode(payload_b64))
                    except Exception as e:
                        raise RuntimeError(f"doubao v3 sse base64 decode failed: {e}")

    if not audio_chunks:
        raise RuntimeError("doubao v3 sse: no audio frames received")

    audio_bytes = b"".join(audio_chunks)
    return audio_bytes, fmt


def _speak_doubao_audio_bytes(audio_bytes, fmt):
    mid = get_meeting_id()
    if not mid:
        return 0, {"error": "no_meeting_id"}
    audio_b64 = base64.b64encode(audio_bytes).decode()
    return http_call(
        "POST",
        f"/bots/{PLATFORM}/{mid}/speak",
        {"audio_base64": audio_b64, "format": fmt, "sample_rate": DOUBAO_SAMPLE_RATE},
    )


def speak_piper(text):
    mid = get_meeting_id()
    if not mid:
        log("[speak] no active meeting_id（请先网页发送 Bot 或设置 MEETING_ID）")
        return 0, {"error": "no_meeting_id"}
    return http_call(
        "POST",
        f"/bots/{PLATFORM}/{mid}/speak",
        {"text": text, "voice": HERMES_VOICE},
    )


def _strip_urls_for_tts(s: str) -> str:
    """Remove hyperlinks from text before TTS; keep chat post_chat with full text."""
    if not s or not s.strip():
        return s
    out = s
    # http(s) — stop at spaces / brackets so “…id=（同款” 不会在 URL 里吞掉中文括号
    out = re.sub(
        r"https?://[^\s）\)（\[\]「」\];\"\'>,]+", "", out, flags=re.IGNORECASE
    )
    # 淘宝短链等无 scheme
    out = re.sub(
        r"(?<![\w/])(?:m\.)?tb\.cn/[^\s）\)（\[\]「」\];\"\'>,]+",
        "",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(r"\s{2,}", " ", out)
    # 去掉 URL 后 “打开：（” 等生硬断句
    out = re.sub(r"[：:]\s*（", "，（", out)
    return out.strip()


def speak_doubao(text):
    audio_bytes, fmt = synth_doubao_bytes(text)
    log(f"[doubao] synth ok ({len(audio_bytes)} bytes, fmt={fmt})")
    return _speak_doubao_audio_bytes(audio_bytes, fmt)


def _wake_completion_speak_text(answer: str) -> str:
    """HERMES_WAKE_PENDING_TTS: cap TTS length; Meet chat still receives full answer."""
    if not HERMES_WAKE_PENDING_TTS or not (answer and answer.strip()):
        return answer
    m = HERMES_WAKE_COMPLETION_TTS_MAX_CHARS
    if len(answer) <= m:
        return answer
    return answer[:m].rstrip() + "……其余已发到会议聊天。"


def speak(text):
    text = _strip_urls_for_tts(text)
    if TTS_PROVIDER == "doubao":
        try:
            return speak_doubao(text)
        except Exception as e:
            log(f"[doubao] FAILED, fallback to piper: {e!r}")
            return speak_piper(text)
    return speak_piper(text)


def _load_or_build_ack_audio():
    if os.path.exists(ACK_CACHE_FILE):
        with open(ACK_CACHE_FILE, "rb") as f:
            return f.read(), ACK_AUDIO_FORMAT, True
    audio_bytes, fmt = synth_doubao_bytes(ACK_TEXT, audio_format=ACK_AUDIO_FORMAT)
    with open(ACK_CACHE_FILE, "wb") as f:
        f.write(audio_bytes)
    return audio_bytes, fmt, False


def speak_ack():
    """Speak a quick ack immediately to reduce waiting anxiety."""
    if not ACK_ENABLED:
        return 0, {"skipped": "ack_disabled"}
    if TTS_PROVIDER != "doubao":
        return speak_piper(ACK_TEXT)
    try:
        audio_bytes, fmt, from_cache = _load_or_build_ack_audio()
        sc, body = _speak_doubao_audio_bytes(audio_bytes, fmt)
        log(
            f"[ack] speak={sc} fmt={fmt} cache={'hit' if from_cache else 'miss'} "
            f"text={ACK_TEXT!r}"
        )
        return sc, body
    except Exception as e:
        log(f"[ack] doubao failed, fallback to piper: {e!r}")
        return speak_piper(ACK_TEXT)


def _greeting_audio_cache_path() -> str:
    key = hashlib.sha256(
        "|".join(
            (
                HERMES_MEETING_GREETING,
                DOUBAO_VOICE,
                DOUBAO_FORMAT,
                str(DOUBAO_SAMPLE_RATE),
                str(DOUBAO_SPEECH_RATE),
                str(DOUBAO_LOUDNESS_RATE),
            )
        ).encode("utf-8")
    ).hexdigest()[:24]
    ext = (DOUBAO_FORMAT or "mp3").strip().lower()
    if ext not in ("mp3", "wav", "pcm"):
        ext = "mp3"
    return os.path.join(TTS_CACHE_DIR, f"greeting_{key}.{ext}")


def _load_or_build_greeting_audio():
    """Doubao greeting audio on disk; regenerate when cache missing or text/voice params change."""
    path = _greeting_audio_cache_path()
    fmt = (DOUBAO_FORMAT or "mp3").strip().lower() or "mp3"
    if os.path.isfile(path):
        with open(path, "rb") as f:
            return f.read(), fmt, True
    if not HERMES_MEETING_GREETING.strip():
        raise RuntimeError("empty HERMES_MEETING_GREETING")
    audio_bytes, out_fmt = synth_doubao_bytes(
        HERMES_MEETING_GREETING.strip(), audio_format=fmt
    )
    with open(path, "wb") as f:
        f.write(audio_bytes)
    return audio_bytes, out_fmt, False


def speak_greeting_cached():
    """Play meeting greeting; Doubao uses disk cache like ACK."""
    text = HERMES_MEETING_GREETING.strip()
    if not text:
        return 0, {"skipped": "empty greeting"}
    if TTS_PROVIDER != "doubao":
        return speak(text)
    try:
        audio_bytes, fmt, hit = _load_or_build_greeting_audio()
        sc, body = _speak_doubao_audio_bytes(audio_bytes, fmt)
        log(f"[greeting] TTS cache={'hit' if hit else 'miss'} fmt={fmt} speak={sc}")
        return sc, body
    except Exception as e:
        log(f"[greeting] cached doubao failed: {e!r}, fallback speak")
        return speak(text)


def post_chat(text):
    mid = get_meeting_id()
    if not mid:
        log("[chat] no active meeting_id（请先网页发送 Bot 或设置 MEETING_ID）")
        return 0, {"error": "no_meeting_id"}
    return http_call(
        "POST", f"/bots/{PLATFORM}/{mid}/chat", {"text": text}
    )


def schedule_meeting_join_greeting(
    native_id: str, vexa_bot_payload: dict | None = None
) -> None:
    """After POST /bots succeeds: poll until bot is active, then speak greeting once."""
    native_id = (native_id or "").strip()
    if not native_id or not HERMES_MEETING_GREETING:
        return
    bot_id = _vexabot_id_from_payload(vexa_bot_payload)

    def run():
        if not _wait_until_bot_active(native_id, bot_id):
            return
        time.sleep(max(0.0, HERMES_MEETING_GREETING_AFTER_ACTIVE_S))
        if get_meeting_id() != native_id:
            log(
                f"[greeting] skip after active (meeting mismatch: expected {native_id!r}, "
                f"current {get_meeting_id()!r})"
            )
            return
        gkey = _greeting_dedupe_key(native_id, bot_id)
        with _meeting_greet_lock:
            if gkey in _meeting_greeted_keys:
                log(
                    f"[greeting] skip duplicate (already played for {gkey!r}; "
                    "restart bridge 或换新会议码可清内存去重)"
                )
                return
            _meeting_greeted_keys.add(gkey)
        try:
            text = HERMES_MEETING_GREETING
            sc, _ = speak_greeting_cached()
            log(f"[greeting] done speak={sc}")
            if HERMES_MEETING_GREETING_POST_CHAT:
                cc, _ = post_chat(text)
                log(f"[greeting] chat={cc}")
        except Exception as e:
            log(f"[greeting] failed: {e!r}")

    threading.Thread(target=run, daemon=True, name="meeting-greeting").start()


def _chat_in_check_secret(handler: BaseHTTPRequestHandler) -> bool:
    if not BRIDGE_CHAT_WEBHOOK_SECRET:
        return True
    auth = handler.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer ") and auth[7:].strip() == BRIDGE_CHAT_WEBHOOK_SECRET:
        return True
    for hname in ("X-Bridge-Secret", "X-Webhook-Secret"):
        if handler.headers.get(hname) == BRIDGE_CHAT_WEBHOOK_SECRET:
            return True
    return False


def _parse_inbound_chat_body(body: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """Return (text, native_meeting_id, message_id). Tolerant of nested payloads."""
    text = ""
    mid = None
    msg_id = None
    msg = body.get("message")
    if isinstance(msg, dict):
        text = (msg.get("text") or msg.get("content") or "").strip()
        msg_id = msg.get("id") or msg.get("message_id")
        mid = msg.get("native_meeting_id") or msg.get("meeting_id")
    if not text:
        text = (
            (body.get("text") or body.get("content") or body.get("body") or "") or ""
        ).strip()
    if not mid:
        raw_mid = (
            body.get("native_meeting_id")
            or body.get("nativeMeetingId")
            or body.get("meeting_id")
        )
        if isinstance(raw_mid, str):
            mid = raw_mid.strip() or None
    if msg_id is None:
        msg_id = body.get("message_id") or body.get("event_id")
    if isinstance(msg_id, (int, float)):
        msg_id = str(int(msg_id))
    elif isinstance(msg_id, str):
        msg_id = msg_id.strip() or None
    return text, mid, msg_id


def _chat_in_duplicate(message_id: str) -> bool:
    """Return True if this id was seen recently (skip handling)."""
    if not message_id:
        return False
    now = time.time()
    ttl = 300.0
    with _chat_in_dedupe_lock:
        for k, t in list(_chat_in_seen_at.items()):
            if now - t > ttl:
                del _chat_in_seen_at[k]
        if message_id in _chat_in_seen_at:
            return True
        _chat_in_seen_at[message_id] = now
        return False


def _chat_in_resolve_meeting(native_from_body: str | None) -> tuple[bool, str | None]:
    """Ensure bridge meeting id matches inbound; optionally set cache from webhook."""
    cur = get_meeting_id()
    nf = (native_from_body or "").strip()
    if nf:
        if cur and nf != cur:
            return False, f"native_meeting_id {nf!r} != bridge current {cur!r}"
        if not cur:
            set_current_meeting_id(nf)
        return True, None
    if not cur:
        return False, "missing native_meeting_id (bridge has no current meeting)"
    return True, None


def _strip_leading_chat_mentions(text: str) -> str:
    s = text.strip()
    for alias in HERMES_CHAT_BOT_ALIASES:
        if not alias:
            continue
        pat = rf"^@\s*{re.escape(alias)}\s*"
        s = re.sub(pat, "", s, flags=re.IGNORECASE).strip()
    return s


def _text_has_bot_mention(text: str) -> bool:
    for alias in HERMES_CHAT_BOT_ALIASES:
        if alias and re.search(rf"@\s*{re.escape(alias)}\b", text, re.IGNORECASE):
            return True
    return False


_BANNER = re.compile(
    r"^[\s│╭╰─⚕]*Hermes[\s─╮╯]*$"
    r"|^[╭╰─]+$"
    r"|^↻?\s*Resumed session\b"
    r"|\bResumed session\s+\S+\s*\(\d+\s+user messages?,\s*\d+\s+total messages?\)\s*$"
)
_SESSION_LINE = re.compile(r"^session_id:\s*(\S+)", re.IGNORECASE)
# JSON or loose text when the line is not exactly "session_id: ..."
_SESSION_ID_JSON = re.compile(r'(?i)"session_?id"\s*:\s*"([^"\\]+)"')


def _extract_session_id_fuzzy(text: str) -> str | None:
    if not (text and text.strip()):
        return None
    text = _ANSI_RE.sub("", text)
    m = re.search(
        r"(?is)(?:^|[\n\r])\s*session_?id\s*[:=]\s*['\"]?([^\s\n\r'\"\\]+)", text
    )
    if m:
        return m.group(1).strip()
    m = _SESSION_ID_JSON.search(text)
    if m:
        return m.group(1).strip()
    return None
_STALE_SESSION_RE = re.compile(
    r"(No session found matching|Session not found:|Use a session ID from a previous CLI run)",
    re.IGNORECASE,
)
_BOX_CHARS_RE = re.compile(r"[╭╰╮╯─│⚕↻]+")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SENT_END = re.compile(r"([。！？\.!?][\"'）)\]」』]?\s*)")
HERMES_STREAM = env("HERMES_STREAM", "true").strip().lower() in ("1", "true", "yes", "on")
STREAM_MIN_CHARS = int(env("HERMES_STREAM_MIN_CHARS", "8"))
USE_CURSOR_CLI = Path(HERMES_BIN).name in {"agent", "cursor-agent"}
# 唤醒：先播 HERMES_WAKE_PENDING_TEXT，再用 HERMES_ASYNC_CURSOR_TIMEOUT_S 等 Cursor；结束后自动 TTS（长文截断播报、全文 post_chat）
HERMES_WAKE_PENDING_TTS = env("HERMES_WAKE_PENDING_TTS", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
HERMES_WAKE_PENDING_TEXT = env(
    "HERMES_WAKE_PENDING_TEXT",
    "好的，正在处理，完成后我会再说一声。",
).strip()
try:
    HERMES_ASYNC_CURSOR_TIMEOUT_S = int(env("HERMES_ASYNC_CURSOR_TIMEOUT_S", "600"))
except ValueError:
    HERMES_ASYNC_CURSOR_TIMEOUT_S = 600
HERMES_ASYNC_CURSOR_TIMEOUT_S = max(HERMES_TIMEOUT_S, HERMES_ASYNC_CURSOR_TIMEOUT_S)
try:
    HERMES_WAKE_COMPLETION_TTS_MAX_CHARS = int(
        env("HERMES_WAKE_COMPLETION_TTS_MAX_CHARS", "500")
    )
except ValueError:
    HERMES_WAKE_COMPLETION_TTS_MAX_CHARS = 500
HERMES_WAKE_COMPLETION_TTS_MAX_CHARS = max(80, HERMES_WAKE_COMPLETION_TTS_MAX_CHARS)


def _env_bool_default_true(name: str) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


CURSOR_AGENT_TRUST = _env_bool_default_true("CURSOR_AGENT_TRUST")
CURSOR_AGENT_APPROVE_MCPS = _env_bool_default_true("CURSOR_AGENT_APPROVE_MCPS")
CURSOR_AGENT_FORCE = _env_bool_default_true("CURSOR_AGENT_FORCE")


def cursor_agent_subprocess_cwd():
    """cwd for Cursor agent subprocess; None keeps the bridge process cwd."""
    if not USE_CURSOR_CLI:
        return None
    raw = env("CURSOR_AGENT_WORKSPACE", "").strip()
    if not raw:
        return None
    path = os.path.expanduser(raw)
    if os.path.isdir(path):
        return path
    log(f"[cursor] CURSOR_AGENT_WORKSPACE is not a directory: {path!r}")
    return None


# Cursor agent: after first successful reply, add bare --continue (no id on disk).
HERMES_AGENT_CONTINUE_MODE = env("HERMES_AGENT_CONTINUE_MODE", "off").strip().lower()
_agent_cli_continue_lock = threading.Lock()
_agent_cli_add_continue_next = False

ACK_ENABLED = env("ACK_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
ACK_TEXT = env("ACK_TEXT", "收到，请稍等").strip() or "收到，请稍等"
ACK_AUDIO_FORMAT = env("ACK_AUDIO_FORMAT", "wav").strip().lower() or "wav"
ACK_CACHE_FILE = os.path.join(TTS_CACHE_DIR, f"ack_wait.{ACK_AUDIO_FORMAT}")


def load_session_id():
    p = session_state_path()
    if os.path.exists(p):
        s = open(p, encoding="utf-8").read().strip()
        return s or None
    return None


def save_session_id(sid):
    if not session_policy.should_persist_captured_session():
        log("[cursor] session id not stored (HERMES_SESSION_POLICY or HERMES_SESSION_PERSIST)")
        return
    try:
        with open(session_state_path(), "w", encoding="utf-8") as f:
            f.write(sid)
    except Exception as e:
        log(f"[cursor] failed to save session id: {e!r}")


def clear_session_id():
    try:
        os.remove(session_state_path())
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"[cursor] failed to clear session id: {e!r}")
    _reset_agent_cli_continue_state()


def _reset_agent_cli_continue_state():
    global _agent_cli_add_continue_next
    with _agent_cli_continue_lock:
        _agent_cli_add_continue_next = False


def _mark_agent_cli_continue_after_success():
    if HERMES_AGENT_CONTINUE_MODE != "after_first" or not USE_CURSOR_CLI:
        return
    global _agent_cli_add_continue_next
    with _agent_cli_continue_lock:
        _agent_cli_add_continue_next = True


def _agent_cli_use_bare_continue() -> bool:
    if HERMES_AGENT_CONTINUE_MODE != "after_first" or not USE_CURSOR_CLI:
        return False
    with _agent_cli_continue_lock:
        return _agent_cli_add_continue_next


def _skip_save_cursor_capture() -> bool:
    """Meeting-bound resume uses deterministic --resume id; avoid overwriting alias.session."""
    return USE_CURSOR_CLI and HERMES_CURSOR_RESUME_MEETING_ID and bool(get_meeting_id())


def _resolve_cursor_resume_sid(resume_sid=None):
    """Return (sid_or_none, policy_reason). Explicit resume_sid skips meeting/file."""
    if resume_sid is not None:
        return (resume_sid or None), "explicit"
    if USE_CURSOR_CLI and HERMES_CURSOR_RESUME_MEETING_ID:
        mid = get_meeting_id()
        if mid:
            return mid.strip(), "meeting_native_id"
    return session_policy.resolve_resume_id(session_state_path())


def _is_stale_session_text(text):
    return bool(text and _STALE_SESSION_RE.search(text))


def _cursor_cmd(prompt, resume_sid=None):
    sid, policy_reason = _resolve_cursor_resume_sid(resume_sid)
    if resume_sid is None:
        log(f"[cursor] session: {policy_reason}")

    if USE_CURSOR_CLI and policy_reason and "policy_new" in policy_reason:
        _reset_agent_cli_continue_state()

    if USE_CURSOR_CLI:
        cmd = [HERMES_BIN]
        if sid:
            cmd += session_policy.cursor_cli_resume_fragment(sid)
        elif _agent_cli_use_bare_continue():
            f = session_policy.bare_continue_flag()
            if not f.startswith("-"):
                f = f"--{f}"
            cmd += [f]
            log(
                f"[cursor] argv includes {f!r} "
                f"(HERMES_AGENT_CONTINUE_MODE=after_first)"
            )
        cmd += [
            "-p",
            f"{SYSTEM_PRIMER}\n\nUser said: {prompt}",
            "--output-format",
            "text",
        ]
        if CURSOR_AGENT_FORCE:
            cmd += ["--force"]
        if CURSOR_AGENT_TRUST:
            cmd += ["--trust"]
        if CURSOR_AGENT_APPROVE_MCPS:
            cmd += ["--approve-mcps"]
        if HERMES_MODEL:
            cmd += ["--model", HERMES_MODEL]
        return cmd, sid

    cmd = [
        HERMES_BIN, "chat",
        "-q", f"{SYSTEM_PRIMER}\n\nUser said: {prompt}",
        "-Q", "--source", "tool",
        "--yolo",
    ]
    if HERMES_MODEL:
        cmd += ["-m", HERMES_MODEL]
    if HERMES_PROVIDER:
        cmd += ["--provider", HERMES_PROVIDER]
    if sid:
        cmd += ["--resume", sid]
    return cmd, sid


def _clean_cursor_output(raw_text):
    """Strip ANSI / box / banner noise; return list of clean answer lines + maybe session id."""
    text = _ANSI_RE.sub("", raw_text)
    answer_lines = []
    captured_sid = None
    for line in text.splitlines():
        s = _BOX_CHARS_RE.sub("", line).strip()
        if not s:
            continue
        m = _SESSION_LINE.match(s)
        if m:
            captured_sid = m.group(1)
            continue
        if _BANNER.match(s):
            continue
        if _is_stale_session_text(s):
            continue
        answer_lines.append(s)
    if not captured_sid:
        captured_sid = _extract_session_id_fuzzy(text)
    return answer_lines, captured_sid


def ask_cursor_streaming(
    prompt, on_sentence, allow_retry=True, resume_sid=None, timeout_s=None
):
    """Run Cursor CLI via Popen, split stdout into sentences, and call
    on_sentence(text) the moment each sentence is complete.
    Returns (full_answer, captured_sid).

    on_sentence may be called from the same thread (sequential)."""
    cmd, sid = _cursor_cmd(prompt, resume_sid=resume_sid)
    log(f"[cursor] ask streaming (model={HERMES_MODEL or 'default'}, resume={sid!r}): {prompt!r}")

    cwd = cursor_agent_subprocess_cwd()
    if cwd:
        log(f"[cursor] agent cwd={cwd!r}")

    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        cwd=cwd,
    )
    raw_buf = b""
    line_buf = ""
    full_raw_parts = []
    pending_clean = ""
    fired = 0
    _to = HERMES_TIMEOUT_S if timeout_s is None else int(timeout_s)
    deadline = time.time() + _to

    def emit_sentences():
        nonlocal pending_clean, fired
        while True:
            m = _SENT_END.search(pending_clean)
            if not m:
                break
            end = m.end()
            sentence = pending_clean[:end].strip()
            pending_clean = pending_clean[end:]
            if not sentence or len(sentence) < STREAM_MIN_CHARS:
                continue
            fired += 1
            try:
                on_sentence(sentence, fired == 1)
            except Exception as e:
                log(f"[cursor-stream] on_sentence err: {e!r}")

    try:
        while True:
            if time.time() > deadline:
                p.kill()
                break
            chunk = p.stdout.read(64)
            if not chunk:
                break
            raw_buf += chunk
            try:
                decoded = raw_buf.decode("utf-8")
                raw_buf = b""
            except UnicodeDecodeError:
                continue
            full_raw_parts.append(decoded)
            line_buf += decoded
            while "\n" in line_buf:
                line, line_buf = line_buf.split("\n", 1)
                line = line.rstrip("\r")
                cleaned, _ = _clean_cursor_output(line)
                if not cleaned:
                    continue
                pending_clean += " ".join(cleaned) + " "
                emit_sentences()
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()

    if line_buf.strip():
        full_raw_parts.append(line_buf)
        cleaned, _ = _clean_cursor_output(line_buf)
        if cleaned:
            pending_clean += " ".join(cleaned) + " "
            emit_sentences()
    if pending_clean.strip() and len(pending_clean.strip()) >= STREAM_MIN_CHARS:
        fired += 1
        try:
            on_sentence(pending_clean.strip(), fired == 1)
        except Exception as e:
            log(f"[cursor-stream] on_sentence tail err: {e!r}")
        pending_clean = ""

    raw = "".join(full_raw_parts)
    cleaned_lines, captured_sid = _clean_cursor_output(raw)
    seen = set()
    deduped = []
    for ln in cleaned_lines:
        if ln in seen:
            continue
        seen.add(ln)
        deduped.append(ln)
    full_answer = " ".join(deduped).strip()
    if _is_stale_session_text(raw) or _is_stale_session_text(full_answer):
        if sid and allow_retry:
            log("[cursor-stream] stale session; clearing and retrying once")
            clear_session_id()
            return ask_cursor_streaming(
                prompt,
                on_sentence,
                allow_retry=False,
                resume_sid="",
                timeout_s=timeout_s,
            )
    if full_answer.strip() and not _is_stale_session_text(full_answer):
        _mark_agent_cli_continue_after_success()
    return full_answer, captured_sid


def ask_cursor(prompt, allow_retry=True, resume_sid=None, timeout_s=None):
    cmd, sid = _cursor_cmd(prompt, resume_sid=resume_sid)
    _to = HERMES_TIMEOUT_S if timeout_s is None else int(timeout_s)
    log(
        f"[cursor] ask (model={HERMES_MODEL or 'default'}, resume={sid!r}, "
        f"timeout={_to}s): {prompt!r}"
    )
    cwd = cursor_agent_subprocess_cwd()
    if cwd:
        log(f"[cursor] agent cwd={cwd!r}")
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_to,
            check=False,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        log("[cursor] timeout")
        return "Sorry, I'm taking too long to answer."
    if r.returncode != 0:
        log(f"[cursor] non-zero rc={r.returncode}: {r.stderr.strip()[:300]}")

    answer_lines, captured_sid = _clean_cursor_output(r.stdout)
    if not captured_sid:
        captured_sid = _extract_session_id_fuzzy((r.stderr or ""))

    if captured_sid and captured_sid != sid and not _skip_save_cursor_capture():
        save_session_id(captured_sid)
        log(f"[cursor] session id stored: {captured_sid}")
    elif (
        USE_CURSOR_CLI
        and not captured_sid
        and r.returncode == 0
        and os.environ.get("HERMES_LOG_AGENT_IO", "").strip().lower()
        in ("1", "true", "yes", "on")
    ):
        snip = ((r.stdout or "") + "\n" + (r.stderr or ""))[:1500]
        log(f"[cursor] HERMES_LOG_AGENT_IO (no session_id parsed): {snip!r}")

    seen = set()
    deduped = []
    for ln in answer_lines:
        if ln in seen:
            continue
        seen.add(ln)
        deduped.append(ln)
    answer = " ".join(deduped).strip()
    if (
        _is_stale_session_text(r.stdout)
        or _is_stale_session_text(r.stderr)
        or _is_stale_session_text(answer)
    ):
        if sid and allow_retry:
            log("[cursor] stale session; clearing and retrying once")
            clear_session_id()
            return ask_cursor(
                prompt, allow_retry=False, resume_sid="", timeout_s=timeout_s
            )
    if not answer:
        log(f"[cursor] empty answer; raw: {r.stdout!r}")
        return "Sorry, I have no answer right now."

    if (
        r.returncode == 0
        and answer
        and not _is_stale_session_text(answer)
        and not _is_stale_session_text(r.stdout)
    ):
        _mark_agent_cli_continue_after_success()

    log(f"[cursor] reply: {answer!r}")
    return answer


def log(line):
    print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)


_WAKE_PUNCT = "，。,.!！?？:：;； \t\n\r"


def strip_wake_word(text):
    """Return query with wake word removed, or None if no wake word matched.

    Match is case-insensitive substring. Wake word may sit anywhere in the text.
    Designed to tolerate Chinese ASR mishearings, so the wake word list should
    include common phonetic variants (e.g. Hermes / Harmony / 赫尔墨斯 / 小赫 /
    小贺 / 助手 ...).
    """
    if not text:
        return None
    lowered = text.lower()
    for w in HERMES_WAKE_WORDS:
        wl = w.lower()
        idx = lowered.find(wl)
        if idx < 0:
            continue
        rest = text[:idx] + text[idx + len(w) :]
        rest = rest.strip(_WAKE_PUNCT).strip()
        return rest
    return None


BOT_ADMIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Vexa — Bot 管理</title>
<style>
  :root { font-family: system-ui, sans-serif; background:#0f1419; color:#e7e9ea; }
  body { max-width: 920px; margin: 1.25rem auto; padding: 0 1rem 3rem; }
  h1 { font-size: 1.35rem; font-weight: 600; margin-bottom: .25rem; }
  .nav { margin-bottom: 1.25rem; font-size: .9rem; }
  .nav a { color:#1d9bf0; text-decoration:none; }
  .nav a:hover { text-decoration:underline; }
  section { margin-top: 1.75rem; padding: 1rem 1.15rem; border-radius:12px; border:1px solid #38444d; background:#15202b; }
  section h2 { font-size: 1rem; font-weight: 600; margin: 0 0 .85rem; color:#ccd6dd; }
  label { display:block; margin-top:.75rem; font-size:.8rem; color:#8b98a5; }
  input, select { width:100%; box-sizing:border-box; margin-top:.3rem; padding:.55rem .65rem;
    border-radius:8px; border:1px solid #38444d; background:#0f1419; color:inherit; font-size:.95rem; }
  .row { display:flex; gap:.75rem; flex-wrap: wrap; }
  .row > * { flex: 1 1 160px; }
  button { margin-top:.85rem; padding:.55rem 1rem; border:none; border-radius:8px;
    background:#1d9bf0; color:#fff; font-weight:600; font-size:.9rem; cursor:pointer; }
  button.secondary { background:#38444d; }
  button.danger { background:#9d174d; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  table { width:100%; border-collapse:collapse; font-size:.85rem; }
  th, td { text-align:left; padding:.5rem .45rem; border-bottom:1px solid #38444d; vertical-align:top; }
  th { color:#8b98a5; font-weight:500; }
  tr:hover td { background:#1a2733; }
  .badge { display:inline-block; padding:.12rem .45rem; border-radius:999px; font-size:.72rem; font-weight:600; }
  .st-active { background:#064e3b; color:#6ee7b7; }
  .st-completed { background:#374151; color:#d1d5db; }
  .st-failed { background:#7f1d1d; color:#fecaca; }
  .mono { font-family: ui-monospace, monospace; font-size:.78rem; word-break:break-all; }
  pre { margin:0; padding:.75rem; border-radius:8px; background:#0f1419; border:1px solid #38444d;
    font-size:.78rem; overflow:auto; max-height:60vh; white-space:pre-wrap; word-break:break-word; }
  .err { color:#f87171; font-size:.85rem; margin-top:.5rem; }
  .ok { color:#6ee7b7; font-size:.85rem; margin-top:.5rem; }
  #detail-overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,.65); z-index:40; align-items:center; justify-content:center; padding:1rem; }
  #detail-overlay.open { display:flex; }
  #detail-box { width:min(720px,100%); max-height:90vh; overflow:auto; background:#15202b; border:1px solid #38444d; border-radius:12px; padding:1rem 1.15rem; }
  #detail-box h3 { margin:0 0 .75rem; font-size:1rem; }
</style>
</head>
<body>
  <p class="nav"><a href="/">← Meet 派 Bot</a></p>
  <h1>Bot 管理</h1>
  <p style="font-size:.85rem;color:#8b98a5;margin:.35rem 0 0;">列表来自 Vexa <code>GET /bots</code>；操作经 bridge 代理，密钥不暴露在浏览器。</p>

  <section>
    <h2>添加 Bot</h2>
    <div class="row">
      <div>
        <label for="addMeet">Meet 链接或会议代码</label>
        <input id="addMeet" type="text" placeholder="https://meet.google.com/xxx-yyyy-zzz 或 xxx-yyyy-zzz"/>
      </div>
      <div>
        <label for="addPlat">平台</label>
        <select id="addPlat">
          <option value="google_meet">google_meet</option>
          <option value="zoom">zoom</option>
          <option value="teams">teams</option>
        </select>
      </div>
      <div>
        <label for="addName">Bot 显示名（可选）</label>
        <input id="addName" type="text" placeholder="可选"/>
      </div>
    </div>
    <button id="btnAdd" type="button">提交添加</button>
    <p id="addMsg" class="err" hidden></p>
  </section>

  <section>
    <h2>会议 / Bot 列表</h2>
    <p style="margin:0 0 .65rem;">
      <button id="btnRefresh" type="button" class="secondary">刷新</button>
    </p>
    <div id="listErr" class="err" hidden></div>
    <div style="overflow-x:auto;">
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>平台</th>
            <th>会议码</th>
            <th>状态</th>
            <th>开始时间</th>
            <th></th>
          </tr>
        </thead>
        <tbody id="tbody"><tr><td colspan="6">加载中…</td></tr></tbody>
      </table>
    </div>
  </section>

  <div id="detail-overlay" role="dialog" aria-modal="true">
    <div id="detail-box">
      <h3 id="detail-title">详情</h3>
      <pre id="detail-pre"></pre>
      <button type="button" class="secondary" id="detail-close">关闭</button>
    </div>
  </div>

<script>
(function(){
  var tbody = document.getElementById('tbody');
  var listErr = document.getElementById('listErr');
  var overlay = document.getElementById('detail-overlay');
  var detailPre = document.getElementById('detail-pre');
  var detailTitle = document.getElementById('detail-title');

  function badge(status){
    var c = 'badge ';
    if (status === 'active') c += 'st-active';
    else if (status === 'failed') c += 'st-failed';
    else c += 'st-completed';
    return '<span class="' + c + '">' + (status || '-') + '</span>';
  }

  function loadList(){
    listErr.hidden = true;
    tbody.innerHTML = '<tr><td colspan="6">加载中…</td></tr>';
    fetch('/api/vexa/bots').then(function(r){
      return r.json().then(function(j){ return { ok:r.ok, status:r.status, body:j }; });
    }).then(function(x){
      if (!x.ok) {
        listErr.textContent = 'HTTP ' + x.status + ': ' + JSON.stringify(x.body);
        listErr.hidden = false;
        tbody.innerHTML = '<tr><td colspan="6">加载失败</td></tr>';
        return;
      }
      var meetings = x.body.meetings || [];
      if (!meetings.length) {
        tbody.innerHTML = '<tr><td colspan="6">暂无记录</td></tr>';
        return;
      }
      tbody.innerHTML = meetings.map(function(m){
        var nid = (m.native_meeting_id || '').replace(/"/g,'&quot;');
        var plat = (m.platform || '').replace(/"/g,'&quot;');
        return '<tr>' +
          '<td>' + (m.id != null ? m.id : '') + '</td>' +
          '<td class="mono">' + (m.platform || '') + '</td>' +
          '<td class="mono">' + (m.native_meeting_id || '') + '</td>' +
          '<td>' + badge(m.status) + '</td>' +
          '<td class="mono">' + (m.start_time || '').slice(0,19).replace('T',' ') + '</td>' +
          '<td>' +
            '<button type="button" class="secondary btn-detail" data-id="' + m.id + '">详情</button> ' +
            (m.status === 'active'
              ? '<button type="button" class="danger btn-stop" data-plat="' + plat + '" data-nid="' + nid + '">关停</button>'
              : '') +
          '</td></tr>';
      }).join('');
      tbody.querySelectorAll('.btn-detail').forEach(function(btn){
        btn.onclick = function(){
          var id = btn.getAttribute('data-id');
          detailTitle.textContent = '详情 meeting_id=' + id;
          detailPre.textContent = '加载中…';
          overlay.classList.add('open');
          fetch('/api/vexa/bots/id/' + id).then(function(r){ return r.json(); }).then(function(j){
            detailPre.textContent = JSON.stringify(j, null, 2);
          }).catch(function(e){
            detailPre.textContent = String(e);
          });
        };
      });
      tbody.querySelectorAll('.btn-stop').forEach(function(btn){
        btn.onclick = function(){
          var plat = btn.getAttribute('data-plat');
          var nid = btn.getAttribute('data-nid');
          if (!confirm('确定关停平台 ' + plat + ' 会议 ' + nid + ' ？')) return;
          fetch('/api/vexa/bots/' + encodeURIComponent(plat) + '/' + encodeURIComponent(nid), {
            method: 'DELETE'
          }).then(function(r){
            return r.json().then(function(j){ return { status: r.status, body: j }; });
          }).then(function(x){
            if (x.status >= 200 && x.status < 300) loadList();
            else alert('关停失败 HTTP ' + x.status + '\\n' + JSON.stringify(x.body));
          }).catch(function(e){ alert(String(e)); });
        };
      });
    }).catch(function(e){
      listErr.textContent = String(e);
      listErr.hidden = false;
      tbody.innerHTML = '<tr><td colspan="6">加载失败</td></tr>';
    });
  }

  document.getElementById('btnRefresh').onclick = loadList;
  document.getElementById('detail-close').onclick = function(){ overlay.classList.remove('open'); };
  overlay.addEventListener('click', function(ev){
    if (ev.target === overlay) overlay.classList.remove('open');
  });

  document.getElementById('btnAdd').onclick = function(){
    var msg = document.getElementById('addMsg');
    msg.hidden = true;
    var meeting = document.getElementById('addMeet').value.trim();
    var plat = document.getElementById('addPlat').value;
    var name = document.getElementById('addName').value.trim();
    if (!meeting) {
      msg.textContent = '请填写 Meet 链接或会议代码';
      msg.hidden = false;
      return;
    }
    var body = { platform: plat, voice_agent_enabled: true };
    if (meeting.indexOf('http') === 0 || meeting.indexOf('meet.') !== -1) {
      body.meeting_url = meeting;
    } else {
      body.native_meeting_id = meeting;
    }
    if (name) body.bot_name = name;
    var btn = document.getElementById('btnAdd');
    btn.disabled = true;
    fetch('/api/vexa/bots', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function(r){
      return r.json().then(function(j){ return { ok:r.ok, status:r.status, body:j }; });
    }).then(function(x){
      if (x.ok && x.status >= 200 && x.status < 300) {
        msg.className = 'ok';
        msg.textContent = '已提交：' + JSON.stringify(x.body).slice(0, 400);
        msg.hidden = false;
        document.getElementById('addMeet').value = '';
        loadList();
      } else {
        msg.className = 'err';
        msg.textContent = 'HTTP ' + x.status + ' ' + JSON.stringify(x.body);
        msg.hidden = false;
      }
    }).catch(function(e){
      msg.className = 'err';
      msg.textContent = String(e);
      msg.hidden = false;
    }).finally(function(){ btn.disabled = false; });
  };

  loadList();
})();
</script>
</body>
</html>"""


MEET_BOT_PANEL_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Vexa — 派 Bot 进 Google Meet</title>
<style>
  :root { font-family: system-ui, sans-serif; background:#0f1419; color:#e7e9ea; }
  body { max-width: 520px; margin: 2rem auto; padding: 0 1rem; }
  h1 { font-size: 1.25rem; font-weight: 600; }
  label { display:block; margin-top:1rem; font-size:.875rem; color:#8b98a5; }
  input { width:100%; box-sizing:border-box; margin-top:.35rem; padding:.65rem .75rem;
    border-radius:8px; border:1px solid #38444d; background:#15202b; color:inherit; font-size:1rem; }
  button { margin-top:1.25rem; width:100%; padding:.75rem; border:none; border-radius:8px;
    background:#1d9bf0; color:#fff; font-weight:600; font-size:1rem; cursor:pointer; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  pre { margin-top:1rem; padding:1rem; border-radius:8px; background:#15202b; border:1px solid #38444d;
    font-size:.8rem; overflow:auto; white-space:pre-wrap; word-break:break-word; }
  .hint { font-size:.8rem; color:#8b98a5; margin-top:1.5rem; line-height:1.5; }
  .ok { color:#00ba7c; }
  .err { color:#f4212e; }
</style>
</head>
<body>
  <h1>派 Bot 进入 Google Meet</h1>
  <p class="nav" style="margin-bottom:1rem;"><a href="/bots-admin">Bot 管理（列表 / 关停 / 详情）</a></p>
  <label for="m">Meet 链接或会议代码</label>
  <input id="m" type="text" placeholder="https://meet.google.com/xxx-yyyy-zzz 或 xxx-yyyy-zzz" autocomplete="off"/>
  <label for="n">Bot 显示名（可选）</label>
  <input id="n" type="text" placeholder="留空则使用 Vexa 默认" autocomplete="off"/>
  <button id="go" type="button">发送 Bot</button>
  <pre id="out" hidden></pre>
  <p class="hint" id="curmeet">当前 bridge 使用的会议代码：加载中…</p>
  <p class="hint">发送成功后，bridge 会自动把 <code>speak</code>/<code>chat</code> 指到该会议。也可单独调用
  <code>POST /api/set-meeting</code>（JSON：<code>{"meeting":"xxx-yyyy-zzz"}</code>）只改会议码不重派 bot。</p>
  <p class="hint">密钥由 bridge 服务端注入。手机访问请 <code>BRIDGE_BIND=0.0.0.0</code>，用局域网 IP 打开本页。</p>
<script>
(function(){
  function refreshCurMeet(){
    var el = document.getElementById('curmeet');
    if (!el) return;
    fetch('/health').then(function(r){ return r.json(); }).then(function(h){
      var v = h.meeting_id;
      el.textContent = '当前 bridge 使用的会议代码：' + (v || '（未设置 — 请先发送 Bot 或 /api/set-meeting）');
    }).catch(function(){
      el.textContent = '当前会议：无法读取 /health';
    });
  }
  refreshCurMeet();
  var m = document.getElementById('m');
  var n = document.getElementById('n');
  var go = document.getElementById('go');
  var out = document.getElementById('out');
  go.onclick = function(){
    out.hidden = false;
    out.className = '';
    out.textContent = '请求中…';
    go.disabled = true;
    fetch('/api/send-bot', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        meeting: m.value.trim(),
        bot_name: n.value.trim() || undefined
      })
    }).then(function(r){
      return r.text().then(function(t){
        try { return { status: r.status, body: JSON.parse(t) }; }
        catch (e) { return { status: r.status, body: { error: t || '(empty)' } }; }
      });
    }).then(function(x){
      if (x.status >= 200 && x.status < 300 && x.body.ok) {
        out.className = 'ok';
        out.textContent = '已提交。会议代码：' + (x.body.native_meeting_id || '') + '\\n\\n' +
          JSON.stringify(x.body.vexa || {}, null, 2);
        refreshCurMeet();
      } else {
        out.className = 'err';
        out.textContent = 'HTTP ' + x.status + '\\n' + JSON.stringify(x.body, null, 2);
      }
    }).catch(function(e){
      out.className = 'err';
      out.textContent = String(e);
    }).finally(function(){ go.disabled = false; });
  };
})();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode())

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/panel"):
            data = MEET_BOT_PANEL_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path in ("/bots-admin", "/admin/bots"):
            data = BOT_ADMIN_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/vexa/bots":
            status, resp = http_call("GET", "/bots", None, timeout_s=45)
            code = status if status else 502
            if not isinstance(resp, dict):
                resp = {"detail": resp}
            self._json(code, resp)
            return
        m_bid = re.match(r"^/api/vexa/bots/id/(\d+)$", path)
        if m_bid:
            mid = m_bid.group(1)
            status, resp = http_call("GET", f"/bots/id/{mid}", None, timeout_s=45)
            code = status if status else 502
            if not isinstance(resp, dict):
                resp = {"detail": resp}
            self._json(code, resp)
            return
        if path == "/health":
            mid = get_meeting_id()
            self._json(200, {
                "ok": True,
                "platform": PLATFORM,
                "meeting_id": mid or None,
                "meeting_id_env_fallback": MEETING_ID or None,
                "voice": HERMES_VOICE,
                "session_alias": hermes_session_key(),
                "current_session_id": load_session_id(),
                "tts_provider": TTS_PROVIDER,
                "doubao_configured": bool(DOUBAO_APPID and DOUBAO_TOKEN),
                "doubao_voice": DOUBAO_VOICE if TTS_PROVIDER == "doubao" else None,
                "chat_in_endpoint": "/api/chat-in",
                "chat_webhook_secret_configured": bool(BRIDGE_CHAT_WEBHOOK_SECRET),
            })
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        m = re.match(r"^/api/vexa/bots/([^/]+)/([^/]+)$", path)
        if not m:
            return self._json(404, {"error": "not found"})
        plat, nid = m.group(1), m.group(2)
        status, resp = http_call(
            "DELETE",
            f"/bots/{quote(plat, safe='')}/{quote(nid, safe='')}",
            None,
            timeout_s=90,
        )
        code = status if status else 502
        if not isinstance(resp, dict):
            resp = {"detail": resp}
        self._json(code, resp)

    def do_POST(self):
        try:
            body = self._read_json()
        except Exception as e:
            return self._json(400, {"error": f"bad json: {e}"})

        path = self.path.split("?", 1)[0]

        if path in ("/api/chat-in", "/webhooks/vexa-chat"):
            return self._handle_chat_in(body)

        if path == "/api/vexa/bots":
            vexa_body = {}
            if body.get("meeting_url"):
                vexa_body["meeting_url"] = str(body["meeting_url"]).strip()
            elif body.get("native_meeting_id"):
                vexa_body["native_meeting_id"] = str(body["native_meeting_id"]).strip()
                vexa_body["platform"] = (body.get("platform") or PLATFORM or "google_meet").strip()
            elif body.get("meeting") or body.get("url"):
                meeting_raw = (body.get("meeting") or body.get("url") or "").strip()
                try:
                    native_id = parse_google_meet_native_id(meeting_raw)
                except ValueError:
                    return self._json(
                        400,
                        {"ok": False, "error": "无效的 Meet 链接或会议代码"},
                    )
                vexa_body["platform"] = (body.get("platform") or PLATFORM or "google_meet").strip()
                vexa_body["native_meeting_id"] = native_id
            else:
                return self._json(
                    400,
                    {"ok": False, "error": "需要 meeting_url、native_meeting_id 或 meeting/url"},
                )
            bn = (body.get("bot_name") or "").strip()
            if bn:
                vexa_body["bot_name"] = bn
            if "voice_agent_enabled" in body:
                vexa_body["voice_agent_enabled"] = bool(body["voice_agent_enabled"])
            else:
                vexa_body["voice_agent_enabled"] = VEXA_VOICE_AGENT_ENABLED
            status, resp = http_call("POST", "/bots", vexa_body, timeout_s=90)
            if status == 0:
                return self._json(
                    502,
                    {"ok": False, "error": resp.get("error", "无法连接 Vexa"), "detail": resp},
                )
            if status >= 400:
                return self._json(status, {"ok": False, "detail": resp})
            nid_cache = vexa_body.get("native_meeting_id")
            if not nid_cache and vexa_body.get("meeting_url"):
                try:
                    nid_cache = parse_google_meet_native_id(str(vexa_body["meeting_url"]))
                except ValueError:
                    nid_cache = None
            if nid_cache:
                set_current_meeting_id(nid_cache)
                log(f"[meeting] active native_meeting_id set to {nid_cache!r} (POST /api/vexa/bots ok)")
                schedule_meeting_join_greeting(nid_cache, resp)
            return self._json(status, {"ok": True, "vexa": resp})

        if path == "/api/send-bot":
            meeting_raw = (
                body.get("meeting")
                or body.get("url")
                or body.get("native_meeting_id")
                or ""
            )
            if isinstance(meeting_raw, str):
                meeting_raw = meeting_raw.strip()
            else:
                meeting_raw = ""
            try:
                native_id = parse_google_meet_native_id(meeting_raw)
            except ValueError:
                return self._json(
                    400,
                    {"ok": False, "error": "请粘贴有效的 Google Meet 链接或会议代码（如 xxx-yyyy-zzz）"},
                )
            plat = (body.get("platform") or PLATFORM or "google_meet").strip()
            bot_name = (body.get("bot_name") or "").strip() or None
            vexa_body = {"platform": plat, "native_meeting_id": native_id}
            if bot_name:
                vexa_body["bot_name"] = bot_name
            if "voice_agent_enabled" in body:
                vexa_body["voice_agent_enabled"] = bool(body["voice_agent_enabled"])
            else:
                vexa_body["voice_agent_enabled"] = VEXA_VOICE_AGENT_ENABLED
            status, resp = http_call("POST", "/bots", vexa_body, timeout_s=90)
            if status == 0:
                return self._json(
                    502,
                    {
                        "ok": False,
                        "native_meeting_id": native_id,
                        "error": resp.get("error", "无法连接 Vexa"),
                        "detail": resp,
                    },
                )
            if status >= 400:
                return self._json(
                    status,
                    {
                        "ok": False,
                        "native_meeting_id": native_id,
                        "error": resp.get("error", "Vexa 拒绝请求"),
                        "detail": resp,
                    },
                )
            set_current_meeting_id(native_id)
            log(f"[meeting] active native_meeting_id set to {native_id!r} (send-bot ok)")
            schedule_meeting_join_greeting(native_id, resp)
            return self._json(
                200,
                {"ok": True, "native_meeting_id": native_id, "vexa": resp},
            )

        if path == "/api/set-meeting":
            meeting_raw = (
                body.get("meeting")
                or body.get("url")
                or body.get("native_meeting_id")
                or ""
            )
            if isinstance(meeting_raw, str):
                meeting_raw = meeting_raw.strip()
            else:
                meeting_raw = ""
            try:
                native_id = parse_google_meet_native_id(meeting_raw)
            except ValueError:
                return self._json(
                    400,
                    {"ok": False, "error": "请提供有效的 Meet 链接或会议代码"},
                )
            set_current_meeting_id(native_id)
            log(f"[meeting] active native_meeting_id set to {native_id!r} (set-meeting)")
            return self._json(200, {"ok": True, "native_meeting_id": native_id})

        if path == "/tx":
            return self._handle_tx(body)

        text = (body.get("text") or "").strip()
        if not text:
            return self._json(400, {"error": "missing 'text'"})

        if path == "/raw":
            sc, _ = speak(text)
            cc, _ = post_chat(text)
            log(f"[raw] speak={sc} chat={cc}")
            return self._json(200, {"spoken": text, "speak_status": sc, "chat_status": cc})

        if path == "/say":
            ack_sc, _ = speak_ack()
            log(f"[say] ack speak={ack_sc}")
            answer = ask_cursor(text)
            sc, _ = speak(answer)
            cc, _ = post_chat(answer)
            log(f"[say] speak={sc} chat={cc}")
            return self._json(200, {
                "input": text, "answer": answer,
                "speak_status": sc, "chat_status": cc,
                "session_id": load_session_id(),
            })

        return self._json(404, {"error": "not found"})

    def _handle_chat_in(self, body):
        """Inbound Meet chat webhook → Cursor reply path（不打语音 ACK）。"""
        if not isinstance(body, dict):
            body = {}
        if not _chat_in_check_secret(self):
            log("[chat-in] unauthorized")
            return self._json(401, {"ok": False, "error": "unauthorized"})
        raw, nid_body, msg_id = _parse_inbound_chat_body(body)
        if not raw.strip():
            return self._json(400, {"ok": False, "error": "missing text"})
        if msg_id and _chat_in_duplicate(msg_id):
            log(f"[chat-in] dedupe {msg_id!r}")
            return self._json(200, {"ok": True, "deduped": True})
        ok, err = _chat_in_resolve_meeting(nid_body)
        if not ok:
            log(f"[chat-in] rejected: {err}")
            return self._json(400, {"ok": False, "error": err})
        if HERMES_CHAT_REQUIRE_MENTION and not _text_has_bot_mention(raw):
            log("[chat-in] skipped (require @mention)")
            return self._json(200, {"ok": True, "skipped": "no_mention"})
        query = _strip_leading_chat_mentions(raw).strip()
        if len(query) < HERMES_CHAT_MIN_LEN:
            return self._json(200, {"ok": True, "skipped": "too_short"})
        now = time.time()
        if now - Handler._last_chat_trigger_at < HERMES_CHAT_COOLDOWN_S:
            log("[chat-in] cooldown")
            return self._json(200, {"ok": True, "skipped": "cooldown"})
        Handler._last_chat_trigger_at = now
        log(f"[chat-in] -> cursor (async): {query!r}")
        threading.Thread(
            target=Handler._run_reply,
            args=(query,),
            name="cursor-chat-in",
            daemon=True,
        ).start()
        return self._json(202, {"ok": True, "accepted": True})

    def _handle_tx(self, body):
        text = (body.get("text") or "").strip()
        if not text:
            return self._json(204, {})
        duration = body.get("duration")
        speaker = body.get("speaker") or ""
        latency_ms = body.get("latency_ms")
        bytes_in = body.get("bytes_in")
        is_final = bool(body.get("final"))
        meta_bits = []
        if speaker:
            meta_bits.append(f"speaker={speaker}")
        if isinstance(duration, (int, float)) and duration > 0:
            meta_bits.append(f"dur={duration:.2f}s")
        if isinstance(latency_ms, (int, float)):
            meta_bits.append(f"asr={int(latency_ms)}ms")
        if isinstance(bytes_in, int):
            meta_bits.append(f"bytes={bytes_in}")
        if not is_final:
            meta_bits.append("partial")
        meta = (" " + " ".join(meta_bits)) if meta_bits else ""
        log(f"[tx]{meta} {text}")

        if HERMES_AUTO_REPLY and WAKE_CAPTURE_ENABLED:
            if is_final:
                self._handle_final_wake_capture(text)
            else:
                self._touch_capture_activity("partial")
        elif is_final and HERMES_AUTO_REPLY:
            self._maybe_trigger_cursor(text)

        return self._json(200, {"ok": True})

    _last_trigger_at = 0.0
    _last_chat_trigger_at = 0.0
    _reply_lock = threading.Lock()
    _capture_lock = threading.Lock()
    _capture_active = False
    _capture_chunks = []
    _capture_started_at = 0.0
    _capture_timer = None

    @classmethod
    def _normalize_capture_chunk(cls, text):
        if not text:
            return ""
        stripped = strip_wake_word(text)
        chunk = stripped if stripped is not None else text
        return chunk.strip(_WAKE_PUNCT).strip()

    @classmethod
    def _merge_capture_chunk_locked(cls, chunk):
        if not chunk:
            return
        if not cls._capture_chunks:
            cls._capture_chunks.append(chunk)
            return
        last = cls._capture_chunks[-1]
        if chunk == last:
            return
        if chunk.startswith(last):
            cls._capture_chunks[-1] = chunk
            return
        if last.startswith(chunk):
            return
        cls._capture_chunks.append(chunk)

    @classmethod
    def _arm_capture_timer_locked(cls):
        if cls._capture_timer is not None:
            cls._capture_timer.cancel()
        cls._capture_timer = threading.Timer(WAKE_CAPTURE_SILENCE_S, cls._flush_capture_timer)
        cls._capture_timer.daemon = True
        cls._capture_timer.start()

    @classmethod
    def _flush_capture_timer(cls):
        cls._flush_capture("silence")

    @classmethod
    def _touch_capture_activity(cls, source):
        now = time.time()
        flush_reason = None
        with cls._capture_lock:
            if not cls._capture_active:
                return
            if (now - cls._capture_started_at) >= WAKE_CAPTURE_MAX_S:
                flush_reason = "max"
            else:
                cls._arm_capture_timer_locked()
        if flush_reason:
            cls._flush_capture(flush_reason)

    @classmethod
    def _finalize_capture_query(cls, chunks):
        out = []
        for c in chunks:
            c = c.strip(_WAKE_PUNCT).strip()
            if not c:
                continue
            if not out:
                out.append(c)
                continue
            prev = out[-1]
            if c == prev or c.startswith(prev):
                out[-1] = c
                continue
            if prev.startswith(c):
                continue
            out.append(c)
        return " ".join(out).strip()

    @classmethod
    def _flush_capture(cls, reason):
        with cls._capture_lock:
            if not cls._capture_active:
                return
            now = time.time()
            dt = now - cls._capture_started_at if cls._capture_started_at else 0.0
            # If we only captured one chunk too quickly, hold a bit longer to
            # absorb the common "short final -> full final" ASR pattern.
            if (
                reason == "silence"
                and len(cls._capture_chunks) <= 1
                and dt < WAKE_CAPTURE_MIN_HOLD_S
            ):
                cls._arm_capture_timer_locked()
                log(
                    f"[wake-capture] hold single chunk "
                    f"age={dt:.1f}s<{WAKE_CAPTURE_MIN_HOLD_S:.1f}s"
                )
                return
            if cls._capture_timer is not None:
                cls._capture_timer.cancel()
                cls._capture_timer = None
            chunks = cls._capture_chunks[:]
            cls._capture_active = False
            cls._capture_chunks = []
            started_at = cls._capture_started_at
            cls._capture_started_at = 0.0

        query = cls._finalize_capture_query(chunks)
        dt = time.time() - started_at if started_at else 0.0
        if not query:
            log(f"[wake-capture] flush reason={reason} after={dt:.1f}s (empty)")
            return
        if len(query) < HERMES_AUTO_MIN_LEN:
            log(f"[wake-capture] flush reason={reason} after={dt:.1f}s (too short): {query!r}")
            return
        log(
            f"[wake-capture] flush reason={reason} after={dt:.1f}s "
            f"chunks={len(chunks)} query={query!r}"
        )
        cls._dispatch_cursor_reply(query)

    @classmethod
    def _dispatch_cursor_reply(cls, query):
        log(f"[wake] -> cursor (async): {query!r}")
        threading.Thread(
            target=cls._run_reply,
            args=(query,),
            name="cursor-reply",
            daemon=True,
        ).start()

    @classmethod
    def _handle_final_wake_capture(cls, text):
        now = time.time()
        need_ack = False
        flush_reason = None

        with cls._capture_lock:
            if cls._capture_active:
                chunk = cls._normalize_capture_chunk(text)
                cls._merge_capture_chunk_locked(chunk)
                if (now - cls._capture_started_at) >= WAKE_CAPTURE_MAX_S:
                    flush_reason = "max"
                else:
                    cls._arm_capture_timer_locked()
                return_flush = flush_reason
            else:
                query = strip_wake_word(text)
                if query is None:
                    return
                if now - cls._last_trigger_at < HERMES_AUTO_COOLDOWN_S:
                    log(f"[wake] cooldown: skip {query!r}")
                    return
                cls._last_trigger_at = now
                cls._capture_active = True
                cls._capture_chunks = []
                cls._capture_started_at = now
                cls._merge_capture_chunk_locked(
                    cls._normalize_capture_chunk(query)
                )
                cls._arm_capture_timer_locked()
                need_ack = True
                return_flush = None
                log("[wake-capture] start")

        if need_ack:
            ack_sc, _ = speak_ack()
            log(f"[wake] ack speak={ack_sc}")
        if return_flush:
            cls._flush_capture(return_flush)

    @classmethod
    def _maybe_trigger_cursor(cls, text):
        query = strip_wake_word(text)
        if query is None:
            return
        if len(query) < HERMES_AUTO_MIN_LEN:
            log(f"[wake] hit but query too short: {query!r}")
            return
        now = time.time()
        if now - cls._last_trigger_at < HERMES_AUTO_COOLDOWN_S:
            log(f"[wake] cooldown: skip {query!r}")
            return
        cls._last_trigger_at = now
        # Speak ACK immediately (outside reply lock) to reduce waiting anxiety.
        ack_sc, _ = speak_ack()
        log(f"[wake] ack speak={ack_sc}")
        log(f"[wake] -> cursor (async): {query!r}")
        threading.Thread(
            target=cls._run_reply,
            args=(query,),
            name="cursor-reply",
            daemon=True,
        ).start()

    @classmethod
    def _run_reply(cls, query):
        with cls._reply_lock:
            try:
                t0 = time.time()
                use_stream = HERMES_STREAM and not HERMES_WAKE_PENDING_TTS
                if HERMES_WAKE_PENDING_TTS and HERMES_STREAM:
                    log("[wake] HERMES_WAKE_PENDING_TTS: forcing oneshot (stream disabled)")
                if use_stream:
                    cls._run_reply_streaming(query, t0)
                else:
                    cls._run_reply_oneshot(query, t0)
            except Exception as e:
                log(f"[wake] ERROR: {e!r}")

    @classmethod
    def _run_reply_oneshot(cls, query, t0):
        cursor_timeout = (
            HERMES_ASYNC_CURSOR_TIMEOUT_S if HERMES_WAKE_PENDING_TTS else None
        )
        if HERMES_WAKE_PENDING_TTS and HERMES_WAKE_PENDING_TEXT:
            try:
                sc, _ = speak(HERMES_WAKE_PENDING_TEXT)
                log(f"[wake] pending-tts speak={sc}")
            except Exception as e:
                log(f"[wake] pending-tts err: {e!r}")
        answer = ask_cursor(query, timeout_s=cursor_timeout)
        t1 = time.time()
        results = {}
        spoken = _wake_completion_speak_text(answer)

        def do_speak():
            results["speak"] = speak(spoken)

        def do_chat():
            results["chat"] = post_chat(answer)

        ts = threading.Thread(target=do_speak, daemon=True)
        tc = threading.Thread(target=do_chat, daemon=True)
        ts.start(); tc.start()
        ts.join(); tc.join()
        t2 = time.time()
        sc = results.get("speak", (0, {}))[0]
        cc = results.get("chat", (0, {}))[0]
        log(
            f"[wake] reply oneshot (cursor={t1 - t0:.1f}s tts+chat={t2 - t1:.1f}s "
            f"speak={sc} chat={cc} pending_tts={HERMES_WAKE_PENDING_TTS}): {answer}"
        )

    @classmethod
    def _run_reply_streaming(cls, query, t0):
        first_speak_at = [None]
        speak_count = [0]

        def on_sentence(sentence, is_first):
            speak_count[0] += 1
            speak_t0 = time.time()
            try:
                sc, _ = speak(sentence)
            except Exception as e:
                log(f"[wake-stream] speak err: {e!r}")
                sc = 0
            speak_dt = time.time() - speak_t0
            if is_first and first_speak_at[0] is None:
                first_speak_at[0] = time.time()
                log(
                    f"[wake-stream] first speak at +{first_speak_at[0] - t0:.1f}s "
                    f"(speak={sc} {speak_dt:.1f}s): {sentence!r}"
                )
            else:
                log(f"[wake-stream] speak#{speak_count[0]} ({speak_dt:.1f}s): {sentence!r}")

        full_answer, captured_sid = ask_cursor_streaming(query, on_sentence)
        t_done = time.time()

        if captured_sid and not _skip_save_cursor_capture():
            save_session_id(captured_sid)
            log(f"[cursor] session id stored: {captured_sid}")

        if speak_count[0] == 0 and full_answer:
            log("[wake-stream] no sentences detected; falling back to one-shot speak")
            try:
                speak(full_answer)
            except Exception as e:
                log(f"[wake-stream] fallback speak err: {e!r}")

        if full_answer:
            try:
                cc, _ = post_chat(full_answer)
            except Exception as e:
                log(f"[wake-stream] chat err: {e!r}")
                cc = 0
        else:
            cc = 0

        first_at = (
            f"+{first_speak_at[0] - t0:.1f}s"
            if first_speak_at[0] is not None
            else "n/a"
        )
        log(
            f"[wake] reply stream (total={t_done - t0:.1f}s, "
            f"first_speak={first_at}, "
            f"sentences={speak_count[0]} chat={cc}): {full_answer!r}"
        )


def main():
    mid = get_meeting_id()
    resume_note = ""
    if USE_CURSOR_CLI and HERMES_CURSOR_RESUME_MEETING_ID and mid:
        resume_note = f"  cursor_resume_meeting={mid!r}"
    log(
        f"bridge http on http://{BIND}:{PORT}  meet-panel=http://{BIND}:{PORT}/  "
        f"bots-admin=http://{BIND}:{PORT}/bots-admin  "
        f"chat-in=http://{BIND}:{PORT}/api/chat-in  "
        f"meeting={mid or '(unset — use panel or MEETING_ID)'}  "
        f"alias={hermes_session_key()}  resume_file={load_session_id()!r}{resume_note}  "
        f"tts={TTS_PROVIDER}"
        + (f"({DOUBAO_VOICE})" if TTS_PROVIDER == "doubao" else f"({HERMES_VOICE})")
        + (f" wake_pending_tts=on(async={HERMES_ASYNC_CURSOR_TIMEOUT_S}s)" if HERMES_WAKE_PENDING_TTS else "")
    )
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
