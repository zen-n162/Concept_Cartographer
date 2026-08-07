# Concept Cartographer

研究者の思考地形を Novak 流概念地図として描き出す QST 社内 AI エージェント。
散らばった研究資料 (OneDrive / SharePoint / Teams / 論文 PDF) から、概念・関係・
「まだ分かっていない場所 (ギャップ候補)」を 1 枚の手描き風地図にする。

## ブランチ構成

| ブランチ | 内容 |
|---|---|
| **main** | **完成版** — 実運用版 (production/) + Web アプリ。ここが開発の本線 |
| **poc** | PoC (概念実証) の保存・改良ライン。タグ `poc-v1` = PoC 完成時点 |

```bash
git switch poc                                   # PoC に切り替え
git worktree add ../ConceptCartographer-poc poc  # main と並べて置く場合 (推奨)
```

---

## 1. PoC の実装方法 (poc ブランチ)

**目的**: 「チャット 1 行 → 資料収集 → 概念抽出 → Excalidraw 描画 → 独立検証」が
成立することの実証。実測 2.5〜3.5 分で E2E 完走。

**構成**:
- **Foundry 4 エージェント** (新 /agents API・kind:prompt): extraction (資料読取と
  概念抽出) / layout (配置) / projection (描画) / verification (独立検証)。
  抽出と検証は別モデルにして自己採点を避ける
- **layout_plan.json 中間契約**: LLM の出力を直接 Excalidraw に渡さず、
  JSON Schema + 意味検証を通った計画だけを描画する
- **描画**: ローカル Excalidraw canvas (127.0.0.1:3000) + MCP ゲートウェイ (8000)
- **M365 読取**: Work IQ Remote MCP (本人権限)

**動かす**: poc ブランチの `poc/README.md` の手順どおり (`cd poc` して実行)。
テスト 32 件。

---

## 2. 完全版の実装方法 (main / production/)

**目的**: PoC を個人モードの実運用サービスへ昇格。R1 (基盤) → R1.5 (編集と学習)
→ R2a (知識モデル多層化) → R2b (検索・QA) → R2c (評価・推薦) まで実装済み。

**パイプライン** (⓪〜⑧。LLM は判断が要る所だけ、構造化・レイアウトは決定的コード):

```
⓪ Query Routing   basic / vector / map / local / global / hybrid の 6 経路
① Ingest          Work IQ (本人権限) + ローカル資料。要約はスコーピングのみ
② Zone            文単位の議論的役割ラベル (AZ / CoreSC)
③ Claims          Result/Conclusion 文 → 簡易 Nanopub 形式の主張
④ Relate          因果 3 点セット (候補 → 語彙証拠 → 独立 LLM 検証)
⑤ Validate        3 検証器合成 (LLM-NLI + 独立 LLM + 規則オントロジー) → rejection_log
⑥ Rhetoric        Toulmin CGW + 内部矛盾 (refutes) 検出 → ⚡ の表示条件
⑦ Meta            provenance / polarity 充填 + 層タグ → 8 記号への決定的投影
⑧ Project         可変詳細度 3 段を同梱した layout_plan → 描画 + 独立検証
```

**主要機能**:
- **可変詳細度**: Overview 10-20 / Standard 20-50 / Detailed 50-100 要素。
  3 レベルを 1 回で生成して同梱 — 切替は LLM ゼロ・1ms
- **8 記号 / 内部 30 種**: 画面は → 〜 ⚡ ⇒ ◇ ◧ ▷ ? の 8 記号、内部は 4 層 30 種の
  関係語彙を保持 (クリック展開で全タグ・検証スコア・主張本文)
- **編集と学習**: 原本不変 + 追記ログで 8 操作の編集 (取り消し可)。修正から
  用語辞書・除外リスト・因果上書きを学習し、適用内容は必ず表示
- **ギャップ 3 型** (構造 / 言説 / 因果) + confirm/dismiss + 型別 Gap Report
- **セッション横断 QA**: 「X と Y の関係は?」(local) 「全体像をまとめて」(global) に
  出典つきで回答。索引は SQLite 派生キャッシュ (自動再構築)
- **評価**: クリック評価 + gold ファイルによるオフライン KPI (正答率・因果精度)
- **コスト制御**: テストモード (同一依頼の再実行を LLM ゼロで再利用) +
  実行毎のトークン表示 + `--token-report`

**動かす**:

```bash
cd production
python3.11 -m venv .venv                       # M1 Mac は /opt/homebrew/bin/python3.11
./.venv/bin/pip install -r requirements.txt    # 依存一式 (本体+テスト+Web)
./.venv/bin/python -m cc_orchestrator.chat "今週の研究を概念地図として整理して"
./.venv/bin/pytest -m "not e2e"     # 650+ 件
```

詳細 (全 CLI フラグ・環境変数・KPI・実測値) は [production/README.md](production/README.md)。

---

## 3. Web アプリの実装方法 (main / production/src/cc_web/)

**構成**: FastAPI (127.0.0.1:8090 固定) + 素の HTML/CSS/JS (ビルド工程なし・CDN なし
= 閉域前提)。CLI と同じパイプラインを呼ぶ薄いラッパで、機能は常に CLI と両面実装。

```bash
cd production
./scripts/start_web.sh      # http://127.0.0.1:8090
```

**画面**: チャット入力 (テンプレート 4 枚) → 進捗カード (13 ステージ) → 結果カード
(検証バッジ・多層分析チップ・地図 / ギャップ / 評価タブ)。地図はクリックで
根拠の逐語引用・機械タグ・検証スコアを展開し、その場で編集 (8 操作) と評価ができる。
ヘッダーで詳細度を即切替。設定でテストモード・多層分析・学習の適用を制御。

**API**: ジョブ投入/進捗、セッション別 SVG・突合 JSON・.excalidraw、編集と取り消し、
ギャップ確定、Gap Report、多層分析の記録、オフライン評価、学習内容の要約など
約 20 エンドポイント (一覧は production/README.md)。

---

## 開発ルール (要点)

- **設計と実装の分担**: 複雑な詳細設計 → 実装 → 検収の 2 段体制。機能は必ず
  CLI と Web の両面に配線し、ロジックは cc_core/ に置く
- **LLM の出力を信用しない**: 受け取り側で正規化・修復し、直した内容は必ず報告
- **決定性**: レイアウト・詳細度・投影・編集の再構成は同じ入力から常に同じ出力
- **公開しないもの**: 設計 PDF・実データの知識グラフ・計画書等は .gitignore で
  機械的に除外 (履歴からも除去済み)
