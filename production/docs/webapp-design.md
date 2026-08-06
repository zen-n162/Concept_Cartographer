# Concept Cartographer Web アプリ 詳細設計書 (R1)

- 設計: Fable 5 / 実装: Opus 5 (本書に従う)
- 目的: 実装済みの実運用版 R1 パイプライン (`cc_orchestrator` / `cc_core`) を、
  添付 UI モック `concept_cartographer_ui_v2_refined_header.html` の外観を持つ
  ローカル Web アプリとして提供する。
- 対象外: 認証基盤 (R1 は az CLI のシングルユーザー)、チーム/機構横断モード
  (UI 上は「準備中」で無効表示)、ACA デプロイ (別途 infra)。

---

## 1. アーキテクチャ

```
ブラウザ (SPA: vanilla HTML/CSS/JS, ビルド工程なし)
   │  fetch (同一オリジン)
FastAPI (127.0.0.1:8090, uvicorn)          ← 新規 src/cc_web/
   ├ JobManager (ThreadPoolExecutor max_workers=1)   ← パイプラインは重い +
   │     └ run_pipeline(...)  [既存 cc_orchestrator]    キャンバスが共有状態の
   ├ 静的配信 (static/)                                  ため直列実行
   ├ セッション読取 (graphs/layout_plan_session_*.json)
   └ SVG 生成 (cc_core.detail.project + cc_core.svg_export.build_svg)
```

原則:
- **フレームワーク・CDN・ビルド工程を使わない**。閉域でも動くよう、アイコンは
  インライン SVG スプライト、フォントはシステム (Hiragino 系)。
- **バインドは 127.0.0.1 のみ** (引き継ぎメモ §4)。0.0.0.0 で起動しない。
- **研究本文をサーバーログへ出さない** (既存 `cc_core.logging_util` の方針)。
  依頼文・履歴は `logs/web_history.jsonl` にローカル保存 (閉域前提で許容)。
- パイプラインは内部で `asyncio.run` を使う同期関数 → **必ずワーカースレッド**で
  実行 (イベントループ内で直接呼ばない)。

## 2. ディレクトリ構成 (新規分)

```
production/
├ src/cc_web/
│  ├ __init__.py
│  ├ app.py            # FastAPI アプリ (create_app() ファクトリ + ルート)
│  ├ jobs.py           # JobManager / Job dataclass / 進捗ステージ定義
│  ├ sessions.py       # plan の読取・SVG キャッシュ・ギャップ操作・展開
│  ├ account.py        # az CLI から /api/me (10分キャッシュ)
│  └ static/
│     ├ index.html     # SPA 本体 + <svg><symbol> アイコンスプライト
│     ├ app.css
│     └ app.js
├ scripts/start_web.sh # uvicorn 起動 (127.0.0.1:8090)
└ tests/test_web_app.py
```

pyproject に extras 追加: `web = ["fastapi>=0.110", "uvicorn>=0.27", "python-multipart>=0.0.9"]`

## 3. 既存コードへの変更 (最小差分・3点のみ)

### 3.1 `cc_orchestrator/pipeline.py` — 進捗フックと offline 実行

```python
ProgressFn = Callable[[str, str], None]   # (stage_key, 日本語ラベル)

def run_pipeline(..., progress: ProgressFn | None = None,
                 offline: bool = False) -> dict:
```

- 各ステージ開始時に `progress(key, label)` を呼ぶ (例外は握りつぶす)。
  ステージ (キー→ラベル):
  `routing→経路判定 / ingest→資料収集 / extract→概念抽出 / relate→関係の検証 /
   detail→詳細度の計算 / gaps→ギャップ検出 / render→描画 / verify→独立検証 /
   export→出力`
- `offline=True` (テスト・再描画用): **Foundry を一切呼ばない**。
  - `kg_file` 必須 (無ければ ValueError)
  - `FoundryAgentsV2()` / `ensure_agents` を生成しない
  - 因果検証は verifier=None (語彙証拠のみ)
  - 描画は `executor("render_layout_plan", {"plan": view})` を直接呼ぶ
  - 検証は `executor("verify_scene", {})` の `passed` から
    `{"verdict": "PASS"|"FAIL", "summary": ...}` を組み立てる
  - それ以外 (詳細度・ギャップ・SVG・KPI) は通常どおり

### 3.2 `cc_core/svg_export.py` — クリック対象の data 属性

- ノードの `<g>` に `data-node-id="{id}" data-kind="{concept|aggregate}" class="cc-node"`
  (集約は `data-aggregate-id` も)
- エッジの `<g>`(線) とラベル `<text>` に `data-edge-id="{id}" class="cc-edge"`
- 島の `<g>` に `data-island-id="{community_id}"`
- 決定性は維持 (既存テスト `test_svg_is_deterministic` を壊さない)

### 3.3 変更しないもの

`cc_core` の他モジュール・`agents_def`・`foundry_v2` は変更しない。
ギャップ確定は既存 `cc_core.gaps.apply_decision` を、展開は
`cc_core.community.expand_aggregate` を、評価は `cc_core.evaluation` を使う。

## 4. バックエンド API 仕様

すべて JSON (SVG エンドポイントのみ `image/svg+xml`)。エラーは
`{"error": {"message": str}}` + 適切な 4xx/5xx。

| Method/Path | 入力 | 出力 |
|---|---|---|
| GET `/healthz` | — | `{"ok": true}` |
| GET `/` | — | index.html |
| GET `/api/me` | — | `{"name","upn","initials","signed_in","mode":"personal"}`。`az account show --query user.name` を 10 分キャッシュ。UPN 先頭 (`nakamura.zen`) を Title Case で name に。失敗時 `signed_in:false, name:"ローカル ユーザー"` |
| GET `/api/templates` | — | §6.4 の 4 件を返す |
| GET `/api/files` | — | `{"files":[{"name","size","ext"}]}` — `inbox/` を列挙 (mtime 降順) |
| POST `/api/files` | multipart `files` (複数可) | `{"saved":[...]}`。拡張子 `pdf/docx/txt/md` のみ許可、他は 400。保存先 `inbox/` (ファイル名は basename のみ使用しパス要素を除去) |
| POST `/api/jobs` | `{"message", "level"?, "local_only"?:bool, "causal_verify"?:bool, "kg_file"?, "target"?:"local"\|"file", "offline"?:bool}` | `202 {"job_id"}` |
| GET `/api/jobs/{id}` | — | `{"job_id","status":"queued\|running\|done\|error", "stage":{"key","label"}\|null, "stages_done":[keys...], "summary":<run_pipeline の戻り値>\|null, "error":str\|null, "created_at","finished_at"}` |
| GET `/api/sessions` | — | `{"sessions":[{"session","created_at","title","levels","default_level"}]}` — `graphs/layout_plan_session_*.json` を mtime 降順で。title は履歴の依頼文 (無ければ session ID) |
| GET `/api/sessions/{s}` | — | `{"session","levels","default_level","gaps_usefulness":<usefulness_rate>,"kpi":<evaluation.summarize(view,[])>}` |
| GET `/api/sessions/{s}/svg?level=standard` | — | SVG。`project(plan, level)` → `build_svg` → `exports/web/{s}_{level}.svg` にキャッシュし返す (plan の mtime が新しければ再生成) |
| GET `/api/sessions/{s}/view?level=` | — | `project()` の結果から UI に必要な部分: `{"nodes":[{id,label,kind,community_id,importance?,aggregate_id?}], "edges":[{id,from,to,label,glyph,confidence?,epistemic_status?,evidence_span?,causal_check?,member_edge_ids?}], "aggregates":[...], "gaps":[...], "levels":...}`。`_level_plans` は返さない (重いので除外) |
| POST `/api/sessions/{s}/gaps/{gid}` | `{"decision":"confirm"\|"dismiss"}` | `{"gap":<更新後>, "usefulness":<usefulness_rate>}`。user_id は /api/me の upn。plan ファイルへ保存。GapDecisionError → 409 |
| POST `/api/sessions/{s}/expand/{agg}` | — | `{"aggregate":<定義>, "members":[{"id","label"}]}` (ラベルは `_level_plans.detailed` から引く)。KeyError → 404 |
| POST `/api/sessions/{s}/evaluation` | `{"satisfaction":1-5}` または `{"edge_id","verdict":"correct\|incorrect\|undecidable"}` または `{"operation":..., ...}` | `{"ok":true}` — `EvaluationStore("logs/evaluation.jsonl")` に session 単位で追記 (map_id=セッションID, user_id=upn) |
| GET `/api/history` | — | `{"items":[{"ts","message","job_id","session"?,"status","route"?}]}` — `logs/web_history.jsonl` 逆順 50 件 |

JobManager 仕様 (`jobs.py`):
- `ThreadPoolExecutor(max_workers=1)`。キャンバス (Excalidraw MCP) が共有状態の
  ため**直列**。2 件目以降は queued。
- Job 完了/失敗時に `web_history.jsonl` へ 1 行追記
  (`{ts, message, job_id, session?, status, route?}`)。
- 進捗は `progress` フックで Job.stage / stages_done を更新。
- `summary["answer"]` がある場合 (basic/vector 経路) は地図なし応答として扱う。

## 5. フロントエンド仕様

### 5.1 デザイントークン (モック準拠・CSS カスタムプロパティ)

```css
--ink:#2C2C2A; --bg:#FDFDFC; --sidebar:#FAFAF8; --border:#E8E6E0;
--muted:#5F5E5A; --faint:#888780; --placeholder:#B4B2A9; --input-border:#D3D1C7;
--label:#444441;
--indigo-900:#26215C; --indigo-700:#3C3489; --indigo-500:#534AB7;
--indigo-400:#7F77DD; --indigo-300:#AFA9EC; --indigo-200:#CECBF6; --indigo-50:#EEEDFE;
--pink-500:#D4537E; --pink-700:#993556; --pink-50:#FBEAF0;
--green-400:#5DCAA5; --green-900:#04342C; --green-300:#9FE1CB;
--green-700:#0F6E56; --green-50:#E1F5EE;
--orange-700:#993C1D; --orange-50:#FAECE7;
--pdf:#A32D2D; --docx:#185FA5; --xlsx:#0F6E56;
--grad-title:linear-gradient(90deg,#534AB7,#D4537E);
--hdr-card-bg:rgba(255,255,255,0.07); --hdr-card-border:rgba(206,203,246,0.35);
```

- 角丸: カード 12px / チップ・ボタン 8px / 入力欄 14px / 送信ボタン 9px
- 罫線: `0.5px solid var(--border)` (非対応環境は 1px にフォールバックで可)
- フォント: `"Hiragino Sans","Hiragino Kaku Gothic ProN","Yu Gothic",sans-serif`
- 実寸: モックは縮小版。実アプリは **サイドバー 240px・基本フォント 13.5px**、
  ヘッダーカードや余白はモックの比率を保って拡大する。

### 5.2 レイアウト (モックの構造を踏襲)

```
<body> grid: [sidebar 240px | main]
 sidebar (#FAFAF8, 右罫線):
   ロゴ (28px 角丸8 #26215C + topology アイコン #CECBF6) + 「Concept␣Cartographer」2行
   [新しいチャット]  ← active 状態: bg #EEEDFE / 文字 #3C3489 / 角丸8
   [チャット履歴] + 履歴 4 件 (30px インデント・ellipsis) + すべて表示
   ── 区切り ──
   [アップロードしたファイル] + ファイル 3 件 (種別アイコン色 + name + "PDF · 2.3 MB")
   + すべてのファイルを表示 (#534AB7)
   ── (margin-top:auto) ──
   ヘルプ / フィードバック / 設定 / « サイドバーを閉じる
 main: flex column
   header (#26215C, 下罫線, 12px 16px, 3 カード横並び gap 10):
     [モードカード flex:1.3]  丸アバター #7F77DD + 「モード: 個人モード ▾」#EEEDFE
        + 説明 #AFA9EC 「自分専用の作業スペースで、個人の研究を整理・可視化します」+ info
     [詳細度カード flex:1.1]  ラベル「詳細度」#AFA9EC 10px +
        セグメント (bg rgba(11,11,11,.25) 角丸8 padding2):
        選択中 = bg #EEEDFE 文字 #26215C / 非選択 = 文字 #CECBF6
     [アカウントカード flex:1.1] イニシャル丸 #5DCAA5/#04342C + 氏名 #EEEDFE ▾
        + @ドメイン #AFA9EC + 「✓ Microsoft 365 でサインイン中」#9FE1CB
   content (flex:1, scroll):
     ホームビュー / セッションビュー (5.3)
   フッター入力 (content 下部固定):
     入力ボックス (角丸14, border #D3D1C7): placeholder「研究について何でも聞いてください...」
       下段: [📎 ファイルをアップロード ▾] [🌐] … 右端 [送信 32px 角丸9 #534AB7]
     免責: 「AI が生成した回答は必ずしも正確とは限りません。重要な情報はご自身で
     ご確認ください。詳細情報」 10px #888780 中央
```

### 5.3 ビューと挙動

**ホームビュー** (初期状態):
- 中央グラデーションタイトル `Concept Cartographer` (26px→実寸 30px,
  `background-clip:text`) + サブ「研究の断片をつなぎ、意味ある全体像へ」
- 「試してみる」ラベル + テンプレートカード 4 枚 (grid 4 列, §6.4)。
  クリック → 入力欄へ message を挿入しフォーカス。
- 右下「すべてのテンプレートを表示 ›」はダミーリンク (R1 は 4 件のみ)。

**送信フロー**:
1. 送信 → `POST /api/jobs` (level = ヘッダーの選択値、明示指定があれば優先)
2. セッションビューへ切替: ユーザー発話バブル + **進捗カード**
   (ステージのチェックリスト。`GET /api/jobs/{id}` を 1.5 秒間隔でポーリング。
   完了済み=✓ #0F6E56 / 実行中=スピナー / 未着手=グレー)
3. `status=done`:
   - `summary.answer` あり (basic/vector 経路) → 回答バブルを表示して終了
   - 地図あり → **結果カード**:
     - サマリ行: 概念数 / 関係数 / 島数 / 検証 PASS バッジ (緑) or FAIL (赤)
     - 関係検証チップ: 「因果を維持 n / 相関へ降格 n / 矛盾を非断定化 n」
     - **地図パネル**: `GET /api/sessions/{s}/svg?level=X` を `<div class="map-wrap">`
       に inline 展開 (fetch text → innerHTML)。横スクロール可。
       右上にミニツールバー: レベル表示 / .excalidraw と SVG のダウンロードリンク
     - タブ: [地図] [ギャップ (n)] [評価]
     - 満足度: ★×5 (クリックで POST evaluation {satisfaction})
4. `status=error` → 赤カードでエラー本文 (先頭 300 字) + 再試行ボタン

**詳細度切替** (ヘッダーのセグメント):
- 開いているセッションがあれば `GET .../svg?level=` を取得して差し替え、
  `POST evaluation {"operation":"level_switch","to":level}` を送る。
- 所要時間を計測しトースト表示: 「Overview に切替 (LLM 呼び出しゼロ・{ms}ms)」
- セッションが無ければ既定レベルの変更のみ (次回生成に使用)。
- 切替直後にヘッダーのセグメント下に小さく各レベルのノード数を表示
  (`levels` から: `Overview 12 · Standard 16 · Detailed 16`)。

**地図のインタラクション** (view JSON を突合に使う):
- `.cc-node[data-kind="aggregate"]` クリック → `POST expand` → モーダル:
  集約ラベル + メンバー一覧 + ボタン「Detailed で開く」(レベル切替)
- `.cc-edge` クリック → ポップオーバー:
  glyph バッジ (因果=赤/相関=青/補強=緑/対立候補=灰/ギャップ=灰破線) +
  `evidence_span[].surface` の引用 (最大 2 件, 出典 document_id) +
  `causal_check.reason` (あれば) +
  関係評価ボタン [正しい / 誤り / 判断不能] → POST evaluation {edge_id, verdict}
  (押下後は選択状態を保持)
- **ギャップタブ**: `gaps` を一覧。各行: 状態アイコン (○候補/✅有用/✖却下) +
  信頼度バー + 推定分類バッジ (data/extraction/true/unknown) + 理由 +
  出典リンク (document_id 名) + [有用] [却下] ボタン → `POST gaps/{gid}`。
  上部に有用率チップ (`usefulness_rate`)。確定済みはボタン無効。
- **評価タブ**: 満足度★ + これまでの関係評価数 + evidence 表示率チップ
  (kpi.evidence_display)。

**サイドバー**:
- 履歴クリック → そのセッションを開く (`GET /api/sessions/{s}` + svg)。
  session が無い項目 (basic 応答等) は履歴表示のみ。
- ファイル: [ファイルをアップロード] ボタン (input type=file multiple,
  accept .pdf,.docx,.txt,.md) → POST → 一覧更新。アイコン色は拡張子で
  (--pdf/--docx/--xlsx、md/txt は --muted)。
- モードカード ▾ → ドロップダウン「個人モード ✓ / チームモード (準備中) /
  機構横断モード (準備中)」後 2 者は disabled (title=计画 R3)。
- 設定モーダル: 既定詳細度 / 因果の独立検証 ON・OFF / Work IQ を使わない
  (local_only) — localStorage 保存、ジョブ送信時に反映。
- サイドバー折りたたみ: 幅 0 へ (localStorage 記憶)。
- ヘルプ: 使い方 (テンプレ/詳細度/ギャップ確定) の静的モーダル。
  フィードバック: 「R1 パイロット中。評価は★と関係評価から送ってください」モーダル。

### 5.4 アイコン (インライン SVG スプライト)

`index.html` 冒頭に `<svg style="display:none"><symbol id="i-...">` で定義し、
`<svg class="ic"><use href="#i-edit"/></svg>` で使う。Tabler Icons (MIT) の
24×24・stroke=2・round を手書きで移植。必要セット:
`topology-star-3, edit, history, folder, file-pdf, file-docx, file-xls, file-text,
help, message-2, settings, chevrons-left, chevrons-right, user, chevron-down,
chevron-right, info-circle, map-2, hierarchy-2, bulb, chart-dots-3, paperclip,
world, send, circle-check, x, star, star-filled, refresh, download, loader-2(回転)`

## 6. 固定コンテンツ

### 6.4 テンプレート 4 件 (`/api/templates`)

| id | icon / 配色 | title | description | message |
|---|---|---|---|---|
| weekly | map-2 / #EEEDFE·#534AB7 | 今週の研究を概念地図として整理して | 今週の研究内容を概念地図にまとめ、主要な概念と関係性を可視化。 | 今週の研究を概念地図として整理して |
| prior | hierarchy-2 / #E1F5EE·#0F6E56 | 先行研究の関係性を概念地図にして | アップロードした論文から先行研究のつながりを整理。 | アップロードした資料から先行研究の関係性を概念地図にして |
| ideas | bulb / #FBEAF0·#993556 | 研究アイデアを広げて整理して | テーマに関連する概念を広げ、構造的に整理。 | 研究アイデアを広げて概念地図として整理して |
| causal | chart-dots-3 / #FAECE7·#993C1D | 実験結果の因果関係を整理して | 実験結果から読み取れる因果関係を地図化し示唆を導出。 | 実験結果の因果関係を概念地図として整理して |

## 7. テスト計画 (`tests/test_web_app.py`)

FastAPI TestClient + `offline` ジョブで **Foundry・MCP に一切依存せず**通す。

前提 fixture: `tmp_path` に作業ディレクトリを作り (`monkeypatch.chdir`)、
`graphs/` に本物の KG fixture (production/graphs の実データをコピー) を置く。
アプリは `create_app()` で生成。

必須テスト:
1. `/healthz`, `/`(index.html 配信), `/api/templates`(4件), `/api/me`(形)
2. **offline ジョブ E2E**: POST /api/jobs {message, kg_file, offline:true,
   causal_verify:false, target:"file"} → ポーリング (最大 60 秒) → done →
   summary.levels が 3 レベル / band_check OK
3. セッション API: 一覧に出る / `svg?level=overview|standard|detailed` が
   `<svg` で始まり `data-node-id` を含む / view JSON に nodes・edges・gaps
4. 詳細度ごとの svg が異なる (overview のノード数 < detailed)
5. ギャップ: 一覧 → confirm → status/confirmed_by 反映 + usefulness 更新 /
   二重確定 409 / 未知 gap_id 404
6. expand: 集約がある場合 members が返る / 未知 agg 404
7. evaluation: satisfaction / edge verdict / operation の 3 形 → logs に追記
8. files: .md アップロード → 一覧に出る / .exe は 400 / パストラバーサル名
   (`../evil.md`) が basename 化される
9. jobs: 未知 job_id 404 / 2 件投入で直列 (2 件目が queued を経る)
10. 既存回帰: `pytest -m "not e2e"` 全体が通ること (svg data 属性追加で
    既存 svg テストを壊していないこと)

## 8. 起動・運用

```bash
cd production
./.venv/bin/pip install -e ".[dev,web]"
./scripts/start_web.sh            # 127.0.0.1:8090
open http://127.0.0.1:8090
```

- `scripts/start_web.sh`: 既存 canvas/gateway (127.0.0.1:3000/8000) の稼働を
  healthz で確認し、無ければ警告表示 (target=file は動く旨)。
  `exec ./.venv/bin/uvicorn cc_web.app:app --host 127.0.0.1 --port ${CC_WEB_PORT:-8090}`
- README.md に「Web アプリ」節を追加 (起動・機能・スクリーンフロー)。

## 9. 実装順序 (Opus 5 への指示)

1. pyproject extras + pipeline の progress/offline (§3.1) + svg data 属性 (§3.2)
2. cc_web バックエンド (jobs → sessions → app) + start_web.sh
3. tests/test_web_app.py を書き、offline E2E を通す
4. フロントエンド (index.html / app.css / app.js) — モック忠実、§5 の挙動
5. `pytest -m "not e2e"` 全通過 + uvicorn 起動して主要 API を curl 確認
6. README 追記。**コミットはしない** (検収後に Fable 側で行う)

受け入れ基準:
- テスト全通過 (既存含む)、offline ジョブ E2E が Foundry なしで完走
- `GET /` の見た目がモックのトークン (色・角丸・3枚組ヘッダー・グラデタイトル・
  サイドバー構成・テンプレ 4 枚・入力欄・免責文言) を再現している
- 詳細度切替が再生成なしで動き、地図クリックでギャップ確定と関係評価が送れる
