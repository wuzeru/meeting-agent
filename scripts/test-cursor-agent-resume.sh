#!/usr/bin/env bash
# Cursor agent：测试 --resume 与虚构 session id 的行为（需本机已解锁钥匙串、agent 可用）
# 用法：
#   cd /path/to/vexa-lite
#   ./scripts/test-cursor-agent-resume.sh
#   ENV_FILE=/else/.env ./scripts/test-cursor-agent-resume.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing $ENV_FILE" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

AGENT="${HERMES_BIN:?HERMES_BIN missing in .env}"
FAKE="${FAKE_SESSION_ID:-00000000-0000-4000-8000-000000000001}"

COMMON_ARGS=(
  --output-format text
  --force
  --trust
  --approve-mcps
)

run_agent() {
  local label="$1"
  shift
  local ec=0
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "▶ $label"
  echo "   $*"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  "$@" 2>&1 || ec=$?
  echo "   exit=$ec"
}

echo "ROOT=$ROOT"
echo "AGENT=$AGENT"
echo "FAKE_SESSION_ID=$FAKE"
echo ""

if [[ ! -x "$AGENT" ]] && ! command -v "$AGENT" &>/dev/null; then
  echo "cannot run AGENT: $AGENT" >&2
  exit 1
fi

echo "=== A. 无 --resume（基线）==="
run_agent "no resume" "$AGENT" -p "只回复一个词：ping" "${COMMON_ARGS[@]}"

echo ""
echo "=== B. 虚构 UUID --resume（单次）==="
run_agent "fake resume once" "$AGENT" --resume "$FAKE" -p "只回复一个词：ping" "${COMMON_ARGS[@]}"

echo ""
echo "=== C. 同一虚构 UUID --resume（连续两次，观察是否「记住上一句」）==="
run_agent "fake resume #1" "$AGENT" --resume "$FAKE" -p "请记住代号：ALPHA-9。只回复 ok。" "${COMMON_ARGS[@]}"
run_agent "fake resume #2 (问代号)" "$AGENT" --resume "$FAKE" -p "刚才让你记的代号是什么？只回答代号本身。" "${COMMON_ARGS[@]}"

echo ""
echo "=== D. 两次均不带 --resume（对照：一般不会跨进程续上下文）==="
run_agent "fresh #1" "$AGENT" -p "请记住代号：BRAVO-7。只回复 ok。" "${COMMON_ARGS[@]}"
run_agent "fresh #2" "$AGENT" -p "刚才让你记的代号是什么？" "${COMMON_ARGS[@]}"

echo ""
echo "完成。请人工判断："
echo "  • C 的第二次是否说出 ALPHA-9：若否，多半虚构 resume 并未建立可续会话。"
echo "  • D 的第二次通常说不出 BRAVO-7（每次新进程），与 C 对比。"
echo ""
echo "可选：若某次输出里有 session_id，可把 REAL_SESSION_ID 写入脚本另跑一组对比（手动）。"