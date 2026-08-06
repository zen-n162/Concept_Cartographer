import json
from pathlib import Path

from cc_core.layout import compute_layout
from cc_core.validate import validate_layout_plan

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _kg():
    return json.loads((FIXTURES / "kg_min.json").read_text(encoding="utf-8"))


def test_layout_output_is_valid_plan():
    plan = compute_layout(_kg())
    result = validate_layout_plan(plan)
    assert result.valid, result.errors


def test_layout_is_deterministic():
    assert compute_layout(_kg()) == compute_layout(_kg())


def test_layout_groups_by_community():
    plan = compute_layout(_kg())
    islands = {i["community_id"]: i for i in plan["islands"]}
    assert set(islands) == {"comm_001", "comm_gap_001"}
    assert islands["comm_gap_001"]["is_gap"] is True
    # every node sits inside its island bbox
    for node in plan["nodes"]:
        x0, y0, x1, y1 = islands[node["community_id"]]["bbox"]
        assert x0 <= node["x"] <= x1
        assert y0 <= node["y"] <= y1


def test_layout_preserves_edges_and_glyphs():
    plan = compute_layout(_kg())
    glyphs = {e["id"]: e["glyph"] for e in plan["edges"]}
    assert glyphs == {"r001": "arrow", "r002": "hole"}


def _no_overlap(a: list[float], b: list[float]) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0


def test_islands_do_not_overlap():
    plan = compute_layout(_kg())
    boxes = [i["bbox"] for i in plan["islands"]]
    for a in range(len(boxes)):
        for b in range(a + 1, len(boxes)):
            assert _no_overlap(boxes[a], boxes[b])


def test_islands_wrap_into_grid_when_many_communities():
    """島が多いとき横一列に伸びず、行を折り返して bbox が重ならないこと。"""
    kg = {
        "graph_version": "kg_many",
        "nodes": [
            {"id": f"c{i:03d}", "label": f"n{i}", "community_id": f"comm_{i % 6:02d}"}
            for i in range(18)
        ],
        "edges": [],
        "communities": [{"id": f"comm_{i:02d}", "name": f"island {i}"} for i in range(6)],
    }
    plan = compute_layout(kg)
    boxes = [i["bbox"] for i in plan["islands"]]
    assert len(boxes) == 6
    for a in range(len(boxes)):
        for b in range(a + 1, len(boxes)):
            assert _no_overlap(boxes[a], boxes[b])
    total_width = max(b[2] for b in boxes) - min(b[0] for b in boxes)
    assert total_width < 3000, f"islands still laid out in one long row ({total_width}px)"
    assert len({b[1] for b in boxes}) > 1, "expected more than one island row"
