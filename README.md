# Concept Cartographer

研究者の思考地形を Novak 流概念地図として描き出す QST 社内 AI エージェント。
散らばった研究資料（OneDrive / SharePoint / Teams / 論文 PDF）から、概念・関係・
「まだ分かっていない場所（ギャップ候補）」を1枚の手描き風地図にする。

## リポジトリ構成

```
ConceptCartographer/
├── README.md                ← このファイル（リポジトリの案内図）
├── PRODUCTION_PLAN.md       ← 実運用（Production）版の計画書
├── poc/                     ← PoC 完成版（保存・変更凍結／タグ poc-v1）
│   ├── README.md            ← PoC の使い方・構成・実測結果
│   └── src/ tests/ docs/    ← 実装一式（テスト32件 pass）
├── production/              ← 実運用版 R1（開発中）
│   ├── README.md            ← 使い方・PoC との違い・実測値
│   └── src/ tests/          ← 可変詳細度ほか R1 実装（テスト99件 pass）
└── 設計文書/                 ← 設計 PDF 5 点 + 引き継ぎメモ（一次基準資料一式）
    ├── Excalidraw MCP引き継ぎメモ….md
    ├── 詳細設計レポート.pdf / _v2 / _v3
    ├── Concept_Cartographer_v4_核設計レポート.pdf
    └── CC_v4_impl_spec (1).pdf
```

- **PoC を動かす**: `poc/README.md` の手順どおり（`cd poc` してから実行）。
  タグ `poc-v1` が移設前の完成時点を指す。
- **実運用版を動かす**: `production/README.md` の手順どおり（`cd production`）。
  R1（個人モード）の中核機能は実装済み — 可変詳細度 3 段・因果の 3 点セット検証・
  ギャップ確定・Query Routing・評価収集・ヘッドレス SVG。
  R2 以降（多層知識モデル・グラフ DB・チーム/横断）は `PRODUCTION_PLAN.md` 参照。

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
