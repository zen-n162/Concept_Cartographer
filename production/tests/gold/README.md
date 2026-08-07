# 日本語正解セット (gold) — 書き方

R2c 設計書 §1.1 / 裁定 O・P・Q。ここに置いた `*.jsonl` が
`--offline-eval` の正解セットになります。**LLM は使いません** — 疑似ラベルを
作るモデルと測られるモデルが同じでは KPI の意味が無いためです (裁定 O)。

## 目標

| 対象 | 目標件数 | ファイル |
|---|---|---|
| 関係の判定 | 150 件 | `relations_gold.jsonl` |
| ギャップの判定 | 50 件 | `gaps_gold.jsonl` |

進捗は `python -m cc_orchestrator.chat --gold-status` で確認できます。
**このディレクトリが空でも全機能が動きます** — その場合は Web のクリック評価
(`logs/evaluation.jsonl`) だけが正解セットになります。

## 2 系統の正解セット (裁定 O)

1. **人間のクリック評価** — Web の地図タブで関係をクリックし
   「正しい / 誤り / 判断不能」を選ぶ。`logs/evaluation.jsonl` へ自動で溜まる。
   誤った関係の削除・付け替えも「誤り」として自動記録される
2. **この gold ファイル** — 腰を据えて付ける判定。クリックでは付けられない
   `causal_ok` (矢印表示の是非) を書けるのはこちらだけ

同じ関係を両方で判定したときは **gold が勝ちます**。

## relations_gold.jsonl

1 行 1 判定の JSON Lines。語彙は `evaluation.jsonl` の `judge_relation` と同じ。

```jsonc
{"from_label": "被ばく線量", "to_label": "細胞損傷", "verdict": "correct",
 "causal_ok": true,
 "session": "20260807_143804",
 "edge_id": "r002",
 "labeled_by": "nakamura.zen@qst.go.jp", "ts": "2026-08-07T15:00:00", "note": "…"}
```

| キー | 必須 | 意味 |
|---|---|---|
| `verdict` | ✅ | `correct` / `incorrect` / `undecidable` |
| `from_label` / `to_label` | ▲ | 概念名。`session`+`edge_id` が無いときは必須 |
| `causal_ok` | | `true`/`false`。**矢印 (因果) として描いてよいか**。因果精度の分子分母 |
| `session` / `edge_id` | | あれば照合が正確になる (裁定 P) |
| `labeled_by` | | 判定した人 |
| `ts` | | ISO8601。**同じ関係の判定が競合したとき最新が勝つ** |
| `note` | | 自由記述 (指標には使わない) |

### `verdict` と `causal_ok` は別の問い

- `verdict` = 「この関係は**あるか**」
- `causal_ok` = 「その関係を**矢印 (因果) として描いてよいか**」

相関はあるが因果ではない、という関係は `verdict: "correct"` かつ
`causal_ok: false` になります。クリック評価には `causal_ok` の欄が無いので、
**因果精度はこの gold ファイルが育つまで測れません** (verdict から機械的に
推測することはしません — 2 つの問いを混同するため)。

### 照合のしかた (裁定 P)

1. `session` + `edge_id` が揃っていればそれで現在の知識グラフと突き合わせる
2. 当たらなければ**正規化ラベル対** (NFKC + trim + casefold) で探す
3. 同じ関係に矛盾する判定が複数あれば **`ts` が最新のものが勝ち**、件数は 1

`user_edited` / `user_added` の関係は**全指標の分母から外れます** — 人が直した
ものを混ぜると「直せば直すほど精度が上がる」誤った読みになるためです。

## gaps_gold.jsonl

既存の confirm / dismiss 語彙 (ギャップ有用率と同じ)。

```jsonc
{"gap_id": "gap-isolated-c003", "session": "20260807_143804",
 "decision": "confirm", "labeled_by": "…", "ts": "…", "note": "…"}
```

`decision` は `confirm` (有用) / `dismiss` (無意味・誤検知)。plan 側で既に
確定済みのギャップと同じ `gap_id` を書いた場合は gold が勝ちます。

関係とギャップは**中身で振り分けます** (`gap_id` か `decision` があればギャップ)。
ファイル名は自由なので `gold_2026q3.jsonl` のような分け方もできます。

## 作業の進め方

```bash
cd production
./.venv/bin/python -m cc_orchestrator.chat --gold-status      # 進捗
./.venv/bin/python -m cc_orchestrator.chat --gold-queue 20    # 次に付ける 20 件
./.venv/bin/python -m cc_orchestrator.chat --offline-eval     # 指標
```

`--gold-queue` は未判定の関係を **glyph 層化サンプリング**で出します。新しい順に
上から付けると正解セットが特定の glyph (たとえば因果の矢印) に偏り、相関や対立の
関係が測られないまま数字だけ良く見えるためです。乱数は使っていないので、
同じ状態なら常に同じキューが出ます。

## 外部ベンチマークについて (裁定 Q)

SciNLI / DiagramEval / RAGAS / SciClaimHunt は**英語データセット**のため
**参考文書扱い**とし、実装しません。日本語 KPI はこの自前の正解セットだけで
測ります。英語ベンチの数字を日本語の実運用 KPI として掲げると、測っていない
ものを測ったことにしてしまうためです。

## 注意

- このディレクトリの `*.jsonl` は**実データ (研究内容) を含みうる**ので、
  リポジトリへコミットする前に共有可否を確認してください
- `*.jsonl.example` は拡張子が違うので正解セットには読み込まれません
- JSON として読めない行は警告を出して飛ばします (1 行の書き損じで
  正解セット全体を失わないため)
