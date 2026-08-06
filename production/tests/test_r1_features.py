"""R1 の残り機能の回帰テスト: 因果検証・ギャップ確定・Routing・評価・SVG。

いずれも実運用計画の裁定に対応する:
  裁定 7  因果は 3 点セット通過時のみ / 矛盾は R1 では非断定
  裁定 8  ギャップは 4 点メタデータ + confirm/dismiss
  §6      Query Routing 3 経路
  §9      オンライン評価 2 系統ラベル + KPI
  §8      ヘッドレス SVG 出力
"""

from __future__ import annotations

import json

import pytest

from cc_core.causal import (
    apply_relation_policy,
    find_causal_cues,
    has_only_correlation_language,
    validate_causal_edge,
)
from cc_core.detail import build_multilevel_plan, project
from cc_core.evaluation import (
    EvaluationSession,
    EvaluationStore,
    causal_precision_log,
    evidence_display_rate,
    relation_error_rate,
    satisfaction_rate,
)
from cc_core.gaps import (
    GapDecisionError,
    apply_decision,
    detect_gaps,
    usefulness_rate,
)
from cc_core.svg_export import build_svg
from cc_orchestrator.routing import route


def edge(label: str, surface: str, glyph: str = "arrow") -> dict:
    return {"id": "r001", "from": "a", "to": "b", "label": label, "glyph": glyph,
            "evidence_span": [{"document_id": "d1", "char_start": 0,
                               "char_end": len(surface), "surface": surface}]}


# ------------------------------------------------------- 因果 (裁定 7)

def test_mechanism_language_is_causal_evidence() -> None:
    hits = find_causal_cues("酸素欠損は機序を介して転移温度を低下させる")
    assert any(h.startswith("mechanism") for h in hits)


def test_intervention_and_counterfactual_are_detected() -> None:
    assert any(h.startswith("intervention")
               for h in find_causal_cues("阻害すると発現が止まった"))
    assert any(h.startswith("counterfactual")
               for h in find_causal_cues("介入がなければ生じなかった"))


def test_correlation_only_is_not_causal_evidence() -> None:
    assert find_causal_cues("δ と Tc は相関している") == []
    assert has_only_correlation_language("δ と Tc は相関している")


def test_causal_edge_without_evidence_is_demoted() -> None:
    check = validate_causal_edge({"id": "r1", "from": "a", "to": "b",
                                  "label": "因果", "glyph": "arrow"})
    assert not check.passed
    assert check.demoted_from == "arrow"


def test_causal_edge_with_lexicon_passes_when_no_verifier() -> None:
    check = validate_causal_edge(edge("因果", "介入により X を操作すると Y が変化した"))
    assert check.passed
    assert check.verifier_verdict == "skipped"


def test_verifier_can_reject_causal_edge() -> None:
    check = validate_causal_edge(
        edge("因果", "介入により X を操作すると Y が変化した"),
        verifier=lambda e, t: False)
    assert not check.passed
    assert check.verifier_verdict == "fail"


def test_verifier_failure_is_safe_side() -> None:
    """検証器が例外を投げたら因果を通さない (安全側に倒す)。"""
    def boom(e, t):
        raise RuntimeError("verifier down")
    check = validate_causal_edge(edge("因果", "機序を介して"), verifier=boom)
    assert not check.passed


def test_require_verifier_blocks_when_absent() -> None:
    check = validate_causal_edge(edge("因果", "機序を介して"),
                                 verifier=None, require_verifier=True)
    assert not check.passed


def test_policy_demotes_causal_and_contradiction() -> None:
    kg = {"graph_version": "kg", "nodes": [{"id": "a", "label": "A"},
                                           {"id": "b", "label": "B"}],
          "edges": [
              {"id": "r1", "from": "a", "to": "b", "label": "因果", "glyph": "arrow",
               "evidence_span": [{"document_id": "d", "char_start": 0,
                                  "char_end": 5, "surface": "相関している"}]},
              {"id": "r2", "from": "a", "to": "b", "label": "矛盾", "glyph": "zigzag"},
          ], "communities": []}
    out, stats = apply_relation_policy(kg)
    assert stats["causal_demoted"] == 1
    assert stats["contradiction_demoted"] == 1
    assert out["edges"][0]["glyph"] == "wave"       # 因果 -> 相関
    assert out["edges"][1]["glyph"] == "tension"    # 矛盾 -> 非断定


def test_policy_can_enable_contradiction_for_r2() -> None:
    kg = {"graph_version": "kg", "nodes": [{"id": "a", "label": "A"},
                                           {"id": "b", "label": "B"}],
          "edges": [{"id": "r2", "from": "a", "to": "b", "label": "矛盾",
                     "glyph": "zigzag"}], "communities": []}
    out, stats = apply_relation_policy(kg, enable_contradiction=True)
    assert out["edges"][0]["glyph"] == "zigzag"
    assert stats["contradiction_demoted"] == 0


def test_policy_does_not_mutate_input() -> None:
    kg = {"graph_version": "kg", "nodes": [{"id": "a", "label": "A"},
                                           {"id": "b", "label": "B"}],
          "edges": [{"id": "r1", "from": "a", "to": "b", "label": "因果",
                     "glyph": "arrow"}], "communities": []}
    apply_relation_policy(kg)
    assert kg["edges"][0]["glyph"] == "arrow"


# ------------------------------------------------------- ギャップ (裁定 8)

def gap_kg() -> dict:
    return {
        "graph_version": "kg",
        "nodes": [{"id": f"c{i}", "label": f"概念{i}",
                   "community_id": "m1" if i < 3 else "m2"} for i in range(6)]
                 + [{"id": "iso", "label": "孤立概念", "community_id": "m3"}],
        "edges": [{"id": "r1", "from": "c0", "to": "c1", "label": "関連", "glyph": "wave"},
                  {"id": "r2", "from": "c1", "to": "c2", "label": "関連", "glyph": "wave"},
                  {"id": "r3", "from": "c3", "to": "c4", "label": "関連", "glyph": "wave"},
                  {"id": "r4", "from": "c4", "to": "c5", "label": "関連", "glyph": "wave"}],
        "communities": [{"id": "m1", "name": "テーマ1"}, {"id": "m2", "name": "テーマ2"},
                        {"id": "m3", "name": "未検証", "is_gap": True}],
    }


def test_gap_has_four_metadata_fields() -> None:
    """v3 §4.6 の 4 点メタデータが必ず揃うこと。"""
    for g in detect_gaps(gap_kg()):
        d = g.to_dict()
        assert 0.0 <= d["confidence"] <= 1.0     # ①信頼度
        assert d["presumed_type"] in ("data", "extraction", "true", "unknown")  # ②分類
        assert d["reason"]                        # ③提示理由
        assert "evidence_links" in d              # ④出典リンク


def test_isolated_node_becomes_gap() -> None:
    gaps = detect_gaps(gap_kg())
    assert any(g.gap_id == "gap-isolated-iso" for g in gaps)


def test_disconnected_communities_become_bridge_gap() -> None:
    gaps = detect_gaps(gap_kg())
    assert any(g.gap_id.startswith("gap-bridge-") for g in gaps)


def test_declared_gap_community_is_reported() -> None:
    gaps = detect_gaps(gap_kg())
    assert any(g.gap_id.startswith("gap-declared-") for g in gaps)


def test_gaps_start_as_candidates() -> None:
    """ギャップは必ず候補から始まる (確定は人間, v4核§8)。"""
    assert all(g.status == "candidate" for g in detect_gaps(gap_kg()))


def test_confirm_and_dismiss_record_who_and_when() -> None:
    plan = {"nodes": [], "gaps": [g.to_dict() for g in detect_gaps(gap_kg())]}
    gid = plan["gaps"][0]["gap_id"]
    g = apply_decision(plan, gid, "confirm", user_id="zen")
    assert g["status"] == "confirmed"
    assert g["confirmed_by"] == "zen" and g["confirmed_at"]


def test_double_decision_is_rejected() -> None:
    plan = {"nodes": [], "gaps": [g.to_dict() for g in detect_gaps(gap_kg())]}
    gid = plan["gaps"][0]["gap_id"]
    apply_decision(plan, gid, "confirm", user_id="zen")
    with pytest.raises(GapDecisionError):
        apply_decision(plan, gid, "dismiss", user_id="zen")


def test_unknown_gap_id_is_rejected() -> None:
    with pytest.raises(GapDecisionError):
        apply_decision({"gaps": []}, "nope", "confirm", user_id="zen")


def test_usefulness_rate_denominator_is_decided_only() -> None:
    """未確定を分母に入れない (計画 §9)。"""
    plan = {"nodes": [], "gaps": [g.to_dict() for g in detect_gaps(gap_kg())]}
    ids = [g["gap_id"] for g in plan["gaps"]]
    apply_decision(plan, ids[0], "confirm", user_id="z")
    apply_decision(plan, ids[1], "dismiss", user_id="z")
    rate = usefulness_rate(plan)
    assert rate["decided"] == 2
    assert rate["usefulness_rate"] == 0.5
    assert rate["total_candidates"] > rate["decided"]


def test_usefulness_rate_is_none_without_decisions() -> None:
    plan = {"nodes": [], "gaps": [g.to_dict() for g in detect_gaps(gap_kg())]}
    assert usefulness_rate(plan)["usefulness_rate"] is None


# --------------------------------------------------------- Routing (§6)

@pytest.mark.parametrize("message,expected", [
    ("今週の研究を概念地図として整理して", "map"),
    ("先月の成果を図にして", "map"),
    ("NV中心とは何ですか", "vector"),
    ("実験は何件ありましたか", "vector"),
    ("こんにちは", "basic"),
    ("ありがとう", "basic"),
])
def test_routing_picks_expected_route(message: str, expected: str) -> None:
    assert route(message).route == expected


def test_routing_reads_detail_level() -> None:
    assert route("今月の研究をざっくり全体像で").detail_level == "overview"
    assert route("今月の研究を詳しく地図にして").detail_level == "detailed"
    assert route("今週の研究を概念地図に").detail_level is None  # 無指定


def test_routing_reads_language_and_tags() -> None:
    d = route("今週の研究を英語で地図にして #量子センサ #NV中心")
    assert d.language == "en"
    assert d.tags == ["NV中心", "量子センサ"]


def test_routing_falls_back_to_classifier() -> None:
    called = {}

    def classifier(msg: str) -> str:
        called["msg"] = msg
        return "vector"

    d = route("ふわっとした要求", classifier=classifier)
    assert d.route == "vector" and d.used_llm and called


def test_routing_survives_classifier_error() -> None:
    def boom(msg: str) -> str:
        raise RuntimeError("down")
    assert route("ふわっとした要求", classifier=boom).route == "map"


# ------------------------------------------------------------ 評価 (§9)

def test_relation_verdicts_use_v3_labels() -> None:
    s = EvaluationSession(map_id="m", user_id="u")
    s.judge_relation("r1", "correct")
    s.judge_relation("r2", "incorrect")
    s.judge_relation("r3", "undecidable")
    with pytest.raises(ValueError):
        s.judge_relation("r4", "useful")  # ギャップ側のラベルは使えない


def test_error_rate_excludes_undecidable() -> None:
    s = EvaluationSession(map_id="m", user_id="u")
    for i in range(7):
        s.judge_relation(f"r{i}", "correct")
    s.judge_relation("r7", "incorrect")
    s.judge_relation("r8", "undecidable")
    stats = relation_error_rate([s.to_dict()])
    assert stats["judged"] == 8
    assert stats["error_rate"] == 0.125
    assert stats["meets_target"]


def test_satisfaction_rate_target() -> None:
    sessions = []
    for score in (5, 4, 4, 2):
        s = EvaluationSession(map_id="m", user_id="u")
        s.rate(score)
        sessions.append(s.to_dict())
    stats = satisfaction_rate(sessions)
    assert stats["high_rate"] == 0.75 and stats["meets_target"]


def test_operation_log_rejects_long_payload() -> None:
    """本文が操作ログへ混入しないこと (サニタイズ方針)。"""
    s = EvaluationSession(map_id="m", user_id="u")
    s.log_operation("view_evidence", edge_id="r1", body="あ" * 500)
    assert "body" not in s.operations[0]
    assert s.evidence_views == 1


def test_store_roundtrip(tmp_path) -> None:
    store = EvaluationStore(tmp_path / "eval.jsonl")
    s = EvaluationSession(map_id="m1", user_id="u1")
    s.rate(5)
    store.append(s)
    assert store.load()[0]["map_id"] == "m1"


def test_evidence_display_rate() -> None:
    plan = {"edges": [{"id": "r1", "evidence_span": [{"document_id": "d"}]},
                      {"id": "r2"}]}
    assert evidence_display_rate(plan)["rate"] == 0.5


def test_causal_precision_log_counts_demotions() -> None:
    plan = {"edges": [
        {"id": "r1", "glyph": "arrow", "causal_check": {"verifier_verdict": "pass"}},
        {"id": "r2", "glyph": "wave", "causal_check": {"demoted_from": "arrow",
                                                       "verifier_verdict": "fail"}},
    ]}
    log = causal_precision_log(plan)
    assert log["kept_as_causal"] == 1 and log["demoted_to_correlation"] == 1


# --------------------------------------------------------- SVG 出力 (§8)

def test_svg_contains_all_nodes_and_is_wellformed() -> None:
    import xml.etree.ElementTree as ET

    kg = {"graph_version": "kg",
          "nodes": [{"id": "a", "label": "NV中心", "community_id": "m1"},
                    {"id": "b", "label": "温度プローブ", "community_id": "m1"}],
          "edges": [{"id": "r1", "from": "a", "to": "b", "label": "因果",
                     "glyph": "arrow"}],
          "communities": [{"id": "m1", "name": "量子センサ"}]}
    svg = build_svg(project(build_multilevel_plan(kg), "standard"))
    ET.fromstring(svg)  # 整形式であること
    assert "NV中心" in svg and "温度プローブ" in svg and "量子センサ" in svg


def test_svg_marks_gap_island_as_non_assertive() -> None:
    kg = {"graph_version": "kg",
          "nodes": [{"id": "a", "label": "A", "community_id": "m1"},
                    {"id": "b", "label": "B", "community_id": "m1"},
                    {"id": "g", "label": "未検証", "community_id": "gap"}],
          "edges": [{"id": "r1", "from": "a", "to": "b", "label": "関連",
                     "glyph": "wave"}],
          "communities": [{"id": "m1", "name": "テーマ"},
                          {"id": "gap", "name": "ギャップ候補", "is_gap": True}]}
    svg = build_svg(project(build_multilevel_plan(kg), "standard"))
    assert "❓" in svg          # 非断定の印
    assert "stroke-dasharray" in svg  # 破線


def test_svg_is_deterministic() -> None:
    kg = {"graph_version": "kg",
          "nodes": [{"id": "a", "label": "A", "community_id": "m"},
                    {"id": "b", "label": "B", "community_id": "m"}],
          "edges": [{"id": "r1", "from": "a", "to": "b", "label": "関連",
                     "glyph": "wave"}],
          "communities": [{"id": "m", "name": "T"}]}
    plan = project(build_multilevel_plan(kg), "standard")
    assert build_svg(plan) == build_svg(plan)
