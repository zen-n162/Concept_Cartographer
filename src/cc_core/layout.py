"""Deterministic layout: knowledge_graph -> layout_plan.

座標・島 bbox の計算は LLM に任せず、このモジュールが決定的に行う
(同じ入力からは常に同じ layout_plan が出る)。

knowledge_graph 入力の想定最小形:
{
  "graph_version": "kg_...",
  "nodes": [{"id", "label", "community_id"?}],
  "edges": [{"id"?, "from", "to", "label"?, "glyph"?}],
  "communities": [{"id", "name", "is_gap"?}]   # optional
}
"""

from __future__ import annotations

import math
from typing import Any

# Layout constants (tuned for Excalidraw default viewport)
NODE_W = 180          # node size (ellipse width); height = size * 0.5
CELL_W = 260          # grid cell width inside an island
CELL_H = 140          # grid cell height inside an island
ISLAND_PAD = 60       # padding between island border and first node
ISLAND_GAP_X = 120    # horizontal gap between islands
ISLAND_GAP_Y = 120    # vertical gap between island rows
ORIGIN_X = 60
ORIGIN_Y = 80
VALID_GLYPHS = {"arrow", "wave", "zigzag", "double", "hole"}


def compute_layout(kg: dict[str, Any], detail_level: str = "standard") -> dict[str, Any]:
    """Compute a layout_plan from a knowledge graph, deterministically."""
    kg_nodes: list[dict[str, Any]] = kg.get("nodes", [])
    kg_edges: list[dict[str, Any]] = kg.get("edges", [])
    communities: dict[str, dict[str, Any]] = {
        c["id"]: c for c in kg.get("communities", [])
    }

    if not kg_nodes:
        raise ValueError("knowledge_graph has no nodes")

    # Group nodes by community, preserving input order (deterministic).
    groups: dict[str, list[dict[str, Any]]] = {}
    for n in kg_nodes:
        cid = n.get("community_id") or "comm_default"
        groups.setdefault(cid, []).append(n)

    nodes_out: list[dict[str, Any]] = []
    islands_out: list[dict[str, Any]] = []

    # Islands are arranged in a grid (not one long row): with many communities a
    # single row grows to several thousand px and stops being readable.
    islands_per_row = max(1, math.ceil(math.sqrt(len(groups))))
    cursor_x = ORIGIN_X
    cursor_y = ORIGIN_Y
    row_height = 0

    for island_idx, (cid, members) in enumerate(groups.items()):
        if island_idx > 0 and island_idx % islands_per_row == 0:
            cursor_x = ORIGIN_X
            cursor_y += row_height + ISLAND_GAP_Y
            row_height = 0

        cols = max(1, math.ceil(math.sqrt(len(members))))
        rows = math.ceil(len(members) / cols)
        width = 2 * ISLAND_PAD + cols * CELL_W
        height = 2 * ISLAND_PAD + rows * CELL_H
        x0, y0 = cursor_x, cursor_y
        row_height = max(row_height, height)

        for idx, n in enumerate(members):
            col, row = idx % cols, idx // cols
            nodes_out.append(
                {
                    "id": n["id"],
                    "label": n["label"],
                    "x": x0 + ISLAND_PAD + col * CELL_W,
                    "y": y0 + ISLAND_PAD + row * CELL_H,
                    "size": NODE_W,
                    "community_id": cid,
                    "style": {"rough": True},
                }
            )

        meta = communities.get(cid, {})
        islands_out.append(
            {
                "community_id": cid,
                "name": meta.get("name", cid),
                "bbox": [x0, y0, x0 + width, y0 + height],
                "is_gap": bool(meta.get("is_gap", False)),
            }
        )
        cursor_x += width + ISLAND_GAP_X


    edges_out: list[dict[str, Any]] = []
    for idx, e in enumerate(kg_edges):
        glyph = e.get("glyph", "arrow")
        if glyph not in VALID_GLYPHS:
            glyph = "arrow"
        edges_out.append(
            {
                "id": e.get("id") or f"r{idx + 1:03d}",
                "from": e["from"],
                "to": e["to"],
                "label": e.get("label", ""),
                "glyph": glyph,
            }
        )

    return {
        "detail_level": detail_level,
        "nodes": nodes_out,
        "edges": edges_out,
        "islands": islands_out,
        "provenance": {
            "graph_version": kg.get("graph_version", "kg_unknown"),
            "generated_for": kg.get("generated_for", "layout_engine"),
            "layout_engine": "cc_core.layout/0.1 deterministic-grid",
        },
    }
