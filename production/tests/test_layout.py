import json
import os
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


def test_many_islands_stay_compact_and_disjoint():
    """島が多くても横一列に伸びず、重ならず、二次元に広がること。

    元は grid の「行に折り返す」実装をそのまま写したテストだった
    (レイアウト v3 §7)。semantic は行の概念を持たないパッキングなので、
    エンジンに依らない不変条件へ一般化する:

      1. 島 bbox どうしが重ならない
      2. 総幅に上限がある (横一列に伸びていない)
      3. 縦にも広がっている (= 一列ではない)

    grid / semantic の両方で成り立つので、両方で回す。
    """
    kg = {
        "graph_version": "kg_many",
        "nodes": [
            {"id": f"c{i:03d}", "label": f"n{i}", "community_id": f"comm_{i % 6:02d}"}
            for i in range(18)
        ],
        "edges": [],
        "communities": [{"id": f"comm_{i:02d}", "name": f"island {i}"} for i in range(6)],
    }
    for engine in ("semantic", "grid"):
        os.environ["CC_LAYOUT_ENGINE"] = engine
        try:
            plan = compute_layout(kg)
        finally:
            os.environ.pop("CC_LAYOUT_ENGINE", None)
        boxes = [i["bbox"] for i in plan["islands"]]
        assert len(boxes) == 6

        for a in range(len(boxes)):
            for b in range(a + 1, len(boxes)):
                assert _no_overlap(boxes[a], boxes[b]), engine

        total_width = max(b[2] for b in boxes) - min(b[0] for b in boxes)
        total_height = max(b[3] for b in boxes) - min(b[1] for b in boxes)
        # 6 島が一列に並べば総幅は「全島の幅の合計 + 隙間」以上になる。
        # それ未満なら一列ではない (実装が何行に折るかには触れない)。
        assert total_width < sum(b[2] - b[0] for b in boxes), \
            f"{engine}: 島が横一列に伸びている ({total_width}px)"
        assert total_height > max(b[3] - b[1] for b in boxes), \
            f"{engine}: 島が縦に広がっていない (実質 1 行)"
