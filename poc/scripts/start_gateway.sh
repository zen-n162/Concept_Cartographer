#!/usr/bin/env bash
# stdio MCP Server -> Streamable HTTP Gateway (メモ §6 準拠, SSE 版の置き換え)
#   Health: http://127.0.0.1:8000/healthz
#   MCP:    http://127.0.0.1:8000/mcp
# 初期検証では 127.0.0.1 のみに bind し、0.0.0.0 で公開しない (メモ §6)。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)/my-mcp-server/mcp_excalidraw"
CANVAS_URL="${EXPRESS_SERVER_URL:-http://127.0.0.1:3000}"
PORT="${GATEWAY_PORT:-8000}"

test -f "$REPO_DIR/dist/index.js" || { echo "dist/index.js missing — run: npm ci && npm run build"; exit 1; }

exec npx -y supergateway \
  --stdio "env ENABLE_CANVAS_SYNC=true EXPRESS_SERVER_URL=$CANVAS_URL node $REPO_DIR/dist/index.js" \
  --outputTransport streamableHttp \
  --port "$PORT" \
  --streamableHttpPath /mcp \
  --healthEndpoint /healthz \
  --logLevel "${GATEWAY_LOG_LEVEL:-info}"
