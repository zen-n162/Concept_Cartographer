# Concept Cartographer 実運用版 (R1)

`PRODUCTION_PLAN.md` の R1 スコープ（個人モード）の実装。
PoC (`../poc/`, タグ `poc-v1`) の実証済み資産を土台に、実運用に必要な
**可変詳細度・関係の検証・ギャップ確定・Query Routing・評価収集**を追加した。

## PoC からの主な違い

| 項目 | PoC | 実運用版 R1 |
|---|---|---|
| 詳細度 | 単一 | **Overview / Standard / Detailed の 3 段（ノード数駆動）** |
| 因果矢印 | LLM 判定のまま描画 | **3 点セット通過時のみ**（候補→語彙証拠→独立検証）。不通過は相関へ降格 |
| 矛盾 | ⚡ で断定表示 | **非断定の「対立候補」へ降格**（L8 が無い R1 では断定しない） |
| ギャップ | 表示のみ | **4 点メタデータ + confirm / dismiss**（有用率 KPI の分母） |
| 入口 | 常にフル生成 | **Query Routing 3 経路**（basic / vector / map） |
| 評価 | なし | **オンライン評価収集 + KPI 集計** |
| 描画先 | VM (run-command 中継) | **MCP か直接ファイル生成**（run-command は全廃） |
| 出力 | .excalidraw のみ | **+ ヘッドレス SVG**（ブラウザ不要） |

## 使い方

```bash
cd production
python3.11 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"

# 地図生成（詳細度は依頼文から自動判定。既定 standard）
./.venv/bin/python -m cc_orchestrator.chat "今週の研究を概念地図として整理して"
./.venv/bin/python -m cc_orchestrator.chat "今月の研究をざっくり全体像で"      # → overview
./.venv/bin/python -m cc_orchestrator.chat "直近30日を詳しく" --level detailed

# 詳細度の切替（LLM 呼び出しゼロ・再レイアウトなし。実測 1ms 以下）
./.venv/bin/python -m cc_orchestrator.chat --switch graphs/layout_plan_session_X.json --level overview

# 集約ノードの展開（ドリルダウン）
./.venv/bin/python -m cc_orchestrator.chat --expand agg-comm_001 --plan graphs/layout_plan_session_X.json

# ギャップ候補の確認と確定
./.venv/bin/python -m cc_orchestrator.chat --gap-list --plan <plan.json>
./.venv/bin/python -m cc_orchestrator.chat --gap-confirm gap-isolated-c003 --plan <plan.json>

# テスト
./.venv/bin/pytest -m "not e2e"      # 99 件
```

描画先は `--target local`（Excalidraw MCP）が既定。ACA 到達前は
`--target file` で MCP なしに `.excalidraw` / SVG を生成でき、この経路でも
可変詳細度と独立検証が成立する（計画 §3-2 の fallback 要件）。

## 可変詳細度の仕組み（v3 §2.4 / 計画 §4）

```
knowledge_graph
  └ community.py   Leiden でコミュニティ検出
                   重要度 = 媒介中心性 0.4 + 出現頻度 0.3 + 新規性 0.3
                   各コミュニティから最低1つ確保しつつ Top-K 選抜
                   非表示分はコミュニティ単位で集約ノードへ畳む
  └ detail.py      3 レベル分を1回で生成し単一 plan に同梱
                   → project(plan, level) は取り出すだけ（再計算なし）
```

**表示枠は「概念 + 集約」の合計**で帯を判定する（認知負荷は画面上の要素総数で
決まるため）。コミュニティ数が枠を超える大規模グラフでは、下位コミュニティを
「その他」集約へ併合して上限を守る。

実測（合成データ・実データ）:

| 入力 | Overview | Standard | Detailed | 生成時間 |
|---|---|---|---|---|
| 19 概念（実データ） | 13 (集約3) | 19 | 19 | 0.03 秒 |
| 100 概念 | 20 (集約10) | 50 (集約10) | 100 | 0.02 秒 |
| 200 概念 | 20 (集約13) | 50 (集約13) | 100 (集約13) | 0.05 秒 |
| 400 概念 | 20 (集約20) | 50 (集約22) | 100 (集約22) | 0.18 秒 |

全レベルで帯を遵守し、未解決のラベル重なりはゼロ。切替は 1ms 以下。

## モジュール構成

```
src/
├ cc_core/
│  ├ community.py    ★ Leiden・重要度・Top-K 選抜・集約・ドリルダウン対応表
│  ├ detail.py       ★ 3レベル同梱 plan の生成 / project() / 帯検査
│  ├ causal.py       ★ causal cue lexicon・3点セット検証・矛盾の非断定化
│  ├ gaps.py         ★ ギャップ検出（4点メタデータ）・confirm/dismiss・有用率
│  ├ evaluation.py   ★ オンライン評価（v3 §7.2.1 の2系統ラベル）・KPI 集計
│  ├ svg_export.py   ★ ヘッドレス SVG（ブラウザ不要）
│  ├ layout.py       日本語幅推定グリッド（上位層の属性を保持するよう改修）
│  ├ adapter.py verify.py overlap.py textmetrics.py validate.py
│  ├ excalidraw_file.py mcp_client.py render.py
├ cc_orchestrator/
│  ├ routing.py      ★ Query Routing 3経路・詳細度/言語/タグ解釈
│  ├ pipeline.py     ⓪Routing→①Ingest→③抽出→④関係検証→詳細度→ギャップ→⑧描画→検証
│  ├ chat.py         CLI（生成・切替・展開・ギャップ確定）
│  └ agents_def.py foundry_v2.py ingest.py tool_exec.py portal_agent.py
└ cc_tools/          FastMCP（ACA デプロイ単位）
```
★ = R1 の新規実装

## R1 で実装していないもの（計画どおり R2 以降）

- L2 ゾーニング / L3 Nanopublication / L5 BFO / L7 Toulmin / L8 CiTO
- 完全 3 段バリデーション（DeBERTa 系 NLI・オントロジー整合性）
- グラフ DB（PostgreSQL + AGE）・LazyGraphRAG・コーパス級 Leiden
- 8 記号への移行（L7/L8 成立が前提）
- Routing の local / global / hybrid / ontology-guided
- オフライン評価（SciNLI 等）と日本語正解セット
- チームモード・機構横断（permission_tags は R1 から保持済み）

## 堅牢性の設計

LLM は指示どおりの形を返すとは限らない。実際に `evidence_span` を配列ではなく
単一オブジェクトで返し、パイプラインが停止した（2026-08-07）。対策として
**受け取り側で必ず正規化する**層 (`cc_core/normalize.py`) を置いた:

- 形の揺れを吸収（単一オブジェクト/文字列 → 配列、camelCase キー、null オフセット）
- 契約違反を修復して先へ流す（参照切れ・自己ループ・重複 ID・未知 glyph）
- 何を直したかをログと実行サマリに残す（黙って直さない）
- 未知の glyph は **相関へ倒す**（因果へ倒すと過剰昇格になるため）

また Work IQ の `copilot_chat` は Foundry 側 HttpClient の 100 秒制限で
`TaskCanceledException` になることがあるため、一過性エラーは自動再試行する。

`char_start` / `char_end` はスキーマ上 **任意**。Work IQ は文字オフセットを
返さないため、その場合の trace back は document 粒度になる（`surface` の
逐語引用は必須なので、因果の語彙証拠検査は成立する）。

## 検証状況

- ユニット / 統合テスト **119 件 pass**（可変詳細度 21 件・R1 機能 44 件・正規化 20 件）
- 実キャンバスへの 3 レベル描画で **すべて検証 PASS**（overview 38 / standard 48 / detailed 48 要素）
- `--target file`（MCP なし fallback）でも描画 82 要素・オフライン検証 PASS
- スケール実測: 400 概念まで帯遵守・重なりゼロ・0.18 秒
- **Work IQ 経由の実運用フロー成功**（2026-08-07）: 資料 4 件 → 概念 16 / 関係 16 /
  島 5 → overview 12(集約2) / standard 16 / detailed 16 → 描画・検証 PASS →
  3 レベルの SVG 出力
