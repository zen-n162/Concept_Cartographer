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


def test_islands_do_not_overlap():
    plan = compute_layout(_kg())
    boxes = [i["bbox"] for i in plan["islands"]]
    for a in range(len(boxes)):
        for b in range(a + 1, len(boxes)):
            ax0, _, ax1, _ = boxes[a]
            bx0, _, bx1, _ = boxes[b]
            assert ax1 <= bx0 or bx1 <= ax0  # laid out left-to-right
