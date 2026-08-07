#!/usr/bin/env bash
# stdio MCP Server -> Streamable HTTP Gateway (メモ §6 準拠, SSE 版の置き換え)
#   Health: http://127.0.0.1:8000/healthz
#   MCP:    http://127.0.0.1:8000/mcp
# 初期検証では 127.0.0.1 のみに bind し、0.0.0.0 で公開しない (メモ §6)。
set -euo pipefail

# 稼働資産は poc ブランチ由来のローカル資産 (poc/my-mcp-server)。
# main から poc/ を外した後もこの実体は未追跡のまま残っている
REPO_DIR="${CC_MCP_DIR:-$(cd "$(dirname "$0")/../.." && pwd)/poc/my-mcp-server/mcp_excalidraw}"
CANVAS_URL="${EXPRESS_SERVER_URL:-http://127.0.0.1:3000}"
PORT="${GATEWAY_PORT:-8000}"

test -f "$REPO_DIR/dist/index.js" || { echo "dist/index.js missing — run: npm ci && npm run build"; exit 1; }

# --- npx の解決 (計画 A) ---------------------------------------------------
# 非対話シェルの PATH では /usr/local/bin/npx (npm 6.14 = Intel 時代の残骸) が
# 先に解決されることがある。この npx は `-y` を解釈できず、パッケージを取りに
# 行かずにヘルプを表示して終了する = **ゲートウェイが黙って起動しない**
# 【実測 2026-08-07: これで :8000 が一晩落ちたまま、描画が数時間ハングした】。
# Homebrew の新しい npx を優先し、無ければ npm のメジャー版を検査して、
# 起動できないなら理由を書いて止める (ヘルプを吐いて死ぬのが一番たちが悪い)。
NPX=""
if [ -x /opt/homebrew/bin/npx ]; then
  NPX=/opt/homebrew/bin/npx
else
  found="$(command -v npx || true)"
  if [ -z "$found" ]; then
    echo "エラー: npx が見つかりません。Node.js (npm 7 以上) を入れてください。" >&2
    exit 1
  fi
  # その npx と同じ場所の npm を見る (PATH 上の別の npm と混ぜない)
  npm_bin="$(dirname "$found")/npm"
  [ -x "$npm_bin" ] || npm_bin="$(command -v npm || true)"
  npm_ver=""
  [ -n "$npm_bin" ] && npm_ver="$("$npm_bin" --version 2>/dev/null || true)"
  npm_major="${npm_ver%%.*}"
  case "$npm_major" in
    ''|*[!0-9]*) npm_major=0 ;;
  esac
  if [ "$npm_major" -lt 7 ]; then
    echo "エラー: 使える npx が古すぎます ($found / npm ${npm_ver:-不明})。" >&2
    echo "  supergateway の起動には npm 7 以上が必要です (npx -y が要るため)。" >&2
    echo "  brew install node で新しい Node.js を入れるか、" >&2
    echo "  PATH の先頭に /opt/homebrew/bin を置いてください。" >&2
    exit 1
  fi
  NPX="$found"
fi

# 選んだ npx と同じ Node を PATH の先頭に置く。npx / supergateway / --stdio の
# `node` はすべて PATH から解決されるので、ここを揃えないと新しい npx が古い
# node で走って落ちる【実測: 非対話 PATH の node は v14、Homebrew は v22】。
PATH="$(dirname "$NPX"):$PATH"
export PATH
echo "gateway: npx=$NPX node=$(node --version 2>/dev/null || echo 不明)" \
     "port=$PORT canvas=$CANVAS_URL"

exec "$NPX" -y supergateway \
  --stdio "env ENABLE_CANVAS_SYNC=true EXPRESS_SERVER_URL=$CANVAS_URL node $REPO_DIR/dist/index.js" \
  --outputTransport streamableHttp \
  --port "$PORT" \
  --streamableHttpPath /mcp \
  --healthEndpoint /healthz \
  --logLevel "${GATEWAY_LOG_LEVEL:-info}"
