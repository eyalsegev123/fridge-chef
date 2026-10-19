#!/usr/bin/env bash
# Start the whole Fridge Chef stack with one command:
#   1. MCP server on port 8080
#   2. chat backend on port 8000 (it discovers the MCP tools on startup)
#   3. ngrok tunnel to port 8000
#   4. register the new ngrok URL as the Telegram webhook
# Logs from both services stream into this window. Ctrl+C stops everything.
#
# Usage: ./start.sh            full stack with Telegram
#        ./start.sh --local    skip ngrok and Telegram (test with /chat only)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$ROOT/logs"
LOCAL_ONLY=false
[[ "${1:-}" == "--local" ]] && LOCAL_ONLY=true

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

for env_file in "$ROOT/mcp-server/.env" "$ROOT/chat-backend/.env"; do
  if [[ ! -f "$env_file" ]]; then
    echo "Missing $env_file. Copy it from .env.example and fill it in first." >&2
    exit 1
  fi
done
if ! $LOCAL_ONLY && ! command -v ngrok >/dev/null; then
  echo "ngrok is not installed. Run: brew install ngrok (or use --local)." >&2
  exit 1
fi

for port in 8080 8000; do
  if lsof -ti "tcp:$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $port is already in use. Stop the old server first." >&2
    exit 1
  fi
done

mkdir -p "$LOG_DIR"
PIDS=()
cleanup() {
  echo
  echo "Stopping Fridge Chef..."
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT
trap "exit 130" INT TERM

wait_for_health() {
  local name="$1" url="$2"
  for _ in $(seq 1 30); do
    if curl -sf "$url" >/dev/null 2>&1; then
      echo "  $name is healthy"
      return 0
    fi
    sleep 1
  done
  echo "$name did not start. Last log lines:" >&2
  tail -n 20 "$LOG_DIR/$name.log" >&2
  exit 1
}

echo "1/4 Starting MCP server on :8080"
(
  cd "$ROOT/mcp-server"
  set -a; source .env; set +a
  exec "$PYTHON" http_server.py
) >"$LOG_DIR/mcp-server.log" 2>&1 &
PIDS+=($!)
wait_for_health mcp-server http://127.0.0.1:8080/health

echo "2/4 Starting chat backend on :8000"
(
  cd "$ROOT/chat-backend"
  exec "$PYTHON" main.py
) >"$LOG_DIR/chat-backend.log" 2>&1 &
PIDS+=($!)
wait_for_health chat-backend http://127.0.0.1:8000/health

if $LOCAL_ONLY; then
  echo "Local mode: skipping ngrok and Telegram."
else
  echo "3/4 Opening ngrok tunnel"
  ngrok http 8000 --log=stdout >"$LOG_DIR/ngrok.log" 2>&1 &
  PIDS+=($!)
  PUBLIC_URL=""
  for _ in $(seq 1 20); do
    PUBLIC_URL="$(curl -s http://127.0.0.1:4040/api/tunnels 2>/dev/null | "$PYTHON" -c '
import json, sys
try:
    tunnels = json.load(sys.stdin)["tunnels"]
except Exception:
    sys.exit(0)
print(next((t["public_url"] for t in tunnels if t["public_url"].startswith("https://")), ""))
' || true)"
    [[ -n "$PUBLIC_URL" ]] && break
    sleep 1
  done
  if [[ -z "$PUBLIC_URL" ]]; then
    echo "ngrok did not report a public URL. See $LOG_DIR/ngrok.log" >&2
    exit 1
  fi
  echo "  Public URL: $PUBLIC_URL"

  echo "4/4 Registering Telegram webhook"
  # Load the bot token and secret only into this subshell, and never echo them.
  RESULT="$(
    set -a; source "$ROOT/chat-backend/.env"; set +a
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
      --data-urlencode "url=${PUBLIC_URL}/telegram/webhook" \
      --data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
      --data-urlencode "drop_pending_updates=true"
  )"
  if [[ "$RESULT" != *'"ok":true'* ]]; then
    echo "Telegram rejected the webhook: $RESULT" >&2
    exit 1
  fi
  echo "  Telegram webhook set. Message your bot now."
  echo "  Request inspector: http://127.0.0.1:4040"
fi

echo
echo "Fridge Chef is running. Streaming logs (Ctrl+C to stop everything)..."
echo
tail -n 0 -f "$LOG_DIR/mcp-server.log" "$LOG_DIR/chat-backend.log"
