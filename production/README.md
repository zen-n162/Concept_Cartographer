# Concept Cartographer 実運用版 (R1 / R1.5 / R2a)

`PRODUCTION_PLAN.md` の R1 スコープ（個人モード）の実装に、
R1.5（編集とフィードバック学習）と R2a（知識モデルの多層化）を積んだもの。
PoC (`../poc/`, タグ `poc-v1`) の実証済み資産を土台に、実運用に必要な
**可変詳細度・関係の検証・ギャップ確定・Query Routing・評価収集**を追加した。

## PoC からの主な違い

| 項目 | PoC | 現在 |
|---|---|---|
| 詳細度 | 単一 | **Overview / Standard / Detailed の 3 段（ノード数駆動）** |
| 因果矢印 | LLM 判定のまま描画 | **3 点セット + 主張の検証を通ったときだけ**。不通過は相関へ降格 |
| 矛盾 | ⚡ で断定表示 | **論証段（別モデル）が矛盾と判定した対だけ ⚡**。それ以外は対立候補 |
| 記号 | 6 種 | **8 種**（因果・相関・補強・矛盾・分類・構成・時系列・疑問）＋内部専用 2 種 |
| 関係の意味 | glyph だけ | **4 層 30 種の関係語彙**を内部に保持し、画面には 8 記号へ畳む |
| ギャップ | 表示のみ | **3 型（構造 / 言説 / 因果）+ 4 点メタデータ + confirm / dismiss** |
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

# 概念図の編集 (原本は書き換えず edits_session_*.jsonl へ追記)
./.venv/bin/python -m cc_orchestrator.chat --plan <plan.json> \
  --edit '{"op":"rename_node","target":"n3","payload":{"label":"新しい名前"}}'
./.venv/bin/python -m cc_orchestrator.chat --plan <plan.json> --edit-file ops.json
./.venv/bin/python -m cc_orchestrator.chat --plan <plan.json> --list-edits
./.venv/bin/python -m cc_orchestrator.chat --plan <plan.json> --revert-edit e-20260807-001

# 修正から学習した内容の確認・再構成・無効化
./.venv/bin/python -m cc_orchestrator.chat --show-learned
./.venv/bin/python -m cc_orchestrator.chat --relearn
./.venv/bin/python -m cc_orchestrator.chat "今週の研究を..." --no-learned

# 多層分析 (R2a。既定 ON)
./.venv/bin/python -m cc_orchestrator.chat "今週の研究を..." --no-layers   # 切る
./.venv/bin/python -m cc_orchestrator.chat --layers-summary <plan.json>    # 要約表示
./.venv/bin/python -m cc_orchestrator.chat --layers-summary 20260807_120000

# テスト
./.venv/bin/pytest -m "not e2e"      # 431 件
```

描画先は `--target local`（Excalidraw MCP）が既定。ACA 到達前は
`--target file` で MCP なしに `.excalidraw` / SVG を生成でき、この経路でも
可変詳細度と独立検証が成立する（計画 §3-2 の fallback 要件）。

## Web アプリ（`docs/webapp-design.md`）

CLI と同じパイプラインをブラウザから使うローカル Web アプリ。

```bash
cd production
./.venv/bin/pip install -e ".[dev,web]"
./scripts/start_web.sh          # 127.0.0.1:8090（0.0.0.0 では bind しない）
open http://127.0.0.1:8090
```

```
ブラウザ（vanilla HTML/CSS/JS・ビルド工程なし・CDN なし）
  │ fetch（同一オリジン）
FastAPI 127.0.0.1:8090          src/cc_web/
  ├ JobManager（ThreadPoolExecutor max_workers=1 = 直列)
  │    └ run_pipeline(..., progress=…)   ← 既存 cc_orchestrator
  ├ セッション読取 graphs/layout_plan_session_*.json
  └ SVG 生成 cc_core.detail.project → cc_core.svg_export.build_svg
```

画面の流れ:

1. ホームでテンプレート 4 枚から選ぶ（またはそのまま依頼文を入力）→ 送信
2. 進捗カード（経路判定 → 資料収集 → 概念抽出 → **文脈ラベル付け → 主張の抽出** →
   関係の検証 → **主張の検証 → 論証と矛盾の検出** → 詳細度の計算 → ギャップ検出 →
   描画 → 独立検証 → 出力）を 1.5 秒間隔でポーリング表示
3. 完成後は結果カード: 概念数 / 関係数 / 島数 / 検証バッジ + 関係検証チップ
   （因果を維持 n・相関へ降格 n・矛盾を非断定化 n）+ 多層分析チップ
   （主張 n 件（検証済 m）・却下 k・矛盾 j 件）+ 地図・ギャップ・評価の 3 タブ
4. ヘッダーの詳細度セグメントで Overview / Standard / Detailed を切替。
   **切替は SVG の取り直しだけ**で LLM 呼び出しはゼロ（所要時間をトーストに表示）
5. 地図をクリック: 集約ノード → 展開モーダル、関係の線 → 根拠の引用ポップオーバー
   （正しい / 誤り / 判断不能の関係評価をその場で送信）。ポップオーバーには
   **機械タグ（内部 30 種）・検証スコアと判定・紐づく主張の本文**も出る。
   あなたが直した関係では「表示はあなたの指定です」と明記する
6. ギャップタブで候補を [有用] [却下] で確定（確定済みは無効化・有用率を表示）。
   各候補に**型バッジ**（構造 / 言説 / 因果）が付き、判断材料は hover で読める
7. 設定モーダルで多層分析（既定 ON）・因果の独立検証・学習の適用を切り替え

主なエンドポイント（すべて JSON、SVG のみ `image/svg+xml`）:

| Method / Path | 用途 |
|---|---|
| `GET /healthz` `/api/me` `/api/templates` | 稼働確認・アカウント（az CLI を 10 分キャッシュ）・テンプレ 4 件 |
| `GET/POST /api/files` | `inbox/` の一覧とアップロード（pdf/docx/txt/md のみ・basename 化） |
| `POST /api/jobs` → `GET /api/jobs/{id}` | 生成の投入（202）と進捗ポーリング |
| `GET /api/sessions[/{s}[/svg\|/view\|/excalidraw]]` | セッション一覧・KPI・詳細度別 SVG（`exports/web/` にキャッシュ）・突合用 JSON・シーンのダウンロード |
| `GET /api/sessions/{s}/layers` | 多層分析の記録（主張・論証・矛盾・統計）。R2a 以前の地図は 404 + 理由 |
| `POST /api/sessions/{s}/gaps/{gid}` | ギャップ確定（再確定は 409） |
| `POST /api/sessions/{s}/expand/{agg}` | 集約ノードの展開（未知は 404） |
| `POST /api/sessions/{s}/evaluation` | 満足度 / 関係評価 / 操作ログ → `logs/evaluation.jsonl` |
| `GET /api/history` | `logs/web_history.jsonl` の逆順 50 件 |
| `GET/POST /api/sessions/{s}/edits` | 編集履歴の取得と適用（8 操作。適用のたび再構成） |
| `POST /api/sessions/{s}/edits/{eid}/revert` | 編集の取り消し（二重取り消しは 409） |
| `GET /api/learned` | 過去の修正から学習した内容の要約 |
| `DELETE /api/files/{name}` | `inbox/` のファイル削除（アップロードの取り消し） |

`run_pipeline(..., offline=True)` は **Foundry を一切呼ばない**実行モード
（保存済み KG から詳細度計算以降だけを回す。`kg_file` 必須）。Web のテストは
この経路で Foundry も MCP も使わずに E2E を通している。

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
│  ├ layers.py       ◆ 4層30種の関係語彙・層タグ→8記号の決定的投影・⑦meta
│  ├ layer_assign.py ◆ 層タグの刻印（glyph/zone/L5 由来）・検証結果と矛盾の反映
│  ├ sentences.py    ◆ 決定的な文分割（ID は本文ハッシュで run 間安定）
│  ├ verifiers.py    ◆ 3検証器と合成・閾値3分岐・rejection_log
│  ├ layers_store.py ◆ layers_session サイドカーの I/O・nanopub_id 採番
│  ├ gaps.py         ★◆ ギャップ検出 3型（構造/言説/因果）・confirm/dismiss・有用率
│  ├ evaluation.py   ★ オンライン評価（v3 §7.2.1 の2系統ラベル）・KPI 集計
│  ├ svg_export.py   ★ ヘッドレス SVG（ブラウザ不要）
│  ├ layout.py       日本語幅推定グリッド（上位層の属性を保持するよう改修）
│  ├ adapter.py verify.py overlap.py textmetrics.py validate.py
│  ├ excalidraw_file.py mcp_client.py render.py
├ cc_orchestrator/
│  ├ routing.py      ★ Query Routing 3経路・詳細度/言語/タグ解釈
│  ├ analysis.py     ◆ cc-analysis の呼び出しと「形の修復」（zone/claims/cgw/refutes）
│  ├ pipeline.py     ⓪Routing→①Ingest→③抽出→②zone→③claims→④関係検証
│  │                 →⑤主張の検証→⑥論証と矛盾→詳細度→ギャップ→⑧描画→検証
│  ├ chat.py         CLI（生成・切替・展開・ギャップ確定・多層分析の要約）
│  └ agents_def.py foundry_v2.py ingest.py tool_exec.py portal_agent.py
├ cc_web/           ★ ローカル Web アプリ（FastAPI + 素の HTML/CSS/JS）
│  ├ app.py         API ルート（create_app ファクトリ）
│  ├ jobs.py        JobManager（直列実行・進捗・履歴）
│  ├ sessions.py    plan の読取 / SVG キャッシュ / ギャップ確定 / 展開
│  ├ account.py     az CLI から /api/me（10 分キャッシュ）
│  └ static/        index.html（アイコンはインライン SVG スプライト）・app.css・app.js
└ cc_tools/          FastMCP（ACA デプロイ単位）
```
★ = R1 の新規実装 / ◆ = R2a（知識モデルの多層化）の新規実装

## まだ実装していないもの（計画どおり R2b 以降）

- **ローカル NLI**（mDeBERTa-xnli）— インタフェースだけ確定。`CC_NLI_BACKEND=local`
  はスタブに当たると警告して LLM NLI へ落ちる（黙って落ちない）
- グラフ DB（PostgreSQL + AGE）・LazyGraphRAG・コーパス級 Leiden
- L5 の本格オントロジー（現在は BFO 上位 5 種 + `is_a` / `part_of` のみ）
- 編集後の再構成で**因果ギャップの `rejection_log` へのリンクが落ちる**
  （ギャップ 3 型そのものは再構成後も出る。`rebuild_session` は `detect_gaps(kg)`
  をログのパス無しで呼ぶため、出典リンクだけが欠ける）
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

## 概念図の編集とフィードバック学習（R1.5）

生成された地図はユーザーが直せる。**原本 (`kg_session_*.json`) は書き換えず**、
編集は `edits_session_*.jsonl` へ 1 行 1 操作で追記する。現在の姿は常に
`fold(原本, 編集ログ)` で決定的に再構成でき、取り消しも「取り消し行の追記」で表す。
AI が出したものと人が直したものの差分が永久に残るので、それが学習の材料になる。

編集できるのは 8 操作: 概念の改名 / 追加 / 削除、関係のラベル / 種類 / 向き / 削除 / 追加。
再構成では**島をシャッフルしない**（コミュニティを凍結）、**編集した概念は
どの詳細度でも消えない**（ピン留め）ようにしている。

「学習」はモデルの重みを変えるものではない。実体は 3 つ:

1. **決定的な自動適用** — 改名は用語辞書へ（写像が一意なときだけ）、削除は除外リストへ
   （同じ概念を 2 回以上消したときだけ）、関係の種類変更・向き反転は**因果上書き表**へ
   （以後その概念対は 3 点セットの LLM 検証を省いて確定。人の判断が最終権威）
2. **抽出プロンプトへの事例注入** — 見落とされた関係などを「過去の修正からの注意」として付加
3. **因果語彙の統計記録** — よく降格される手がかり語を記録（自動降格はせず、人が判断）

適用内容は必ず実行サマリに出る（黙って直さない）。`--no-learned` / Web の設定で無効化できる。

R1.5 より前のセッションは関係ポリシー適用**前**の KG を原本として保存していたため、
そのまま再構成すると降格済みの相関が因果矢印へ戻る【実測: 6 本】。`rebuild_session` は
前回 plan の判定を正として戻し、その内容を `provenance.policy_reconciled` に残す。

## 知識モデルの多層化（R2a）

地図に出る記号は 8 種だが、**内部では 4 層 30 種の関係語彙**を持っている。
記号はその 30 種を読めるところまで畳んだ結果でしかない。畳む前の情報は
`layer_tags` として残るので、関係をクリックすれば何を見てその記号にしたかが出る。

### 8 記号（画面に出るもの）

| 記号 | 意味 | 出る条件 |
|---|---|---|
| → | 因果 | 層 C に `causes` があり、**かつ裏付けがある**（下記） |
| 〜 | 相関 | 既定。因果の裏付けが足りなかったものもここへ落ちる |
| ⇒ | 補強 | 層 D の `corroborates` / `agrees_with` |
| ⚡ | 矛盾 | 層 D の `refutes`（**論証段が別モデルで矛盾と判定した対だけ**） |
| ◇ | 分類 | 層 A の `is_a`（資料に明示された分類関係） |
| ◧ | 構成 | 層 A の `part_of` |
| ≫ | 時系列 | 層 C の `precedes` |
| ? | 疑問 | 層 D の `questions` |

内部専用が 2 種ある。`hole`（ギャップ候補）と `tension`（非断定の対立候補）は
**機械が判断できていない状態**を表すので、編集メニューの選択肢には出さない。

### → と ⚡ が出る条件（正直な説明）

**→（因果）** が出るのは、次のどれかを満たしたときだけ:

1. 根拠テキストに機序・介入・反事実の語彙証拠があり、別モデルの独立検証も通った
2. 主張の検証スコア（3 検証器の重み付き平均）が 0.75 以上
3. あなたが過去に「これは因果だ」と直した概念対（人の判断が最終権威）

裏付けが足りなかった因果候補は**消さずに 〜（相関）として残す**。地図から
消すより、相関として見えていたほうが後で判断できるためで、クリックすれば
「因果の候補だったが裏付けが足りない」と出る。落ちた分は
`logs/rejections/rejections_<session>.jsonl` に理由つきで残る。

**⚡（矛盾）** が出るのは、抽出された主張の対を別モデルが「両立しない」と
判定し、**かつその対に対応する関係が地図に既にある**ときだけ。対応する関係が
無ければ矛盾は記録に残るだけで、⚡ のための新しい線は引かない。

どちらも「LLM がそう言ったから」では出ない。これは「学習」がモデルの再学習で
ないのと同じで、**言っていること以上のことをしない**という方針の現れ。

### 検証（3 検証器の合成）

抽出した主張はそのままでは地図に載らない。種類の違う検証器を 3 つ走らせ、
重み付き平均で 1 つのスコアにする:

| 検証器 | 重み | 何を見るか |
|---|---|---|
| NLI | 0.40 | 根拠文から主張が導けるか（含意関係） |
| 独立 LLM | 0.35 | 抽出とは**別モデル**が「根拠に明示されているか」を判定 |
| オントロジー | 0.25 | 決定的な整合性規則（LLM を使わない） |

判定は 3 分岐。0.75 以上 = 検証済み / 0.50 以上 = 要レビュー（登録は続ける）/
0.50 未満 = 却下（`rejection_log` へ記録し、地図の主張リンクからは外す）。
**走れなかった検証器は重みごと外して再正規化する** — モデルの障害が
「主張が弱い」に化けないようにするため。オントロジーだけしか走らなかった
対象は検証済みにしない（整合性検査は主張が正しいことの裏付けではない）。

### ギャップ 3 型

同じ地図から 3 種類のギャップを見分ける。型は**検出信号の種類**で、
`presumed_type`（データ不足 / 抽出漏れ / 真の空白）は**その原因の推定**。

| 型 | 何を見て見つけるか |
|---|---|
| 構造 | 孤立ノード・弱接続・テーマ間の断絶（R1 からの検出） |
| 言説 | 主張が紐づく概念なのに、手法（Method / Experiment）の文に基づく関係が 1 本も無い |
| 因果 | 因果の候補として検証にかけたが裏付けが足りず、相関止まりになった関係 |

各候補は「何を見たか（grounds）」「どの規則で判断したか（warrant）」を
レコード自身に持つ。ギャップの確定は人が行う（裁定 8）ので、判断材料を
候補と同じ場所に置いてある。

### LLM 呼び出しの上限

多層分析は既定 ON だが、**既定値だけで最悪 29 call/run 以下**に収まるよう
上限を決めてある（文脈ラベル 8 + 主張 2 + 検証 16 + 論証 2 + 矛盾 1）。
実測値は `summary["layers"]["stats"]["llm_calls"]` に出る。
環境変数（`CC_ZONE_MAX_SENTENCES` / `CC_VALIDATE_MAX_CALLS` ほか）で更に絞れる。

多層分析は `--no-layers` / Web の設定で切れる。切ると R1.5 と同じ、
語彙証拠だけの地図になる（記号は 8 種のまま。層タグが無いので → の条件が
3 点セットだけになる）。

## 検証状況

### R1 / R1.5

- ユニット / 統合テスト（可変詳細度 21 件・R1 機能 44 件・正規化 20 件・
  Web アプリ 34 件・編集 30 件・学習 20 件・Web 編集 API 16 件ほか）
- 実キャンバスへの 3 レベル描画で **すべて検証 PASS**（overview 38 / standard 48 / detailed 48 要素）
- `--target file`（MCP なし fallback）でも描画 82 要素・オフライン検証 PASS
- スケール実測: 400 概念まで帯遵守・重なりゼロ・0.18 秒
- **Work IQ 経由の実運用フロー成功**（2026-08-07）: 資料 4 件 → 概念 16 / 関係 16 /
  島 5 → overview 12(集約2) / standard 16 / detailed 16 → 描画・検証 PASS →
  3 レベルの SVG 出力

### R2a（知識モデルの多層化）

- ユニット / 統合テスト **431 件 pass**（R2a 分 197 件: 層と投影 63・文分割と
  主張抽出 58・検証と論証 43・統合フリップ 33）
- モック E2E で 8 記号のうち → 〜 ⇒ ⚡ ◇ ◧ が点灯、layers サイドカーと
  rejection_log を出力、**LLM 呼び出し 18 call**（上限 30）
- **実 Foundry で検証エージェントの契約を実測**（2026-08-07）: `task: "nli"` /
  `"claim_check"` の 3 call とも**ツールを呼ばず純 JSON で応答**。
  含意あり = `entails 0.98` / 根拠にある主張 = `supported 0.995` /
  根拠に無い主張 = `unsupported 0.005`
- 3 世代（R1.5 以前 / R1.5 / R2a）の地図で読込・改名・種類変更・再構成が成立。
  触っていない関係の記号は再構成で動かない
- headless Chrome で **JS エラー 0**。8 記号の編集メニュー・機械タグの
  クリック展開・ギャップ型バッジ・多層分析トグルを確認
