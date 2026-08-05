# Concept Cartographer PoC

`layout_plan.json` を中間契約として、知識グラフを Excalidraw MCP で Novak 流概念マップに描画するパイプライン。
Microsoft Foundry 上のマルチエージェント (5 構成: workflow + 4 agents) で運用する。

設計ソース: `Excalidraw MCP引き継ぎメモをダウンロード.md`（一次基準）、v4 核設計レポート / CC_v4_impl_spec（参考）。

## 構成

```
テキスト / knowledge_graph
  └─ cartographer-workflow (Foundry Workflows で順序固定)
       ├─ extraction-agent    テキスト → knowledge_graph JSON (LLM のみ)
       ├─ layout-agent        KG → layout_plan (座標は cc-tools compute_layout に委譲)
       ├─ projection-agent    layout_plan → 描画 (cc-tools render_layout_plan)
       └─ verification-agent  describe_scene / verify_scene で独立検証
                 │
                 ▼
   cc-tools MCP (Python/FastMCP) ──▶ Excalidraw MCP gateway (/mcp) ──▶ Canvas Server (:3000)
```

- **layout_plan 契約**: `schemas/layout_plan.schema.json`（メモ §9 準拠）。knowledge_graph を Excalidraw MCP に直接渡さない
- **決定性**: 座標計算 (`cc_core.layout`)・描画 (`cc_core.adapter`) は決定的コード。LLM は抽出とオーケストレーションのみ
- **glyph 変換**: arrow=因果(赤) / wave=相関(青点線) / zigzag=矛盾(橙) / double=補強(緑太線) / hole=ギャップ候補(灰・**破線+半透明**、確定事項として描画しない)
- **描画順**: island → node → edge。element ID は `isl-*` / `node-*` / `edge-*` で決定的に採番し、失敗時は逆順ロールバック
- **ログ**: ラベル本文を出さない（SHA-256 digest 先頭 8 桁のみ）

## ローカル実行 (M1)

```bash
# 0) 初回のみ
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
(cd my-mcp-server/mcp_excalidraw && npm ci && npm run build)

# 1) 2 ターミナルで起動
./scripts/start_canvas.sh     # 127.0.0.1:3000 (ブラウザで開くと目視確認できる)
./scripts/start_gateway.sh    # 127.0.0.1:8000/mcp + /healthz (streamableHttp)

# 2) 疎通 + ツールスキーマのスナップショット取得
./scripts/smoke_test.sh       # -> schemas/excalidraw_tools_snapshot.json

# 3) エージェントなしの基準線 (メモ §8 最小描画テスト)
./.venv/bin/python -m cc_core.render fixtures/layout_plan_min.json
./.venv/bin/python -m cc_core.render fixtures/layout_plan_gap.json   # ギャップ候補: 破線+半透明

# 4) テスト
./.venv/bin/pytest -m "not e2e"   # ユニット (サーバー不要)
./.venv/bin/pytest -m e2e         # E2E (canvas + gateway 起動時のみ)

# 5) cc-tools MCP サーバー (エージェントが呼ぶ層) の単体起動
CC_TOOLS_HOST=127.0.0.1 ./.venv/bin/python -m cc_tools.server   # 127.0.0.1:8080/mcp
```

Docker でローカル結合 (ACA と同じトポロジ):

```bash
docker compose -f infra/docker/compose.local.yml up --build
```

## ACA デプロイ (M2)

`infra/main.bicep` — ACA managed environment を **internal: true** (Public IP なし・インターネット非公開) で構築。

```bash
az acr build -r <acr> -t excalidraw-mcp:latest -f infra/docker/Dockerfile.excalidraw my-mcp-server/mcp_excalidraw
az acr build -r <acr> -t cc-tools:latest -f infra/docker/Dockerfile.cctools .
az deployment group create -g prj-qst-ai -f infra/main.bicep \
  -p infraSubnetId=<ACA用サブネットID> -p acrName=<acr>
```

**管理者調整が必要な前提**（メモ §11 のとおり未確定）:
- ACA 用サブネット（`Microsoft.App/environments` 委任）の払い出し
- Foundry (Standard Agent Setup / Private Networking) からこの VNet への経路 + Private DNS ゾーンリンク
- 疎通確認は VM-Excalidraw-MCP (Bastion 経由) から: `MCP_BASE=https://excalidraw-mcp.internal.<env-domain> ./scripts/smoke_test.sh`

## Foundry セットアップ (M3)

1. VS Code の **Foundry Toolkit** でサインイン → `prj-qst-ai` の Foundry プロジェクトに接続
2. Project Connection を 2 つ登録: `excalidraw-mcp` (`https://excalidraw-mcp.internal.<env>/mcp`)、`cc-tools` (`https://cc-tools.internal.<env>/mcp`)
3. `agents/*.yaml` の 4 エージェントを Agent Designer で作成・デプロイ（`model.id` はプロジェクトのデプロイ名に合わせる）
4. `workflows/cartographer_workflow.yaml` をワークフローデザイナに合わせて登録（順序・再試行 1 回・分岐は本ファイルが正）
5. Playground から実行例: 「NV中心の発光は温度プローブとして機能する。酸化還元状態との関連は未検証である。」→ verification-agent の verdict が PASS、canvas UI で目視確認

Foundry Workflows が利用できない場合のフォールバック: 同じ 4 エージェント定義を Microsoft Agent Framework (Python) の Workflow としてコード定義し ACA にホストする（`workflows/cartographer_workflow.yaml` の遷移をそのまま移植）。

## 完了条件チェックリスト（メモ §12）

- [ ] Canvas / stdio MCP / Streamable HTTP `/mcp` が応答
- [ ] `tools/list` でツール一覧取得（smoke_test.sh）
- [ ] 2 ノード + 1 エッジ作成（`cc_core.render` + fixtures/layout_plan_min.json）
- [ ] `describe_scene` で読み戻し検証（verify_scene）
- [ ] `layout_plan.json` から描画
- [ ] ギャップ候補が破線・半透明で描画（fixtures/layout_plan_gap.json + test_e2e_local.py）
- [ ] `.excalidraw` と SVG 出力（PNG はブラウザ接続時のみ）
- [ ] Foundry Playground からワークフロー起動 → 最小描画テスト成功
- [ ] 秘密鍵・トークンがリポジトリに含まれない（`.gitignore` + コミット前確認）

## セキュリティ規約（メモ §4）

- VM / ACA に Public IP を付与しない。SSH 22 / Canvas 3000 / MCP 8000 / Inspector 6274-6277 をインターネット公開しない
- `.env` / `*.pem` / 秘密鍵 / Inspector トークンは `.gitignore` 済み。コミット前に確認する
- ログへ論文本文・Teams 本文・個人情報を記録しない（`cc_core.logging_util` の digest 方式を全層で使用）
- 研究者データ投入前にダミーデータで E2E を通す
