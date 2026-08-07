#!/usr/bin/env bash
# Excalidraw Canvas Server (REST + WebSocket) — 127.0.0.1:3000 (メモ §5.1)
set -euo pipefail

# 稼働資産は poc ブランチ由来のローカル資産 (poc/my-mcp-server)。
# main から poc/ を外した後もこの実体は未追跡のまま残っている
REPO_DIR="${CC_MCP_DIR:-$(cd "$(dirname "$0")/../.." && pwd)/poc/my-mcp-server/mcp_excalidraw}"
cd "$REPO_DIR"

test -f dist/server.js || { echo "dist/server.js missing — run: npm ci && npm run build"; exit 1; }

exec env HOST="${HOST:-127.0.0.1}" PORT="${PORT:-3000}" node dist/server.js
