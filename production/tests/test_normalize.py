"""LLM 出力の形崩れに対する正規化テスト。

2026-08-07 の実障害: 抽出エージェントが evidence_span を **配列ではなく単一
オブジェクト**で返し、辞書を for で回してキー (文字列) が出たため
`'str' object has no attribute 'get'` でパイプラインが停止した。
併せて char_start / char_end が null で返り、スキーマ違反にもなっていた。

教訓: プロンプトで縛るだけに頼らず、受け取り側で必ず正規化する。
以下は「エージェントがこう返しても壊れない」ことの回帰テスト。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cc_core.causal import _edge_text, apply_relation_policy
from cc_core.detail import build_multilevel_plan
from cc_core.normalize import normalize_evidence_span, normalize_kg, NormalizeReport
from cc_core.validate import validate_layout_plan


def kg_with(edges: list[dict]) -> dict:
    return {
        "graph_version": "kg_t",
        "nodes": [{"id": "a", "label": "A", "community_id": "m"},
                  {"id": "b", "label": "B", "community_id": "m"}],
        "edges": edges,
        "communities": [{"id": "m", "name": "テーマ"}],
    }


# ------------------------------------------------- evidence_span の形の揺れ

def test_single_object_becomes_list() -> None:
    """実障害そのもの: 単一オブジェクト -> 配列。"""
    rep = NormalizeReport()
    out = normalize_evidence_span(
        {"document_id": "d1", "surface": "原文", "char_start": None,
         "char_end": None}, rep)
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["surface"] == "原文"
    assert "char_start" not in out[0]   # null は落とす
    assert rep.repairs


def test_bare_string_becomes_span() -> None:
    rep = NormalizeReport()
    out = normalize_evidence_span("原文の引用", rep)
    assert out == [{"surface": "原文の引用"}]


def test_list_of_strings_is_accepted() -> None:
    rep = NormalizeReport()
    out = normalize_evidence_span(["引用1", "引用2"], rep)
    assert [x["surface"] for x in out] == ["引用1", "引用2"]


def test_camelcase_keys_are_accepted() -> None:
    rep = NormalizeReport()
    out = normalize_evidence_span(
        [{"documentId": "d1", "charStart": 5, "charEnd": 9, "surface": "x"}], rep)
    assert out[0]["document_id"] == "d1"
    assert out[0]["char_start"] == 5 and out[0]["char_end"] == 9


def test_half_open_char_range_is_dropped() -> None:
    """片側だけの char 範囲は範囲として使えないので落とす。"""
    rep = NormalizeReport()
    out = normalize_evidence_span([{"surface": "x", "char_start": 3}], rep)
    assert "char_start" not in out[0]


def test_none_and_garbage_are_safe() -> None:
    rep = NormalizeReport()
    assert normalize_evidence_span(None, rep) == []
    assert normalize_evidence_span(123, rep) == []
    assert normalize_evidence_span([None, 5], rep) == []


# ------------------------------------------------------ KG 全体の正規化

def test_malformed_kg_survives_full_pipeline() -> None:
    """実障害と同じ形の KG が、描画計画まで通ること。"""
    kg = kg_with([{"id": "r1", "from": "a", "to": "b", "label": "因果",
                   "glyph": "arrow",
                   "evidence_span": {"document_id": "d1", "char_start": None,
                                     "char_end": None,
                                     "surface": "介入により X を操作すると Y が変化した"}}])
    norm, rep = normalize_kg(kg)
    assert rep.repairs
    out, stats = apply_relation_policy(norm)     # ここで以前は AttributeError
    assert stats["causal_kept"] == 1             # 語彙証拠があるので因果が残る
    plan = build_multilevel_plan(out)
    assert validate_layout_plan(plan).valid


def test_causal_text_extraction_is_defensive() -> None:
    """正規化を通さずに壊れた形が来ても _edge_text が落ちないこと。"""
    for ev in ({"surface": "機序を介して"}, "反事実であれば", [{"surface": "介入"}],
               [{"no_surface": 1}], None, 42):
        assert isinstance(_edge_text({"evidence_span": ev, "label": "L"}), str)


def test_dangling_edges_are_dropped() -> None:
    kg = kg_with([{"id": "r1", "from": "a", "to": "zzz", "label": "x", "glyph": "wave"},
                  {"id": "r2", "from": "a", "to": "b", "label": "y", "glyph": "wave"}])
    norm, rep = normalize_kg(kg)
    assert [e["id"] for e in norm["edges"]] == ["r2"]
    assert rep.dropped_edges == ["r1"]


def test_self_loops_are_dropped() -> None:
    kg = kg_with([{"id": "r1", "from": "a", "to": "a", "label": "x", "glyph": "wave"}])
    norm, rep = normalize_kg(kg)
    assert norm["edges"] == []
    assert rep.dropped_edges == ["r1"]


def test_unknown_glyph_falls_back_to_correlation() -> None:
    """未知の glyph は因果ではなく相関へ倒す (安全側)。"""
    kg = kg_with([{"id": "r1", "from": "a", "to": "b", "label": "x",
                   "glyph": "sparkle"}])
    norm, _ = normalize_kg(kg)
    assert norm["edges"][0]["glyph"] == "wave"


def test_missing_glyph_defaults_to_correlation() -> None:
    kg = kg_with([{"id": "r1", "from": "a", "to": "b", "label": "x"}])
    norm, _ = normalize_kg(kg)
    assert norm["edges"][0]["glyph"] == "wave"


def test_duplicate_ids_are_renumbered() -> None:
    kg = {"graph_version": "k",
          "nodes": [{"id": "a", "label": "A"}, {"id": "a", "label": "A2"}],
          "edges": [], "communities": []}
    norm, rep = normalize_kg(kg)
    assert len({n["id"] for n in norm["nodes"]}) == 2
    assert any("重複" in k for k in rep.repairs)


def test_string_nodes_become_objects() -> None:
    kg = {"graph_version": "k", "nodes": ["概念A", "概念B"],
          "edges": [], "communities": []}
    norm, _ = normalize_kg(kg)
    assert [n["label"] for n in norm["nodes"]] == ["概念A", "概念B"]
    assert all(n["id"] for n in norm["nodes"])


def test_missing_community_definition_is_filled() -> None:
    kg = {"graph_version": "k",
          "nodes": [{"id": "a", "label": "A", "community_id": "未定義"}],
          "edges": [], "communities": []}
    norm, rep = normalize_kg(kg)
    assert any(c["id"] == "未定義" for c in norm["communities"])


def test_confidence_is_clamped() -> None:
    kg = kg_with([{"id": "r1", "from": "a", "to": "b", "label": "x",
                   "glyph": "wave", "confidence": 5.0}])
    norm, _ = normalize_kg(kg)
    assert norm["edges"][0]["confidence"] == 1.0


def test_invalid_epistemic_status_is_dropped() -> None:
    kg = kg_with([{"id": "r1", "from": "a", "to": "b", "label": "x",
                   "glyph": "wave", "epistemic_status": "とても確か"}])
    norm, _ = normalize_kg(kg)
    assert "epistemic_status" not in norm["edges"][0]


def test_non_dict_input_raises_clearly() -> None:
    with pytest.raises(TypeError):
        normalize_kg("これは KG ではない")


def test_normalization_is_idempotent() -> None:
    kg = kg_with([{"id": "r1", "from": "a", "to": "b", "label": "因果",
                   "glyph": "arrow", "evidence_span": {"surface": "機序"}}])
    once, _ = normalize_kg(kg)
    twice, rep = normalize_kg(once)
    assert once == twice
    assert not rep.repairs   # 2 回目は直すところが無い


# ------------------------------------------------------- 実データでの再現

def test_real_failing_session_is_repaired() -> None:
    """実際に失敗したセッションの KG が通ること (再発防止)。"""
    path = (Path(__file__).resolve().parents[1] / "graphs"
            / "kg_session_20260807_003732.json")
    if not path.exists():
        pytest.skip("実障害データが無い")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(raw["edges"][0]["evidence_span"], dict)  # 壊れた形のまま
    kg, rep = normalize_kg(raw)
    assert rep.repairs
    out, _ = apply_relation_policy(kg)
    plan = build_multilevel_plan(out)
    assert validate_layout_plan(plan).valid
    assert all(isinstance(e.get("evidence_span", []), list) for e in plan["edges"])
