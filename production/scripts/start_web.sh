#!/usr/bin/env bash
# Concept Cartographer Web アプリ — 127.0.0.1:8090 (設計書 §8)
#   UI:     http://127.0.0.1:8090
#   Health: http://127.0.0.1:8090/healthz
# 0.0.0.0 では bind しない (引き継ぎメモ §4)。閉域・単一ユーザー前提。
set -euo pipefail

PROD_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROD_DIR"

PORT="${CC_WEB_PORT:-8090}"
CANVAS_URL="${CC_CANVAS_URL:-http://127.0.0.1:3000}"
GATEWAY_HEALTH="${CC_GATEWAY_HEALTH:-http://127.0.0.1:8000/healthz}"

test -x ./.venv/bin/uvicorn || {
  echo "uvicorn missing — run: ./.venv/bin/pip install -e \".[dev,web]\"" >&2
  exit 1
}

# 描画先 (Excalidraw MCP) は必須ではない。無ければ警告だけ出して起動する。
# target=file なら MCP なしでも .excalidraw / SVG は生成できる (計画 §3-2)。
warn=0
curl -sf -m 2 -o /dev/null "$CANVAS_URL" || { echo "警告: canvas が応答しません ($CANVAS_URL)"; warn=1; }
curl -sf -m 2 -o /dev/null "$GATEWAY_HEALTH" || { echo "警告: MCP gateway が応答しません ($GATEWAY_HEALTH)"; warn=1; }
if [ "$warn" = "1" ]; then
  echo "  → ライブキャンバスへの描画は使えません。target=file (MCP 不要) は動きます。"
  echo "  → 使う場合は scripts/start_canvas.sh と scripts/start_gateway.sh を先に起動してください。"
fi

echo "Concept Cartographer: http://127.0.0.1:${PORT}"
exec ./.venv/bin/uvicorn cc_web.app:app --host 127.0.0.1 --port "$PORT"
