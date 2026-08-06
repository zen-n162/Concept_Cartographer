"""レイアウトの可読性 (重なり) テスト。

2026-08-05 の実障害: エッジラベルがノード間のスキマより広く、11/17 本の
ラベルがノードに重なって読めなくなった。レイアウトは間隔をラベル幅から
決めるべき、という回帰テスト。
"""

import json
from pathlib import Path

import pytest

from cc_core.layout import compute_layout, edge_label_px, node_size
from cc_core.overlap import check_overlaps, resolve_label_offset
from cc_core.textmetrics import display_width, truncate, wrap_to_lines
from cc_core.validate import validate_layout_plan

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _kg():
    return json.loads((FIXTURES / "kg_min.json").read_text(encoding="utf-8"))


def _dense_kg(n_nodes: int = 12, label: str = "長めの概念ラベル名称") -> dict:
    """同一行に長いラベルのエッジが並ぶ、重なりが起きやすい構成。"""
    nodes = [{"id": f"c{i:03d}", "label": f"{label}{i}", "community_id": "m1"}
             for i in range(n_nodes)]
    edges = [{"id": f"r{i:03d}", "from": f"c{i:03d}", "to": f"c{i+1:03d}",
              "label": "関係の説明テキスト", "glyph": "arrow"}
             for i in range(n_nodes - 1)]
    return {"graph_version": "kg_dense", "nodes": nodes, "edges": edges,
            "communities": [{"id": "m1", "name": "密なテーマ"}]}


# --- textmetrics ---

def test_display_width_counts_fullwidth_as_one_em():
    assert display_width("概念") == pytest.approx(2.0)
    assert display_width("ab") == pytest.approx(1.1)
    assert display_width("") == 0


def test_truncate_respects_budget():
    out = truncate("非常に長い関係ラベルの説明文", 8.0)
    assert display_width(out) <= 8.0
    assert out.endswith("…")
    assert truncate("短い", 8.0) == "短い"


def test_wrap_splits_japanese_anywhere():
    lines = wrap_to_lines("あいうえおかきくけこ", 4.0, max_lines=3)
    assert all(display_width(x) <= 4.0 for x in lines)
    assert "".join(lines).rstrip("…") in "あいうえおかきくけこ"


# --- node sizing ---

def test_node_grows_with_label_length():
    w_short, _ = node_size("NV中心")
    w_long, _ = node_size("非常に長い概念名称のノードラベル")
    assert w_long > w_short


def test_node_size_is_clamped():
    w, h = node_size("あ" * 200)
    assert w <= 300 and h >= 66


# --- layout spacing ---

def test_gap_between_adjacent_nodes_fits_edge_label():
    """同一行の隣接ノード間のスキマが、そこに載るラベルより広いこと。"""
    plan = compute_layout(_dense_kg())
    nodes = {n["id"]: n for n in plan["nodes"]}
    checked = 0
    for e in plan["edges"]:
        a, b = nodes[e["from"]], nodes[e["to"]]
        if a["y"] != b["y"]:
            continue
        left, right = (a, b) if a["x"] < b["x"] else (b, a)
        gap = right["x"] - (left["x"] + left["size"])
        assert gap >= edge_label_px(e["label"], e["glyph"]), \
            f"edge {e['id']}: スキマ {gap} < ラベル幅"
        checked += 1
    assert checked > 0, "同一行の隣接エッジが1本も無い"


def test_dense_layout_has_no_label_overlap():
    plan = compute_layout(_dense_kg())
    nodes = {n["id"]: n for n in plan["nodes"]}
    report = check_overlaps(plan)
    unresolved = [c for c in report.label_on_node
                  if resolve_label_offset(
                      next(e for e in plan["edges"] if e["id"] == c["edge"]), nodes) is None]
    assert not unresolved, unresolved


def test_no_node_overlap_and_all_inside_islands():
    for kg in (_kg(), _dense_kg()):
        report = check_overlaps(compute_layout(kg))
        assert not report.node_on_node
        assert not report.node_outside_island


def test_layout_still_valid_and_deterministic():
    plan = compute_layout(_kg())
    assert validate_layout_plan(plan).valid
    assert compute_layout(_kg()) == plan


def test_long_edge_over_intermediate_node_gets_offset():
    """中間ノードを飛び越すエッジのラベルは退避位置が与えられる。"""
    kg = {
        "graph_version": "kg_skip",
        "nodes": [{"id": f"c{i}", "label": f"ノード{i}", "community_id": "m1"}
                  for i in range(3)],
        "edges": [{"id": "r1", "from": "c0", "to": "c2",
                   "label": "飛び越す関係", "glyph": "arrow"}],
        "communities": [{"id": "m1", "name": "テスト"}],
    }
    plan = compute_layout(kg)
    nodes = {n["id"]: n for n in plan["nodes"]}
    # c0,c1,c2 が横一列なら c0→c2 の中点は c1 の上に来る
    if nodes["c0"]["y"] == nodes["c1"]["y"] == nodes["c2"]["y"]:
        assert resolve_label_offset(plan["edges"][0], nodes) is not None
