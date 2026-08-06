#!/usr/bin/env bash
# Excalidraw Canvas Server (REST + WebSocket) — 127.0.0.1:3000 (メモ §5.1)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)/my-mcp-server/mcp_excalidraw"
cd "$REPO_DIR"

test -f dist/server.js || { echo "dist/server.js missing — run: npm ci && npm run build"; exit 1; }

exec env HOST="${HOST:-127.0.0.1}" PORT="${PORT:-3000}" node dist/server.js
