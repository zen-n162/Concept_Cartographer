#!/usr/bin/env bash
# /healthz + tools/list 疎通テスト (メモ §7.1, §10-12)
# ローカル: ./scripts/smoke_test.sh
# ACA:      MCP_BASE=https://<internal-fqdn> ./scripts/smoke_test.sh
set -euo pipefail

BASE="${MCP_BASE:-http://127.0.0.1:8000}"
OUT_DIR="$(cd "$(dirname "$0")/.." && pwd)/schemas"
TOOLS_JSON="$(mktemp)"
trap 'rm -f "$TOOLS_JSON"' EXIT

echo "== 1) healthz =="
curl -fsS "$BASE/healthz" && echo " OK"

echo "== 2) tools/list =="
# inspector v2 CLI: target は位置引数, streamable HTTP は --transport http
npx -y @modelcontextprotocol/inspector@latest \
  --cli "$BASE/mcp" \
  --transport http \
  --method tools/list \
  > "$TOOLS_JSON"

COUNT=$(jq '.tools | length' "$TOOLS_JSON")
echo "tools: $COUNT"
test "$COUNT" -ge 20 || { echo "expected >=20 tools"; exit 1; }

echo "== 3) create_element inputSchema snapshot -> schemas/excalidraw_tools_snapshot.json =="
jq '{captured_at_note: "regenerate via scripts/smoke_test.sh", tools: [.tools[] | select(.name as $n | ["create_element","update_element","delete_element","query_elements","describe_scene","export_scene","export_to_image","clear_canvas","batch_create_elements"] | index($n)) | {name, inputSchema}]}' \
  "$TOOLS_JSON" > "$OUT_DIR/excalidraw_tools_snapshot.json"
echo "snapshot written"

echo "SMOKE TEST PASSED"
