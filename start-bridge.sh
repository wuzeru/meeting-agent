#!/bin/zsh
# Vexa <-> Hermes bridge launcher.
# Reads ~/vexa-lite/.env, exports everything, then starts bridge.py in background.
# Logs go to ~/vexa-lite/logs/bridge.log.

set -e
cd "$(dirname "$0")"

ENV_FILE="${ENV_FILE:-$PWD/.env}"
LOG_DIR="$PWD/logs"
LOG_FILE="$LOG_DIR/bridge.log"
PID_FILE="$PWD/.bridge.pid"

mkdir -p "$LOG_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing $ENV_FILE" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${VEXA_API_KEY:?need VEXA_API_KEY in .env}"

if [[ -f "$PID_FILE" ]]; then
  oldpid=$(cat "$PID_FILE" 2>/dev/null || echo)
  if [[ -n "$oldpid" ]] && kill -0 "$oldpid" 2>/dev/null; then
    echo "stopping previous bridge pid=$oldpid"
    kill "$oldpid" || true
    sleep 1
    kill -9 "$oldpid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi

# Belt and braces — kill any other stray bridge.py processes.
pkill -f "bridge.py" 2>/dev/null || true
sleep 0.5

echo "[start-bridge] starting bridge (tts=${TTS_PROVIDER:-piper}, MEETING_ID=${MEETING_ID:-'(optional — use panel)')})"
nohup python3 "$PWD/bridge.py" >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
sleep 1
echo "[start-bridge] pid=$(cat $PID_FILE) log=$LOG_FILE"
tail -n 5 "$LOG_FILE" || true
