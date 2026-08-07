# Concept Cartographer — 開発ガイド

このファイルは目次です。詳細は各ドキュメントを参照してください。

## 役割分担 (恒久ルール)

- **複雑な詳細設計 = Claude Fable 5**: 機能追加の前に `production/docs/<機能>-design.md` を書く
  (データモデル・API・エッジケース・テスト計画・受け入れ基準)
- **主な実装 = Claude Opus 5**: 設計書に基づく実装を担当。逸脱は報告し、Fable 5 が検収してからコミット
- **軽い追加実装 = Claude Sonnet 5**: 設計が固まっている小さめの機能
- 機能変更は **CLI (`cc_orchestrator.chat`) と Web (`cc_web`) の両方**に反映する。
  ロジックは `cc_core/` に置き、CLI フラグと Web API はどちらも薄いラッパにする

## ドキュメント

| ファイル | 内容 |
|---|---|
| `PRODUCTION_PLAN.md` | 実運用計画書 (15 章 + 裁定 10 件、R0–R3 のリリース計画、IT 依頼) |
| `production/README.md` | 実運用版の使い方・仕組み・検証状況 |
| `production/docs/webapp-design.md` | Web アプリの詳細設計 (API・画面・デザイントークン) |
| `production/docs/edit-feedback-design.md` | 編集とフィードバック学習の詳細設計 |
| `production/docs/excalidraw-export-design.md` | 「Excalidraw で開く」の設計 |
| `設計文書/` | 元の設計 PDF と引き継ぎメモ |
| `poc/` | 凍結保存した PoC (タグ `poc-v1`)。原則触らない |

## 環境の要点 (実測済みのハマりどころ)

- M1 Mac だがシェルが Rosetta x86_64 のことがある。venv は必ず
  `/opt/homebrew/bin/python3.11` (arm64) で作る。`mcp>=1.9,<2` 固定
  (2.0 の cryptography wheel が壊れている)。azure-identity は使えず az CLI トークンを使う
- Foundry は新 `/agents` API (kind:prompt + Responses API) のみ。旧 `/assistants` は
  gpt-5.6 系と非互換でポータルにも出ない。エージェントは名前固定でバージョンを積む
- VM への run-command 中継は全廃。描画は `--target local|file` の 2 経路
- zsh で変数名 `GID` は使わない (特殊変数で setgid エラーになる)

## 定型コマンド

```bash
cd production
./.venv/bin/pytest -m "not e2e" -q                       # テスト (Foundry 不要)
./scripts/start_web.sh                                    # Web http://127.0.0.1:8090
./.venv/bin/python -m cc_orchestrator.chat "今週の研究を概念地図として整理して"
```
