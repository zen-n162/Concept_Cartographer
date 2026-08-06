# Concept Cartographer

研究者の思考地形を Novak 流概念地図として描き出す QST 社内 AI エージェント。
散らばった研究資料（OneDrive / SharePoint / Teams / 論文 PDF）から、概念・関係・
「まだ分かっていない場所（ギャップ候補）」を1枚の手描き風地図にする。

## リポジトリ構成

```
ConceptCartographer/
├── README.md                ← このファイル（リポジトリの案内図）
├── PRODUCTION_PLAN.md       ← 実運用（Production）版の計画書
├── poc/                     ← PoC 完成版（E2E 検証済み・保存）
│   ├── README.md            ← PoC の使い方・構成・実測結果
│   ├── src/ tests/ …        ← 実装一式（テスト32件 pass）
│   └── docs/                ← PoC 説明図・ポータル利用手順
├── 詳細設計レポート.pdf / _v2 / _v3   ← 設計文書（v1〜v3）
├── Concept_Cartographer_v4_核設計レポート.pdf
├── CC_v4_impl_spec (1).pdf           ← v4 実装仕様書
└── Excalidraw MCP引き継ぎメモ….md    ← PoC の一次基準だった引き継ぎメモ
```

- **PoC を動かす**: `poc/README.md` の手順どおり（`cd poc` してから実行）。
  タグ `poc-v1` が移設前の完成時点を指す。
- **実運用版**: `PRODUCTION_PLAN.md` に基づき、ルート直下に `production/` として
  段階的に構築予定（個人モード → チーム → 機構横断）。

## PoC で実証済みのこと（2026-08-05/06）

- チャット1行「今週の研究を概念地図として整理して」→ Work IQ が本人権限で
  OneDrive/SharePoint/Teams から資料収集 → Foundry の 4 エージェント
  （gpt-5.6 sol/luna/terra）→ Excalidraw 描画 → 独立検証 PASS まで全自動
- 中間契約 `layout_plan.json` による決定的レイアウト（座標は LLM に計算させない）
- ギャップ候補の非断定描画（破線 + 半透明）、日本語ラベルの重なり解消
- Foundry ポータル完結モード（`cc-cartographer-portal` が .excalidraw を添付で返す）
- 閉域 VM (VM-Excalidraw-MCP) への描画とローカルミラー

詳細と制約（権限・API 非互換などの実測記録）は `poc/README.md` と
`PRODUCTION_PLAN.md` の「リスク」「IT 部門への依頼」を参照。
