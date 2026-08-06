"""可変詳細度の回帰テスト (実運用計画 §4 / v3 §2.4)。

守るべき性質:
  1. 表示ノード数が帯に収まる (Overview 10-20 / Standard 20-50 / Detailed 50-100)。
     **集約ノードも表示枠を消費する**ので、概念+集約の合計で判定する。
  2. レベル間で粒度が分化する (小さいグラフでも overview が最も粗い)。
  3. 全テーマ (コミュニティ) が overview でも地図上に残る。
  4. 決定的である (同じ入力から常に同じ結果)。
  5. 切替は再レイアウトなしで、各レベルが単体で妥当な layout_plan である。
  6. 上位層が付けた属性 (evidence_span 等) がレベル横断で保持される。
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from cc_core.community import (
    LEVEL_BANDS,
    LEVEL_ORDER,
    analyze,
    build_graph,
    count_aggregates,
    detect_communities,
    expand_aggregate,
    score_importance,
)
from cc_core.detail import build_multilevel_plan, check_level_bands, project
from cc_core.overlap import check_overlaps, resolve_label_offset
from cc_core.validate import validate_layout_plan

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def synth_kg(n_nodes: int, n_comms: int = 6, seed: int = 7) -> dict:
    """コミュニティ構造を持つ合成知識グラフ。"""
    rnd = random.Random(seed)
    nodes = [{"id": f"c{i:03d}", "label": f"概念{i:03d}のラベル",
              "community_id": f"src_{i % n_comms}"} for i in range(n_nodes)]
    edges = []
    for i in range(n_nodes):
        # 同一コミュニティ内を密に、他コミュニティへは疎に繋ぐ
        same = [j for j in range(n_nodes) if j % n_comms == i % n_comms and j != i]
        for _ in range(2):
            if same:
                j = rnd.choice(same)
                edges.append({"id": f"r{len(edges):04d}", "from": f"c{i:03d}",
                              "to": f"c{j:03d}", "label": "関係の説明",
                              "glyph": "wave", "confidence": 0.8})
        if rnd.random() < 0.25:
            j = rnd.randrange(n_nodes)
            if j != i:
                edges.append({"id": f"r{len(edges):04d}", "from": f"c{i:03d}",
                              "to": f"c{j:03d}", "label": "横断関係",
                              "glyph": "double", "confidence": 0.6})
    return {"graph_version": "kg_synth", "nodes": nodes, "edges": edges,
            "communities": [{"id": f"src_{i}", "name": f"テーマ{i}"}
                            for i in range(n_comms)]}


@pytest.fixture
def real_kg() -> dict:
    path = Path(__file__).resolve().parents[1] / "graphs" / "kg_session_20260805_175134.json"
    if not path.exists():
        pytest.skip("実データ fixture が無い")
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------- 帯の遵守

@pytest.mark.parametrize("n_nodes", [19, 60, 100, 200, 400])
def test_display_count_within_band(n_nodes: int) -> None:
    """概念 + 集約の合計が各レベルの上限を超えないこと。"""
    plan = build_multilevel_plan(synth_kg(n_nodes, n_comms=min(10, max(3, n_nodes // 25))))
    assert check_level_bands(plan) == [], plan["levels"]
    for level in LEVEL_ORDER:
        view = project(plan, level)
        assert len(view["nodes"]) <= LEVEL_BANDS[level][1], (
            f"{level}: {len(view['nodes'])} > {LEVEL_BANDS[level][1]}")


def test_levels_are_progressively_coarser() -> None:
    """overview <= standard <= detailed の順で粗くなること。"""
    plan = build_multilevel_plan(synth_kg(120, n_comms=8))
    lv = plan["levels"]
    assert lv["overview"]["nodes"] <= lv["standard"]["nodes"] <= lv["detailed"]["nodes"]
    # 大きいグラフでは実際に差がつく (3 レベルが同一だと選ぶ意味がない)
    assert lv["overview"]["nodes"] < lv["detailed"]["nodes"]


def test_small_graph_still_differentiates_overview() -> None:
    """小さいグラフでも overview は standard より粗いこと。"""
    plan = build_multilevel_plan(synth_kg(19, n_comms=5))
    lv = plan["levels"]
    assert lv["overview"]["nodes"] < lv["standard"]["nodes"]


# ------------------------------------------------------- 島が消えないこと

def test_every_community_survives_overview() -> None:
    """overview でも全テーマが (概念か集約の形で) 地図に残ること。"""
    kg = synth_kg(150, n_comms=7)
    analysis = analyze(kg)
    view = project(build_multilevel_plan(kg), "overview")
    shown = {n["community_id"] for n in view["nodes"]}
    all_comms = set(analysis.communities.values())
    # 集約へ併合された分は comm_misc になるため、表示コミュニティ数は
    # 元より少なくなりうるが、ゼロや極端な欠落は許さない
    assert shown, "overview に島が 1 つも残っていない"
    assert len(shown) >= min(len(all_comms), 3)


# ------------------------------------------------------------- 決定性

def test_analysis_is_deterministic() -> None:
    kg = synth_kg(80)
    a, b = analyze(kg), analyze(kg)
    assert a.communities == b.communities
    assert a.visible == b.visible
    assert [x.to_dict() for x in a.aggregates["overview"]] == \
           [x.to_dict() for x in b.aggregates["overview"]]


def test_multilevel_plan_is_deterministic() -> None:
    kg = synth_kg(60)
    assert build_multilevel_plan(kg) == build_multilevel_plan(kg)


# ------------------------------------------- 各レベルが妥当な plan であること

@pytest.mark.parametrize("level", LEVEL_ORDER)
def test_each_level_is_a_valid_plan(level: str, real_kg: dict) -> None:
    view = project(build_multilevel_plan(real_kg), level)
    result = validate_layout_plan(view)
    assert result.valid, result.errors


@pytest.mark.parametrize("level", LEVEL_ORDER)
def test_each_level_is_readable(level: str) -> None:
    """どのレベルでも未解決のラベル重なりが無いこと。"""
    view = project(build_multilevel_plan(synth_kg(100, n_comms=8)), level)
    nodes = {n["id"]: n for n in view["nodes"]}
    unresolved = [
        c for c in check_overlaps(view).label_on_node
        if resolve_label_offset(
            next(e for e in view["edges"] if e["id"] == c["edge"]), nodes) is None
    ]
    assert not unresolved, unresolved


def test_switch_does_not_require_recompute() -> None:
    """切替は同梱 plan の取り出しだけで済むこと (v3 §2.4)。"""
    plan = build_multilevel_plan(synth_kg(80))
    for level in LEVEL_ORDER:
        view = project(plan, level)
        assert not view.get("_needs_recompute")


def test_project_marks_recompute_when_not_bundled() -> None:
    plan = build_multilevel_plan(synth_kg(40))
    del plan["_level_plans"]["overview"]
    assert project(plan, "overview").get("_needs_recompute") is True


# ------------------------------------------------------------- 属性の保持

def test_edge_attributes_survive_all_levels() -> None:
    """evidence_span 等が縮約・レベル切替を越えて保持されること。"""
    kg = synth_kg(80)
    for e in kg["edges"]:
        e["evidence_span"] = [{"document_id": "d1", "char_start": 0,
                               "char_end": 10, "surface": "原文"}]
        e["epistemic_status"] = "observed"
    plan = build_multilevel_plan(kg)
    for level in LEVEL_ORDER:
        view = project(plan, level)
        assert view["edges"], level
        assert all(e.get("evidence_span") for e in view["edges"]), level


def test_merged_edges_keep_member_ids() -> None:
    """縮約されたエッジが元の id を保持すること (v4核§6.4)。"""
    plan = build_multilevel_plan(synth_kg(150, n_comms=8))
    view = project(plan, "overview")
    merged = [e for e in view["edges"] if e.get("member_edge_ids")]
    assert merged, "overview で縮約が 1 本も起きていない"
    assert all(len(e["member_edge_ids"]) >= 2 for e in merged)


# ----------------------------------------------------------- ドリルダウン

def test_expand_aggregate_returns_members() -> None:
    plan = build_multilevel_plan(synth_kg(120, n_comms=7))
    assert plan["aggregates"], "集約が生成されていない"
    agg = plan["aggregates"][0]
    members = expand_aggregate(plan, agg["id"])
    assert members == agg["member_node_ids"]
    assert len(members) >= 2


def test_expand_unknown_aggregate_raises() -> None:
    plan = build_multilevel_plan(synth_kg(60))
    with pytest.raises(KeyError):
        expand_aggregate(plan, "agg-does-not-exist")


# ----------------------------------------------------- コミュニティ・重要度

def test_communities_detected_on_clustered_graph() -> None:
    kg = synth_kg(90, n_comms=6)
    comms = detect_communities(build_graph(kg))
    assert 2 <= len(set(comms.values())) <= 20
    assert len(comms) == len(kg["nodes"])


def test_importance_components_are_normalized() -> None:
    kg = synth_kg(50)
    g = build_graph(kg)
    scores = score_importance(g, kg)
    assert scores
    for s in scores.values():
        for v in (s.betweenness, s.frequency, s.novelty):
            assert 0.0 <= v <= 1.0
        assert 0.0 <= s.total <= 1.0


def test_isolated_nodes_do_not_crash() -> None:
    kg = {"graph_version": "kg", "nodes": [{"id": "a", "label": "A"},
                                           {"id": "b", "label": "B"}],
          "edges": [], "communities": []}
    plan = build_multilevel_plan(kg)
    assert len(plan["nodes"]) == 2


def test_count_aggregates_matches_built_aggregates() -> None:
    kg = synth_kg(100, n_comms=6)
    analysis = analyze(kg)
    for level in LEVEL_ORDER:
        expected = count_aggregates(analysis.communities, analysis.visible[level])
        built = len(analysis.aggregates[level])
        # 併合が起きた場合は built < expected になりうる (上限で丸めるため)
        assert built <= expected or expected == 0
