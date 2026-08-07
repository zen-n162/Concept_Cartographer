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

# 古いサーバが残っていると bind に失敗し、**修正したはずのコードが動かない**まま
# 前のプロセスが応答し続ける【実測 2026-08-07: 3 時間前に起動したサーバが旧仕様で
# 生成し、修正が効いていないように見えた】。掴んでいるのが自分のアプリなら置き換える。
# -sTCP:LISTEN が要点 — lsof は接続中のクライアント (ブラウザ) も返すため、
# リスナーに限定しないと Chrome を「別アプリ」と誤認して起動を拒否する【実測】
STALE_PIDS="$(lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null || true)"
if [ -n "$STALE_PIDS" ]; then
  for pid in $STALE_PIDS; do
    if ps -p "$pid" -o command= 2>/dev/null | grep -q "cc_web.app"; then
      started="$(ps -p "$pid" -o lstart= 2>/dev/null | sed 's/^ *//')"
      echo "既存の Web サーバを停止します (pid $pid / 起動 $started)"
      kill "$pid" 2>/dev/null || true
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.3
      done
      kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    else
      echo "エラー: ポート ${PORT} を別のアプリ (pid $pid) が使用中です。" >&2
      echo "  CC_WEB_PORT=8091 ./scripts/start_web.sh のように別ポートで起動してください。" >&2
      exit 1
    fi
  done
  sleep 0.5
fi

# 起動したコードの版数を出す (古いサーバ問題を目視で気づけるように)
REV="$(git -C "$PROD_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
DIRTY=""
git -C "$PROD_DIR" diff --quiet 2>/dev/null || DIRTY=" +未コミットの変更あり"
echo "Concept Cartographer: http://127.0.0.1:${PORT}  [code ${REV}${DIRTY}]"
exec ./.venv/bin/uvicorn cc_web.app:app --host 127.0.0.1 --port "$PORT"
