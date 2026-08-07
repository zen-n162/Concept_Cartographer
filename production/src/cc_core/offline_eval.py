"""オフライン評価と日本語正解セット (R2c 設計書 §1・裁定 O/P/Q)。

R1 の `cc_core.evaluation` は「いま画面に出ている地図」に対するオンライン集計
だった。こちらは**溜まった人間の判定を正解セットとして扱い**、現在の知識グラフ
と突き合わせて KPI を測る。両者は別物なので混ぜない — オンライン側は 1 セッション
のスナップショット、こちらはコーパス横断の累積である。

**LLM は 1 回も呼ばない** (裁定 O)。KPI の客観性が目的なので、疑似ラベルを作る
モデルと測られるモデルが同じでは意味がない。ラベルの出所は次の 2 系統だけ:

  1. 人間のクリック評価 — `logs/evaluation.jsonl` (Web の関係評価、編集の含意)
  2. 専用 gold ファイル — `tests/gold/*.jsonl` (腰を据えて付けた判定)

裁定 P (照合キー) の実装は `Label.key` と `match_labels` にある:

  - セッション + edge_id が揃っていればそれで照合する
  - 無ければ**正規化ラベル対** `(from_norm, to_norm)` で照合する
    (`editing.normalize_label` を再利用 — 照合の正規化規則を 2 か所に
     書くと、片方だけ直したときに黙って取りこぼす)
  - 同一対に矛盾する判定が複数あれば **ts 順で最新が勝ち**、件数は 1

`user_edited` / `user_added` のエッジは全指標の分母から外す。既存 KPI
(`evaluation.causal_precision_log`) と同じ原則で、人が直したものを混ぜると
「直せば直すほど精度が上がる」誤った読みになるため。

外部ベンチ (SciNLI / DiagramEval / RAGAS / SciClaimHunt) は英語データセット
なので参考文書扱いとし、ここでは実装しない (裁定 Q)。日本語 KPI は自前の
正解セットだけで測る。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from cc_core.editing import GRAPHS_DIR, normalize_label
from cc_core.evaluation import RELATION_VERDICTS
from cc_core.gaps import usefulness_rate
from cc_core.layers import CAUSAL_GLYPH
from cc_core.logging_util import get_logger

logger = get_logger("cc_core.offline_eval")

EVAL_LOG = "logs/evaluation.jsonl"
GOLD_DIR = "tests/gold"
REPORT_DIR = "logs"

# 裁定 O の到達目標。「あと何件付ければ KPI が意味を持つか」を機械で出すための数。
RELATION_GOLD_TARGET = 150
GAP_GOLD_TARGET = 50

# 指標の目標値 (R2c 設計書 §1.2)
RELATION_ACCURACY_TARGET = 0.70
CAUSAL_PRECISION_TARGET = 0.70
GAP_USEFULNESS_TARGET = 0.40

# gaps_gold.jsonl の語彙 (既存の confirm/dismiss と同じ)。plan 側の status へ写す
GAP_DECISIONS = {"confirm": "confirmed", "dismiss": "dismissed",
                 "confirmed": "confirmed", "dismissed": "dismissed"}

EMPTY_HINT = (
    "まだ関係の判定がありません。集め方は 3 通りです: "
    "(1) Web の地図タブで関係をクリックし「正しい / 誤り / 判断不能」を選ぶ "
    "(2) 誤った関係を削除・付け替えする (編集は自動で「誤り」判定として記録されます) "
    "(3) tests/gold/relations_gold.jsonl に判定を直接書く "
    f"(書き方は {GOLD_DIR}/README.md)。"
    f"目標は関係 {RELATION_GOLD_TARGET} 件 / ギャップ {GAP_GOLD_TARGET} 件です。"
)


# ------------------------------------------------------------------ 型


@dataclass(frozen=True)
class Label:
    """関係 1 本に対する人間の判定 1 件。

    `causal_ok` は verdict とは**別の判断**である。verdict は「この関係は
    あるか」、causal_ok は「矢印 (因果) として描いてよいか」で、後者だけを
    因果精度の分子分母に使う。クリック評価には causal_ok が無いので、
    因果精度は gold ファイルが育つまで分母 0 のままになる — これは仕様で、
    verdict から機械的に推測して埋めると 2 つの問いを混同する。
    """

    verdict: str
    source: str                      # "click" | "gold"
    ts: str = ""
    session: str | None = None
    edge_id: str | None = None
    from_norm: str | None = None
    to_norm: str | None = None
    causal_ok: bool | None = None
    labeled_by: str | None = None
    note: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        """裁定 P の照合キー。session+edge_id を優先し、無ければ正規化ラベル対。"""
        if self.session and self.edge_id:
            return ("edge", self.session, self.edge_id)
        return ("pair", self.from_norm or "", self.to_norm or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict, "source": self.source, "ts": self.ts,
            "session": self.session, "edge_id": self.edge_id,
            "from_norm": self.from_norm, "to_norm": self.to_norm,
            "causal_ok": self.causal_ok, "labeled_by": self.labeled_by,
            "note": self.note,
        }


@dataclass
class Matched:
    """判定と、現在の KG のエッジとの突合結果。

    `status` は 3 値:
      matched     現在の KG に生きていて AI 由来 — 全指標の分母に入る
      user_origin ユーザーが足した/直した関係 — 分母から外す
      missing     現在の KG に無い (削除された / 別セッションの古い判定)
    """

    label: Label
    status: str
    session: str | None = None
    edge_id: str | None = None
    glyph: str | None = None
    from_norm: str | None = None
    to_norm: str | None = None
    from_label: str | None = None
    to_label: str | None = None

    @property
    def counts(self) -> bool:
        return self.status == "matched"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "session": self.session, "edge_id": self.edge_id,
            "glyph": self.glyph, "from_label": self.from_label,
            "to_label": self.to_label, "verdict": self.label.verdict,
            "causal_ok": self.label.causal_ok, "source": self.label.source,
            "ts": self.label.ts,
        }


# ------------------------------------------------------------ 読み込み


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    """壊れた行は飛ばして読む。1 行の書き損じで正解セット全体を失わないため。"""
    if not path.exists():
        return
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("gold: JSON として読めない行を飛ばしました %s:%d",
                           path.name, lineno)
            continue
        if isinstance(row, dict):
            yield row


def _gold_files(gold_dir: str | Path) -> list[Path]:
    """`tests/gold/*.jsonl` を名前順で。`.jsonl.example` は拡張子が違うので入らない。"""
    base = Path(gold_dir)
    return sorted(base.glob("*.jsonl")) if base.exists() else []


def _is_gap_row(row: dict[str, Any]) -> bool:
    """ギャップ判定の行か (関係判定と同じディレクトリに同居させるため)。

    ファイル名ではなく**中身**で振り分ける。`gold_2026q3.jsonl` のような
    名前で保存されても正しく読めるようにするためで、判定の語彙そのものが
    2 系統で重ならない (verdict vs decision) からこれで一意に決まる。
    """
    return "gap_id" in row or "decision" in row


def load_labels(eval_log: str | Path = EVAL_LOG,
                gold_dir: str | Path = GOLD_DIR) -> list[Label]:
    """2 系統の関係判定を統合する (裁定 O/P)。

    戻り値は照合キーで一意化済み (裁定 P の「最新が勝ち」を適用)。ts が同じ
    ときは**後から読んだほう**が勝つ = クリック評価 → gold の順なので、
    腰を据えて付けた gold が現場のクリックを上書きする。
    """
    rows: list[tuple[str, int, Label]] = []
    seq = 0

    # --- 1. クリック評価 (logs/evaluation.jsonl) ---
    for record in _iter_jsonl(Path(eval_log)):
        session = str(record.get("map_id") or "") or None
        ts = str(record.get("created_at") or "")
        verdicts = record.get("relation_verdicts") or {}
        if not isinstance(verdicts, dict):
            continue
        for edge_id, verdict in verdicts.items():
            if verdict not in RELATION_VERDICTS:
                continue
            seq += 1
            rows.append((ts, seq, Label(
                verdict=str(verdict), source="click", ts=ts,
                session=session, edge_id=str(edge_id),
                labeled_by=str(record.get("user_id") or "") or None)))

    # --- 2. gold ファイル (tests/gold/*.jsonl) ---
    for path in _gold_files(gold_dir):
        for row in _iter_jsonl(path):
            if _is_gap_row(row):
                continue
            verdict = str(row.get("verdict") or "")
            if verdict not in RELATION_VERDICTS:
                logger.warning("gold: 未知の verdict を飛ばしました %s: %r",
                               path.name, verdict)
                continue
            causal_ok = row.get("causal_ok")
            seq += 1
            rows.append((str(row.get("ts") or ""), seq, Label(
                verdict=verdict, source="gold", ts=str(row.get("ts") or ""),
                session=str(row.get("session") or "") or None,
                edge_id=str(row.get("edge_id") or "") or None,
                from_norm=normalize_label(row["from_label"]) if row.get("from_label") else None,
                to_norm=normalize_label(row["to_label"]) if row.get("to_label") else None,
                causal_ok=bool(causal_ok) if isinstance(causal_ok, bool) else None,
                labeled_by=str(row.get("labeled_by") or "") or None,
                note=str(row.get("note") or "") or None)))

    return _latest_wins(rows)


def _latest_wins(rows: list[tuple[str, int, Label]]) -> list[Label]:
    """同じ照合キーの判定を ts 順に潰して 1 件にする (裁定 P)。

    ts が無い古い行は空文字なので必ず負ける。ts が並んだときは読み込み順
    (seq) の後ろが勝つ — 追記型ログでは後の行が新しい判断だから。
    """
    winners: dict[tuple[str, str, str], Label] = {}
    for _, _, label in sorted(rows, key=lambda r: (r[0], r[1])):
        winners[label.key] = label
    return sorted(winners.values(), key=lambda x: (x.ts, x.session or "", x.edge_id or ""))


def load_gold_gaps(gold_dir: str | Path = GOLD_DIR) -> list[dict[str, Any]]:
    """gaps_gold.jsonl の判定を plan の gap と同じ形へ写す。

    `usefulness_rate` をそのまま再利用するため、confirm/dismiss を
    confirmed/dismissed の `status` に直して返す (設計 §1.2「既存の
    usefulness_rate を再利用」)。
    """
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for path in _gold_files(gold_dir):
        for row in _iter_jsonl(path):
            if not _is_gap_row(row):
                continue
            status = GAP_DECISIONS.get(str(row.get("decision")
                                           or row.get("status") or ""))
            if status is None:
                continue
            gap_id = str(row.get("gap_id") or "")
            session = str(row.get("session") or "")
            out[(session, gap_id)] = {
                "gap_id": gap_id, "session": session, "status": status,
                "source": "gold", "ts": str(row.get("ts") or ""),
                "confidence": float(row.get("confidence") or 0.0),
                "presumed_type": str(row.get("presumed_type") or "unknown"),
                "reason": str(row.get("note") or row.get("reason") or ""),
            }
    return sorted(out.values(), key=lambda g: (g["session"], g["gap_id"]))


# -------------------------------------------------------------- 突合


def _edge_rows(store: Any, sessions: list[str] | None = None) -> list[dict[str, Any]]:
    """現在の KG (fold 済み) の全エッジを平坦化する。

    `store.load_kg` が fold 済みを返すのが肝で、原本を読むと「消したはずの
    関係」に判定が当たってしまう (cc_store.files のドキュメント参照)。
    """
    names = list(sessions) if sessions is not None else store.list_sessions()
    rows: list[dict[str, Any]] = []
    for session in names:
        try:
            kg = store.load_kg(session)
        except Exception as exc:  # 壊れた/消えたセッションで全体を止めない
            logger.warning("offline_eval: KG を読めません session=%s (%s)",
                           session, type(exc).__name__)
            continue
        labels = {str(n.get("id")): str(n.get("label") or "")
                  for n in kg.get("nodes", []) if n.get("id")}
        for edge in kg.get("edges", []):
            edge_id = str(edge.get("id") or "")
            if not edge_id:
                continue
            from_label = labels.get(str(edge.get("from")), str(edge.get("from") or ""))
            to_label = labels.get(str(edge.get("to")), str(edge.get("to") or ""))
            rows.append({
                "session": session,
                "edge_id": edge_id,
                "glyph": str(edge.get("glyph") or "wave"),
                "from_label": from_label,
                "to_label": to_label,
                "from_norm": normalize_label(from_label),
                "to_norm": normalize_label(to_label),
                "label": str(edge.get("label") or ""),
                "user_origin": str(edge.get("origin") or "").startswith("user"),
            })
    return rows


def _index(rows: list[dict[str, Any]]) -> tuple[dict, dict]:
    by_edge = {(r["session"], r["edge_id"]): r for r in rows}
    by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    # 新しいセッションを先に見る (list_sessions が新しい順なので入力順のまま)
    for row in rows:
        by_pair.setdefault((row["from_norm"], row["to_norm"]), []).append(row)
    return by_edge, by_pair


def match_labels(labels: Iterable[Label], store: Any, *,
                 sessions: list[str] | None = None) -> list[Matched]:
    """判定を現在の KG (fold 済み) と突き合わせる (裁定 P)。

    edge_id が当たらなかった判定にラベル対があれば、そこへ**落ちる**。
    エッジ ID はセッションローカルで、付け替えや再生成で変わりうるのに対し
    概念ラベルは残るため。裁定 P の「edge_id 優先」はそのまま守っている
    (先に ID を見て、外れたときだけラベル対を見る)。

    最後にもう一度「解決後の同一エッジ」で最新勝ちを適用する。gold が
    ラベル対で、クリックが edge_id で同じ関係を指していることがあり、
    読み込み時の一意化では別キーとして残ってしまうため (件数は 1)。
    """
    by_edge, by_pair = _index(_edge_rows(store, sessions))
    resolved: list[Matched] = []
    for label in labels:
        row = None
        if label.session and label.edge_id:
            row = by_edge.get((label.session, label.edge_id))
        if row is None and label.from_norm and label.to_norm:
            candidates = by_pair.get((label.from_norm, label.to_norm)) or []
            if label.session:  # 同じセッションのものを優先する
                row = next((c for c in candidates if c["session"] == label.session), None)
            row = row or (candidates[0] if candidates else None)
        if row is None:
            resolved.append(Matched(
                label=label, status="missing", session=label.session,
                edge_id=label.edge_id, from_norm=label.from_norm,
                to_norm=label.to_norm))
            continue
        resolved.append(Matched(
            label=label,
            status="user_origin" if row["user_origin"] else "matched",
            session=row["session"], edge_id=row["edge_id"], glyph=row["glyph"],
            from_norm=row["from_norm"], to_norm=row["to_norm"],
            from_label=row["from_label"], to_label=row["to_label"]))

    winners: dict[tuple, Matched] = {}
    for item in sorted(resolved, key=lambda m: m.label.ts):
        ident = ((item.session, item.edge_id) if item.status != "missing"
                 else item.label.key)
        winners[ident] = item
    return sorted(winners.values(),
                  key=lambda m: (m.session or "", m.edge_id or ""))


# -------------------------------------------------------------- 指標


def _metric(numerator: int, denominator: int, target: float | None) -> dict[str, Any]:
    """{value, n, target, meets_target} の統一形。

    分母 0 のとき value は **None** であって 0.0 ではない。「まだ測れない」と
    「測ったら 0 だった」は別の情報で、0.0 にすると未着手が「目標未達」として
    ダッシュボードに赤く出てしまう。meets_target も同じ理由で None にする。
    """
    if denominator <= 0:
        return {"value": None, "n": 0, "target": target, "meets_target": None}
    value = numerator / denominator
    return {
        "value": round(value, 3),
        "n": denominator,
        "target": target,
        "meets_target": (value >= target) if target is not None else None,
    }


def offline_metrics(matched: Iterable[Matched], *,
                    total_relations: int = 0,
                    gaps: list[dict[str, Any]] | None = None,
                    gold_gaps: list[dict[str, Any]] | None = None,
                    ) -> dict[str, Any]:
    """関係正答率 / 因果精度 / ギャップ有用率 / 網羅率 (設計 §1.2)。

    分母に入るのは `status == "matched"` のものだけ = 現在の KG に生きていて
    AI 由来の関係。user origin と、既に消えた関係の判定は数えない。
    """
    items = list(matched)
    usable = [m for m in items if m.counts]

    correct = sum(1 for m in usable if m.label.verdict == "correct")
    incorrect = sum(1 for m in usable if m.label.verdict == "incorrect")
    undecidable = sum(1 for m in usable if m.label.verdict == "undecidable")

    # 因果精度: 「いま矢印で描かれている関係」への causal_ok 判定のみ
    causal = [m for m in usable
              if m.glyph == CAUSAL_GLYPH and m.label.causal_ok is not None]
    causal_ok = sum(1 for m in causal if m.label.causal_ok)

    merged_gaps = _merge_gaps(gaps or [], gold_gaps or [])
    gap_rate = usefulness_rate({"gaps": merged_gaps})

    relation_accuracy = _metric(correct, correct + incorrect,
                                RELATION_ACCURACY_TARGET)
    relation_accuracy.update(correct=correct, incorrect=incorrect,
                             undecidable=undecidable)

    causal_precision = _metric(causal_ok, len(causal), CAUSAL_PRECISION_TARGET)
    causal_precision.update(
        causal_ok=causal_ok,
        note=("causal_ok は gold ファイルにしか無い項目です "
              "(クリック評価からは推測しません)") if not causal else None)

    gap_usefulness = _metric(gap_rate["confirmed"], gap_rate["decided"],
                             GAP_USEFULNESS_TARGET)
    gap_usefulness.update(total_candidates=gap_rate["total_candidates"],
                          confirmed=gap_rate["confirmed"],
                          dismissed=gap_rate["dismissed"])

    coverage = _metric(len(usable), total_relations, None)
    coverage.update(
        judged_relations=len(usable),
        total_relations=total_relations,
        gold_relations=_progress(len(items), RELATION_GOLD_TARGET),
        gold_gaps=_progress(gap_rate["decided"], GAP_GOLD_TARGET),
    )
    # coverage の n は「全関係数」(分母) を意味させると読み違えるので判定済み件数に
    coverage["n"] = len(usable)

    return {
        "relation_accuracy": relation_accuracy,
        "causal_precision": causal_precision,
        "gap_usefulness": gap_usefulness,
        "coverage": coverage,
        "labels": {
            "total": len(items),
            "matched": len(usable),
            "user_origin": sum(1 for m in items if m.status == "user_origin"),
            "missing": sum(1 for m in items if m.status == "missing"),
            "click": sum(1 for m in items if m.label.source == "click"),
            "gold": sum(1 for m in items if m.label.source == "gold"),
        },
    }


def _progress(n: int, target: int) -> dict[str, Any]:
    """gold 進捗 n/150・n/50 (裁定 O)。同じ {value,n,target,meets_target} の形。"""
    return {
        "value": round(min(n / target, 1.0), 3) if target else None,
        "n": n, "target": target, "meets_target": n >= target,
        "remaining": max(target - n, 0),
    }


def _merge_gaps(plan_gaps: list[dict[str, Any]],
                gold_gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """plan のギャップ確定と gold の判定を重ねる (gold が勝つ)。

    同じ gap を両方で判断していたら、腰を据えて付けた gold を採る — 関係側の
    「最新が勝ち」と同じ考え方で、片方を黙って二重計上しないことが目的。
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for gap in plan_gaps:
        merged[(str(gap.get("session") or ""), str(gap.get("gap_id") or ""))] = gap
    for gap in gold_gaps:
        merged[(str(gap.get("session") or ""), str(gap.get("gap_id") or ""))] = gap
    return list(merged.values())


# ---------------------------------------------------- ラベル付けの作業キュー


def unlabeled_relations(store: Any, labels: Iterable[Label], *,
                        sessions: list[str] | None = None) -> list[dict[str, Any]]:
    """まだ誰も判定していない AI 由来の関係 (新しいセッション順)。"""
    judged: set[tuple[str, str]] = set()
    pairs: set[tuple[str, str]] = set()
    for label in labels:
        if label.session and label.edge_id:
            judged.add((label.session, label.edge_id))
        if label.from_norm and label.to_norm:
            pairs.add((label.from_norm, label.to_norm))
    rows = [r for r in _edge_rows(store, sessions)
            if not r["user_origin"]
            and (r["session"], r["edge_id"]) not in judged
            and (r["from_norm"], r["to_norm"]) not in pairs]
    return sorted(rows, key=lambda r: (r["session"], r["edge_id"]), reverse=True)


def gold_queue(store: Any, labels: Iterable[Label], k: int = 10, *,
               sessions: list[str] | None = None) -> list[dict[str, Any]]:
    """未判定の関係から k 件を **glyph 層化サンプリング**で選ぶ (設計 §1.2)。

    素直に新しい順で k 件取ると、正解セットが特定の glyph に偏る。因果の矢印
    ばかり判定した正解セットで「関係正答率」を名乗ると、相関や対立の関係が
    測られないまま数字だけが良く見えてしまう。層の大きさに比例して配り
    (最大剰余法)、層内は新しいセッション順で決定的に切る — 乱数は使わない
    ので、同じ入力なら常に同じキューが出る。
    """
    pool = unlabeled_relations(store, labels, sessions=sessions)
    return _stratify(pool, k)


def _stratify(pool: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    if k <= 0 or not pool:
        return []
    if k >= len(pool):
        return list(pool)
    strata: dict[str, list[dict[str, Any]]] = {}
    for row in pool:
        strata.setdefault(row["glyph"], []).append(row)
    names = sorted(strata)
    total = len(pool)
    quotas = {n: min(int(len(strata[n]) * k / total), len(strata[n])) for n in names}
    # 端数は剰余の大きい層 → 層が大きい順 → 名前順。残量のある層にだけ配る
    order = sorted(names, key=lambda n: (-((len(strata[n]) * k / total) % 1),
                                         -len(strata[n]), n))
    while sum(quotas.values()) < k:
        for name in order:
            if sum(quotas.values()) >= k:
                break
            if quotas[name] < len(strata[name]):
                quotas[name] += 1
    picked = [row for name in names for row in strata[name][:quotas[name]]]
    return sorted(picked, key=lambda r: (r["session"], r["edge_id"]), reverse=True)


# ---------------------------------------------------------- 組み立て


def _plan_gaps(store: Any, sessions: list[str] | None = None) -> list[dict[str, Any]]:
    """全セッションの plan からギャップ候補を集める (session を添えて返す)。"""
    names = list(sessions) if sessions is not None else store.list_sessions()
    out: list[dict[str, Any]] = []
    for session in names:
        try:
            plan = store.load_plan(session)
        except Exception:  # plan が壊れていても他セッションは測る
            continue
        for gap in (plan or {}).get("gaps", []) or []:
            if isinstance(gap, dict):
                out.append({**gap, "session": session})
    return out


def run_offline_eval(store: Any = None, *,
                     eval_log: str | Path = EVAL_LOG,
                     gold_dir: str | Path = GOLD_DIR,
                     graphs_dir: str | Path = GRAPHS_DIR,
                     queue_size: int = 5) -> dict[str, Any]:
    """CLI `--offline-eval` と Web `GET /api/evaluation/offline` の共通の中身。

    LLM 呼び出しゼロ (裁定 O)。判定が 1 件も無くても例外にせず、`empty` と
    `hint` (集め方の案内) を返す — 受け入れ基準 2。「まだ測っていない」を
    エラーにすると、使い始めの利用者が壊れていると誤解するため。
    """
    if store is None:
        from cc_store import SessionStore
        store = SessionStore(graphs_dir)

    labels = load_labels(eval_log, gold_dir)
    sessions = store.list_sessions()
    matched = match_labels(labels, store, sessions=sessions)
    rows = _edge_rows(store, sessions)
    total_relations = sum(1 for r in rows if not r["user_origin"])
    metrics = offline_metrics(
        matched, total_relations=total_relations,
        gaps=_plan_gaps(store, sessions), gold_gaps=load_gold_gaps(gold_dir))
    queue = gold_queue(store, labels, queue_size, sessions=sessions)

    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "sources": {
            "eval_log": str(eval_log),
            "gold_dir": str(gold_dir),
            "gold_files": [p.name for p in _gold_files(gold_dir)],
            "sessions": len(sessions),
            "relations": total_relations,
        },
        "metrics": metrics,
        "queue": queue,
        "next_unlabeled": queue[0] if queue else None,
        "unlabeled": max(total_relations - metrics["coverage"]["judged_relations"], 0),
        "empty": not labels,
        "hint": EMPTY_HINT if not labels else None,
    }


def save_report(report: dict[str, Any], *, out_dir: str | Path = REPORT_DIR,
                today: dt.date | None = None) -> Path:
    """`logs/offline_eval_{date}.json` に保存する (設計 §1.2)。

    日付ごとに 1 ファイルなので、同じ日に何度走らせても上書きされる。KPI は
    「その日どこまで来たか」の記録で、1 日に何十本も残す種類のものではない。
    """
    path = Path(out_dir) / f"offline_eval_{(today or dt.date.today()).isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


__all__ = [
    "CAUSAL_PRECISION_TARGET", "EMPTY_HINT", "EVAL_LOG", "GAP_GOLD_TARGET",
    "GAP_USEFULNESS_TARGET", "GOLD_DIR", "Label", "Matched",
    "RELATION_ACCURACY_TARGET", "RELATION_GOLD_TARGET",
    "gold_queue", "load_gold_gaps", "load_labels", "match_labels",
    "offline_metrics", "run_offline_eval", "save_report", "unlabeled_relations",
]
