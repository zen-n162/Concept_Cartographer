"""R2c-1 オフライン評価と日本語正解セットの回帰テスト — R2c 設計書 §3。

主眼は 5 つ:

  - **裁定 P の照合キー**。session+edge_id を優先し、無ければ正規化ラベル対。
    同じ関係への矛盾した判定は最新が勝ち、**件数は 1**
  - **分母の定義**。user origin の関係と、現在の KG にもう無い関係は数えない。
    「直せば直すほど精度が上がる」読みを作らないための既存 KPI と同じ原則
  - **分母 0 は None であって 0.0 ではない**。「まだ測れない」と「測ったら 0」
    を混同すると、使い始めが目標未達に見える
  - **判定 0 件でも落ちない** (受け入れ基準 2)。集め方の案内を返す
  - **LLM を呼ばない** (裁定 O)。正解セットを作るモデルと測られるモデルが
    同じでは KPI の意味が無い

各テストは tmp_path に graphs/ と logs/ を作るので production/ を汚さない。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cc_core import offline_eval
from cc_core.editing import append_edit
from cc_core.evaluation import EvaluationSession, EvaluationStore, summarize
from cc_core.offline_eval import (
    GAP_GOLD_TARGET,
    RELATION_GOLD_TARGET,
    Label,
    gold_queue,
    load_gold_gaps,
    load_labels,
    match_labels,
    offline_metrics,
    run_offline_eval,
    save_report,
    unlabeled_relations,
)
from cc_store import SessionStore

PRODUCTION = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------- 補助


def write_session(graphs: Path, session: str, edges: list[dict],
                  *, nodes: list[dict] | None = None,
                  gaps: list[dict] | None = None) -> None:
    """kg_session_{s}.json (+ 任意で gaps を持つ plan) を書く。

    ノードを省いたときはエッジの端点からラベル付きで自動生成する
    (照合はラベル対で行うので、ノードのラベルが要る)。
    """
    graphs.mkdir(parents=True, exist_ok=True)
    if nodes is None:
        ids = sorted({e[k] for e in edges for k in ("from", "to")})
        nodes = [{"id": i, "label": f"概念{i}"} for i in ids]
    (graphs / f"kg_session_{session}.json").write_text(json.dumps({
        "graph_version": "kg_r2c_test", "nodes": nodes, "edges": edges,
        "communities": [{"id": "comm_001", "name": "テーマ", "is_gap": False}],
    }, ensure_ascii=False), encoding="utf-8")
    if gaps is not None:
        (graphs / f"layout_plan_session_{session}.json").write_text(
            json.dumps({"nodes": [], "edges": [], "gaps": gaps},
                       ensure_ascii=False), encoding="utf-8")


def edge(eid: str, a: str, b: str, *, glyph: str = "wave",
         origin: str | None = None) -> dict:
    row = {"id": eid, "from": a, "to": b, "glyph": glyph, "label": ""}
    if origin:
        row["origin"] = origin
    return row


def click_log(path: Path, session: str, verdicts: dict[str, str],
              *, ts: str = "2026-08-07T10:00:00") -> None:
    ev = EvaluationSession(map_id=session, user_id="u1", created_at=ts)
    for edge_id, verdict in verdicts.items():
        ev.judge_relation(edge_id, verdict)
    EvaluationStore(path).append(ev)


def gold_file(gold_dir: Path, rows: list[dict], name: str = "relations_gold.jsonl") -> None:
    gold_dir.mkdir(parents=True, exist_ok=True)
    (gold_dir / name).write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")


@pytest.fixture
def bench(tmp_path):
    """graphs/ + logs/ + tests/gold/ を持つ作業ディレクトリ。"""
    graphs = tmp_path / "graphs"
    logs = tmp_path / "logs"
    gold = tmp_path / "gold"
    logs.mkdir()
    graphs.mkdir()
    return {"root": tmp_path, "graphs": graphs, "gold": gold,
            "eval_log": logs / "evaluation.jsonl"}


# ================================================= 照合 (裁定 P)


def test_edge_id_match_wins_when_session_and_id_are_present(bench) -> None:
    """session+edge_id が揃っていればそれで当てる (裁定 P の第 1 規則)。"""
    write_session(bench["graphs"], "20260807_100000",
                  [edge("r001", "c1", "c2"), edge("r002", "c2", "c3")])
    click_log(bench["eval_log"], "20260807_100000", {"r001": "correct"})

    labels = load_labels(bench["eval_log"], bench["gold"])
    matched = match_labels(labels, SessionStore(bench["graphs"]))
    assert len(matched) == 1
    assert matched[0].status == "matched"
    assert (matched[0].session, matched[0].edge_id) == ("20260807_100000", "r001")


def test_label_pair_match_when_no_edge_id(bench) -> None:
    """edge_id が無い gold は正規化ラベル対で当たる (NFKC + casefold)。"""
    write_session(bench["graphs"], "20260807_100000", [edge("r001", "c1", "c2")],
                  nodes=[{"id": "c1", "label": "被ばく線量"},
                         {"id": "c2", "label": "細胞損傷"}])
    # 全角/半角・大小の揺れがあっても normalize_label で吸収される
    gold_file(bench["gold"], [{"from_label": " 被ばく線量 ", "to_label": "細胞損傷",
                               "verdict": "correct", "ts": "2026-08-07T11:00:00"}])

    matched = match_labels(load_labels(bench["eval_log"], bench["gold"]),
                           SessionStore(bench["graphs"]))
    assert len(matched) == 1
    assert matched[0].status == "matched"
    assert matched[0].edge_id == "r001"


def test_latest_label_wins_and_counts_as_one(bench) -> None:
    """同じ関係への矛盾した判定は最新が勝ち、**件数は 1** (裁定 P)。"""
    write_session(bench["graphs"], "20260807_100000", [edge("r001", "c1", "c2")])
    click_log(bench["eval_log"], "20260807_100000", {"r001": "correct"},
              ts="2026-08-07T10:00:00")
    click_log(bench["eval_log"], "20260807_100000", {"r001": "incorrect"},
              ts="2026-08-07T12:00:00")

    labels = load_labels(bench["eval_log"], bench["gold"])
    assert len(labels) == 1
    assert labels[0].verdict == "incorrect"

    metrics = offline_metrics(match_labels(labels, SessionStore(bench["graphs"])))
    assert metrics["relation_accuracy"]["n"] == 1
    assert metrics["relation_accuracy"]["value"] == 0.0


def test_gold_and_click_on_same_edge_are_deduped_after_resolution(bench) -> None:
    """ラベル対の gold と edge_id のクリックが同じ関係を指したら 1 件に潰れる。

    読み込み時点ではキーが違う (pair と edge) ので残る。突合で同じエッジへ
    解決された後にもう一度最新勝ちを掛けるのが `match_labels` の役目。
    """
    write_session(bench["graphs"], "20260807_100000", [edge("r001", "c1", "c2")],
                  nodes=[{"id": "c1", "label": "線量"}, {"id": "c2", "label": "損傷"}])
    click_log(bench["eval_log"], "20260807_100000", {"r001": "correct"},
              ts="2026-08-07T10:00:00")
    gold_file(bench["gold"], [{"from_label": "線量", "to_label": "損傷",
                               "verdict": "incorrect", "ts": "2026-08-07T13:00:00"}])

    labels = load_labels(bench["eval_log"], bench["gold"])
    assert len(labels) == 2                      # 読み込み時点では別キー
    matched = match_labels(labels, SessionStore(bench["graphs"]))
    assert len(matched) == 1                     # 解決後に 1 件へ
    assert matched[0].label.verdict == "incorrect"   # 新しい gold が勝つ


def test_user_origin_edges_are_excluded_from_denominator(bench) -> None:
    """ユーザーが足した/直した関係は分母に入らない (既存 KPI と同じ原則)。"""
    write_session(bench["graphs"], "20260807_100000", [
        edge("r001", "c1", "c2"),
        edge("r002", "c2", "c3", origin="user_added"),
    ])
    click_log(bench["eval_log"], "20260807_100000",
              {"r001": "correct", "r002": "correct"})

    matched = match_labels(load_labels(bench["eval_log"], bench["gold"]),
                           SessionStore(bench["graphs"]))
    assert {m.status for m in matched} == {"matched", "user_origin"}
    metrics = offline_metrics(matched)
    assert metrics["relation_accuracy"]["n"] == 1
    assert metrics["labels"]["user_origin"] == 1


def test_labels_for_deleted_edges_are_missing_not_counted(bench) -> None:
    """消した関係への古い判定は `missing` で、分母に入らない。

    `store.load_kg` が fold 済みを返すことに依存している — 原本を読むと
    「消したはずの関係」に判定が当たり続ける。
    """
    write_session(bench["graphs"], "20260807_100000",
                  [edge("r001", "c1", "c2"), edge("r002", "c2", "c3")])
    click_log(bench["eval_log"], "20260807_100000",
              {"r001": "correct", "r002": "correct"})
    append_edit("20260807_100000", {"op": "delete_edge", "target": "r002"},
                graphs_dir=bench["graphs"], eval_log=None)

    matched = match_labels(load_labels(bench["eval_log"], bench["gold"]),
                           SessionStore(bench["graphs"]))
    metrics = offline_metrics(matched)
    assert metrics["labels"]["missing"] == 1
    assert metrics["relation_accuracy"]["n"] == 1


# ================================================= 指標の算術


def test_relation_accuracy_arithmetic_ignores_undecidable(bench) -> None:
    """判断不能は分子にも分母にも入らない (既存 relation_error_rate と同じ)。"""
    write_session(bench["graphs"], "20260807_100000",
                  [edge(f"r{i:03d}", "c1", f"c{i}") for i in range(1, 5)])
    click_log(bench["eval_log"], "20260807_100000", {
        "r001": "correct", "r002": "correct", "r003": "incorrect",
        "r004": "undecidable"})

    metrics = offline_metrics(match_labels(
        load_labels(bench["eval_log"], bench["gold"]), SessionStore(bench["graphs"])))
    acc = metrics["relation_accuracy"]
    assert (acc["correct"], acc["incorrect"], acc["undecidable"]) == (2, 1, 1)
    assert acc["n"] == 3
    assert acc["value"] == 0.667                     # 2/3 を 3 桁で丸める
    assert acc["target"] == 0.70 and acc["meets_target"] is False


def test_empty_denominator_is_none_not_zero() -> None:
    """分母 0 は None。0.0 にすると「未着手」が「目標未達」に見える。"""
    metrics = offline_metrics([])
    for name in ("relation_accuracy", "causal_precision", "gap_usefulness"):
        assert metrics[name]["value"] is None, name
        assert metrics[name]["meets_target"] is None, name
        assert metrics[name]["n"] == 0, name


def test_causal_precision_counts_only_arrow_edges_with_causal_ok(bench) -> None:
    """因果精度の分母は「矢印で描かれていて causal_ok の判定がある関係」だけ。

    verdict からは推測しない — 「関係はあるか」と「矢印でよいか」は別の問い。
    """
    write_session(bench["graphs"], "20260807_100000", [
        edge("r001", "c1", "c2", glyph="arrow"),
        edge("r002", "c2", "c3", glyph="arrow"),
        edge("r003", "c3", "c4", glyph="wave"),
    ])
    gold_file(bench["gold"], [
        {"session": "20260807_100000", "edge_id": "r001", "verdict": "correct",
         "causal_ok": True, "ts": "2026-08-07T10:00:00"},
        {"session": "20260807_100000", "edge_id": "r002", "verdict": "correct",
         "causal_ok": False, "ts": "2026-08-07T10:01:00"},
        # wave の判定と、causal_ok の無い判定は分母に入らない
        {"session": "20260807_100000", "edge_id": "r003", "verdict": "correct",
         "causal_ok": True, "ts": "2026-08-07T10:02:00"},
    ])
    click_log(bench["eval_log"], "20260807_100000", {"r001": "correct"})

    metrics = offline_metrics(match_labels(
        load_labels(bench["eval_log"], bench["gold"]), SessionStore(bench["graphs"])))
    causal = metrics["causal_precision"]
    assert causal["n"] == 2 and causal["causal_ok"] == 1
    assert causal["value"] == 0.5


def test_causal_precision_notes_that_clicks_cannot_provide_causal_ok(bench) -> None:
    """クリックだけのときは「gold にしか無い項目」だと明示する。"""
    write_session(bench["graphs"], "20260807_100000",
                  [edge("r001", "c1", "c2", glyph="arrow")])
    click_log(bench["eval_log"], "20260807_100000", {"r001": "correct"})
    metrics = offline_metrics(match_labels(
        load_labels(bench["eval_log"], bench["gold"]), SessionStore(bench["graphs"])))
    assert metrics["causal_precision"]["value"] is None
    assert "gold" in metrics["causal_precision"]["note"]


def test_coverage_and_gold_progress(bench) -> None:
    """網羅率 = 判定済み / 全関係。gold 進捗は n/150・n/50 (裁定 O)。"""
    write_session(bench["graphs"], "20260807_100000",
                  [edge(f"r{i:03d}", "c1", f"c{i}") for i in range(1, 11)])
    click_log(bench["eval_log"], "20260807_100000",
              {"r001": "correct", "r002": "incorrect"})

    report = run_offline_eval(SessionStore(bench["graphs"]),
                              eval_log=bench["eval_log"], gold_dir=bench["gold"])
    cov = report["metrics"]["coverage"]
    assert cov["total_relations"] == 10
    assert cov["judged_relations"] == 2
    assert cov["value"] == 0.2
    assert cov["target"] is None and cov["meets_target"] is None
    assert cov["gold_relations"] == {
        "value": round(2 / RELATION_GOLD_TARGET, 3), "n": 2,
        "target": RELATION_GOLD_TARGET, "meets_target": False,
        "remaining": RELATION_GOLD_TARGET - 2}
    assert cov["gold_gaps"]["target"] == GAP_GOLD_TARGET
    assert report["unlabeled"] == 8


def test_gap_usefulness_reuses_usefulness_rate_and_gold_overrides_plan(bench) -> None:
    """ギャップ有用率は既存 usefulness_rate をそのまま使い、gold が plan に勝つ。"""
    write_session(bench["graphs"], "20260807_100000", [edge("r001", "c1", "c2")],
                  gaps=[{"gap_id": "g1", "status": "dismissed"},
                        {"gap_id": "g2", "status": "confirmed"},
                        {"gap_id": "g3", "status": "candidate"}])
    gold_file(bench["gold"], [
        # plan では dismissed だった g1 を、腰を据えて confirm と判定し直す
        {"gap_id": "g1", "session": "20260807_100000", "decision": "confirm",
         "ts": "2026-08-07T14:00:00"}], name="gaps_gold.jsonl")

    assert len(load_gold_gaps(bench["gold"])) == 1
    report = run_offline_eval(SessionStore(bench["graphs"]),
                              eval_log=bench["eval_log"], gold_dir=bench["gold"])
    gap = report["metrics"]["gap_usefulness"]
    assert gap["n"] == 2                     # candidate は分母に入らない
    assert gap["confirmed"] == 2 and gap["dismissed"] == 0
    assert gap["value"] == 1.0
    assert report["metrics"]["coverage"]["gold_gaps"]["n"] == 2


# ================================================= gold ファイル


def test_gold_file_round_trip_and_field_mapping(bench) -> None:
    """gold の 1 行が Label のどの欄になるかを固定する (設計 §1.1 の形)。"""
    gold_file(bench["gold"], [{
        "from_label": "被ばく線量", "to_label": "細胞損傷", "verdict": "correct",
        "causal_ok": True, "session": "20260807_143804", "edge_id": "r002",
        "labeled_by": "nakamura.zen@qst.go.jp", "ts": "2026-08-07T15:00:00",
        "note": "機序の記述あり"}])
    label = load_labels(bench["eval_log"], bench["gold"])[0]
    assert label.source == "gold"
    assert (label.verdict, label.causal_ok) == ("correct", True)
    assert (label.session, label.edge_id) == ("20260807_143804", "r002")
    assert (label.from_norm, label.to_norm) == ("被ばく線量", "細胞損傷")
    assert label.key == ("edge", "20260807_143804", "r002")


def test_broken_gold_lines_are_skipped_not_fatal(bench) -> None:
    """1 行の書き損じで正解セット全体を失わない。"""
    bench["gold"].mkdir(parents=True, exist_ok=True)
    (bench["gold"] / "relations_gold.jsonl").write_text(
        '{"from_label": "A", "to_label": "B", "verdict": "correct"}\n'
        "これは JSON ではない\n"
        "\n"
        '{"from_label": "C", "to_label": "D", "verdict": "bogus_verdict"}\n'
        '{"from_label": "E", "to_label": "F", "verdict": "incorrect"}\n',
        encoding="utf-8")
    labels = load_labels(bench["eval_log"], bench["gold"])
    assert [l.verdict for l in labels] == ["correct", "incorrect"]


def test_example_files_are_not_loaded_as_gold(bench) -> None:
    """`*.jsonl.example` は拡張子が違うので正解セットに混ざらない。"""
    bench["gold"].mkdir(parents=True, exist_ok=True)
    (bench["gold"] / "relations_gold.jsonl.example").write_text(
        '{"from_label": "A", "to_label": "B", "verdict": "correct"}\n',
        encoding="utf-8")
    assert load_labels(bench["eval_log"], bench["gold"]) == []


def test_shipped_examples_parse_with_the_real_loader(tmp_path) -> None:
    """同梱の .example が実際に読める形であることを保証する。

    README に書いた形と loader が食い違うと、案内どおり書いた人が
    黙って 0 件になる。拡張子を外して本物の loader に通す。
    """
    src = PRODUCTION / "tests" / "gold"
    gold = tmp_path / "gold"
    gold.mkdir()
    for name in ("relations_gold.jsonl.example", "gaps_gold.jsonl.example"):
        (gold / name[: -len(".example")]).write_text(
            (src / name).read_text(encoding="utf-8"), encoding="utf-8")
    labels = load_labels(tmp_path / "missing.jsonl", gold)
    assert len(labels) == 3
    assert {l.causal_ok for l in labels} == {True, False}
    gaps = load_gold_gaps(gold)
    assert {g["status"] for g in gaps} == {"confirmed", "dismissed"}


# ================================================= キュー (層化)


def test_gold_queue_is_glyph_stratified_and_deterministic(bench) -> None:
    """未判定の関係を glyph の構成比に沿って選ぶ (乱数を使わない)。

    素直に新しい順で取ると正解セットが 1 つの glyph に偏り、他の関係型が
    測られないまま数字だけ良く見える。
    """
    edges = ([edge(f"a{i:03d}", "c1", f"n{i}", glyph="arrow") for i in range(60)]
             + [edge(f"w{i:03d}", "c1", f"m{i}", glyph="wave") for i in range(30)]
             + [edge(f"d{i:03d}", "c1", f"k{i}", glyph="double") for i in range(10)])
    write_session(bench["graphs"], "20260807_100000", edges)
    store = SessionStore(bench["graphs"])

    picked = gold_queue(store, [], 10)
    assert len(picked) == 10
    mix: dict[str, int] = {}
    for row in picked:
        mix[row["glyph"]] = mix.get(row["glyph"], 0) + 1
    assert mix == {"arrow": 6, "wave": 3, "double": 1}   # 60/30/10 の構成比
    assert gold_queue(store, [], 10) == picked           # 決定的


def test_gold_queue_skips_already_judged_and_user_edges(bench) -> None:
    write_session(bench["graphs"], "20260807_100000", [
        edge("r001", "c1", "c2"),
        edge("r002", "c2", "c3"),
        edge("r003", "c3", "c4", origin="user_added"),
    ])
    click_log(bench["eval_log"], "20260807_100000", {"r001": "correct"})
    store = SessionStore(bench["graphs"])
    labels = load_labels(bench["eval_log"], bench["gold"])

    remaining = unlabeled_relations(store, labels)
    assert [r["edge_id"] for r in remaining] == ["r002"]
    assert [r["edge_id"] for r in gold_queue(store, labels, 5)] == ["r002"]


def test_gold_queue_returns_everything_when_k_exceeds_pool(bench) -> None:
    write_session(bench["graphs"], "20260807_100000",
                  [edge("r001", "c1", "c2"), edge("r002", "c2", "c3")])
    assert len(gold_queue(SessionStore(bench["graphs"]), [], 99)) == 2
    assert gold_queue(SessionStore(bench["graphs"]), [], 0) == []


# ================================================= 空の状態 (受け入れ基準 2)


def test_zero_labels_does_not_crash_and_explains_how_to_collect(bench) -> None:
    """判定 0 件でも例外にせず「まだ判定がありません + 集め方」を返す。"""
    write_session(bench["graphs"], "20260807_100000", [edge("r001", "c1", "c2")])
    report = run_offline_eval(SessionStore(bench["graphs"]),
                              eval_log=bench["eval_log"], gold_dir=bench["gold"])
    assert report["empty"] is True
    assert "まだ関係の判定がありません" in report["hint"]
    assert "relations_gold.jsonl" in report["hint"]        # 集め方が書いてある
    assert report["metrics"]["relation_accuracy"]["value"] is None
    assert report["next_unlabeled"]["edge_id"] == "r001"   # 次の一手は出る


def test_labels_that_all_went_stale_are_reported_not_silently_zero(bench) -> None:
    """判定はあるのに 1 件も突合しない状態を、値が全部 None のまま放置しない。

    `empty` は false (判定はある) なので案内文は出ないが、`labels` を見れば
    「なぜ分母が 0 なのか」が分かる。CLI/Web はここを読んで理由を出す。
    """
    write_session(bench["graphs"], "20260807_100000", [edge("r001", "c1", "c2")])
    # 存在しないセッションへの判定 (古い地図・別環境からのログ)
    click_log(bench["eval_log"], "m1", {"r001": "correct", "r002": "incorrect"})

    report = run_offline_eval(SessionStore(bench["graphs"]),
                              eval_log=bench["eval_log"], gold_dir=bench["gold"])
    assert report["empty"] is False
    assert report["metrics"]["labels"] == {
        "total": 2, "matched": 0, "user_origin": 0, "missing": 2,
        "click": 2, "gold": 0}
    assert report["metrics"]["relation_accuracy"]["value"] is None


def test_cli_warns_when_every_label_is_stale(tmp_path) -> None:
    """CLI が「1 件も一致しません」と言う (全部 — の理由を示す)。"""
    write_session(tmp_path / "graphs", "20260807_100000", [edge("r001", "c1", "c2")])
    (tmp_path / "logs").mkdir()
    click_log(tmp_path / "logs" / "evaluation.jsonl", "m1", {"r001": "correct"})
    out = _cli(["--offline-eval"], tmp_path)
    assert "1 件も一致しません" in out


def test_no_sessions_at_all_is_not_an_error(bench) -> None:
    """地図が 1 枚も無い状態でも動く (使い始めの一番最初)。"""
    report = run_offline_eval(SessionStore(bench["graphs"]),
                              eval_log=bench["eval_log"], gold_dir=bench["gold"])
    assert report["empty"] is True
    assert report["sources"]["sessions"] == 0
    assert report["next_unlabeled"] is None
    assert report["metrics"]["coverage"]["value"] is None


def test_save_report_writes_dated_json(bench) -> None:
    import datetime as dt

    report = run_offline_eval(SessionStore(bench["graphs"]),
                              eval_log=bench["eval_log"], gold_dir=bench["gold"])
    out = save_report(report, out_dir=bench["root"] / "logs",
                      today=dt.date(2026, 8, 7))
    assert out.name == "offline_eval_2026-08-07.json"
    assert json.loads(out.read_text(encoding="utf-8"))["empty"] is True


# ================================================= 既存 KPI との共存


def test_offline_metrics_do_not_disturb_online_summarize(bench) -> None:
    """既存の R1 KPI (evaluation.summarize) と衝突しない (設計 §3)。

    オンライン側は「この地図 1 枚」、オフライン側は「コーパス累積」で
    数え方が違う。片方を動かしてももう片方の値は変わらない。
    """
    write_session(bench["graphs"], "20260807_100000",
                  [edge("r001", "c1", "c2"), edge("r002", "c2", "c3")])
    click_log(bench["eval_log"], "20260807_100000",
              {"r001": "correct", "r002": "incorrect"})
    sessions = EvaluationStore(bench["eval_log"]).load()
    plan = {"edges": [{"id": "r001", "evidence_span": "x"}], "gaps": []}

    before = summarize(plan, sessions)
    report = run_offline_eval(SessionStore(bench["graphs"]),
                              eval_log=bench["eval_log"], gold_dir=bench["gold"])
    after = summarize(plan, sessions)

    assert before == after                                   # 副作用が無い
    assert before["relation_error"]["error_rate"] == 0.5     # オンラインは誤り率
    assert report["metrics"]["relation_accuracy"]["value"] == 0.5  # こちらは正答率


def test_no_llm_client_is_ever_constructed(bench, monkeypatch) -> None:
    """裁定 O: オフライン評価は LLM を 1 回も呼ばない。"""
    import cc_orchestrator.foundry_v2 as foundry

    def boom(*args, **kwargs):
        raise AssertionError("オフライン評価が LLM クライアントを作った")

    monkeypatch.setattr(foundry.FoundryAgentsV2, "__init__", boom)
    write_session(bench["graphs"], "20260807_100000", [edge("r001", "c1", "c2")])
    click_log(bench["eval_log"], "20260807_100000", {"r001": "correct"})
    run_offline_eval(SessionStore(bench["graphs"]),
                     eval_log=bench["eval_log"], gold_dir=bench["gold"])


# ================================================= Web API


def test_web_offline_evaluation_endpoint(tmp_path, monkeypatch) -> None:
    """GET /api/evaluation/offline が CLI と同じ JSON を返す。"""
    from fastapi.testclient import TestClient

    from cc_web.app import create_app

    graphs = tmp_path / "graphs"
    write_session(graphs, "20260807_100000",
                  [edge("r001", "c1", "c2"), edge("r002", "c2", "c3")])
    (tmp_path / "logs").mkdir()
    click_log(tmp_path / "logs" / "evaluation.jsonl", "20260807_100000",
              {"r001": "correct"})
    monkeypatch.chdir(tmp_path)

    with TestClient(create_app()) as client:
        res = client.get("/api/evaluation/offline")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["empty"] is False
    assert body["metrics"]["relation_accuracy"]["value"] == 1.0
    assert body["metrics"]["coverage"]["total_relations"] == 2
    assert body["next_unlabeled"]["edge_id"] == "r002"
    # 画面が使う欄が揃っている (指標 4 + 進捗 2 + 次の 1 手)
    for name in ("relation_accuracy", "causal_precision", "gap_usefulness",
                 "coverage"):
        assert set(body["metrics"][name]) >= {"value", "n", "target", "meets_target"}


def test_web_offline_evaluation_is_200_with_no_data(tmp_path, monkeypatch) -> None:
    """判定 0 件は 404/500 ではなく 200 + hint (受け入れ基準 2)。"""
    from fastapi.testclient import TestClient

    from cc_web.app import create_app

    (tmp_path / "graphs").mkdir()
    monkeypatch.chdir(tmp_path)
    with TestClient(create_app()) as client:
        res = client.get("/api/evaluation/offline")
    assert res.status_code == 200
    assert res.json()["empty"] is True
    assert res.json()["hint"]


# ================================================= CLI


def _cli(args: list[str], cwd: Path) -> str:
    env = {"PYTHONPATH": str(PRODUCTION / "src"), "PATH": "/usr/bin:/bin"}
    res = subprocess.run([sys.executable, "-m", "cc_orchestrator.chat", *args],
                         cwd=cwd, env=env, capture_output=True, text=True,
                         timeout=120)
    assert res.returncode == 0, res.stderr
    return res.stdout


def test_cli_offline_eval_gold_status_and_queue(tmp_path) -> None:
    """3 つの CLI フラグが実際に走り、JSON を保存する (LLM 不要)。"""
    graphs = tmp_path / "graphs"
    write_session(graphs, "20260807_100000", [
        edge("r001", "c1", "c2", glyph="arrow"),
        edge("r002", "c2", "c3", glyph="wave"),
        edge("r003", "c3", "c4", glyph="wave"),
    ])
    (tmp_path / "logs").mkdir()
    click_log(tmp_path / "logs" / "evaluation.jsonl", "20260807_100000",
              {"r001": "correct"})

    out = _cli(["--offline-eval"], tmp_path)
    assert "オフライン評価" in out and "関係正答率" in out
    saved = list((tmp_path / "logs").glob("offline_eval_*.json"))
    assert len(saved) == 1
    assert json.loads(saved[0].read_text(encoding="utf-8"))["empty"] is False

    status = _cli(["--gold-status"], tmp_path)
    assert f"/ {RELATION_GOLD_TARGET}" in status and "あと" in status

    queue = _cli(["--gold-queue", "2"], tmp_path)
    assert "ラベル付けキュー 2 件" in queue
    assert "r001" not in queue          # 判定済みは出ない


def test_cli_offline_eval_with_no_labels_explains_collection(tmp_path) -> None:
    """判定 0 件の CLI 出力に「集め方」が出る (受け入れ基準 2)。"""
    (tmp_path / "graphs").mkdir()
    out = _cli(["--offline-eval"], tmp_path)
    assert "まだ判定がありません" in out
    assert "relations_gold.jsonl" in out
