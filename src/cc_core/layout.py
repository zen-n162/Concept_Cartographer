"""Deterministic layout: knowledge_graph -> layout_plan.

座標・島 bbox の計算は LLM に任せず、このモジュールが決定的に行う
(同じ入力からは常に同じ layout_plan が出る)。

可読性の設計方針 (2026-08-05 の「文字が重なる」問題への対応):
  - ノードの大きさはラベルの表示幅から決める (固定幅だと長いラベルがはみ出す)
  - 列の間隔は「その行に載るエッジラベルの実幅」から決める
    (Excalidraw はエッジラベルを両端ノードの中点に置くため、スキマがラベルより
     狭いとラベルがノードに重なる — これが重なりの主因だった)
  - 行の間隔はノードの高さ + ラベル高さ + 余白

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

from cc_core.textmetrics import balanced_lines, display_width, truncate

# --- 描画パラメータ (adapter と揃えること) ---
NODE_FONT = 14          # ノードラベルの文字サイズ
EDGE_FONT = 12          # エッジラベルの文字サイズ
ELLIPSE_USABLE = 0.58   # 楕円に内接する文字領域の幅比 (Excalidraw の実測値より)
NODE_W_MIN, NODE_W_MAX = 170, 300
NODE_H_MIN = 66
LINE_H = 1.25

EDGE_LABEL_MAX_EM = 8.0     # エッジラベルはこの幅で丸める (長すぎると間隔が破綻する)
NODE_LABEL_MAX_LINES = 2

COL_MARGIN = 28         # ラベル両脇の余白
ROW_MARGIN = 34
ISLAND_PAD = 56         # 島の枠と最初のノードの間
ISLAND_GAP_X = 120      # 島どうしの横の間隔
ISLAND_GAP_Y = 130      # 島の行間
ORIGIN_X = 60
ORIGIN_Y = 80
VALID_GLYPHS = {"arrow", "wave", "zigzag", "double", "hole"}

# adapter 側の glyph 接頭辞 (ラベル幅の見積りに使う)
GLYPH_PREFIX_EM = {"arrow": 0.0, "wave": 1.6, "zigzag": 1.6, "double": 1.6, "hole": 1.6}


def node_size(label: str) -> tuple[float, float]:
    """ラベルが収まる楕円の (幅, 高さ) を返す。"""
    lines, per_line_em = balanced_lines(label, NODE_LABEL_MAX_LINES)
    text_w = per_line_em * NODE_FONT
    text_h = lines * NODE_FONT * LINE_H
    w = min(NODE_W_MAX, max(NODE_W_MIN, text_w / ELLIPSE_USABLE + 24))
    h = max(NODE_H_MIN, text_h / ELLIPSE_USABLE + 18)
    return round(w), round(h)


def edge_label_px(label: str, glyph: str) -> float:
    """エッジラベルの描画幅 (接頭記号込み)。"""
    if not label:
        return 0.0
    em = display_width(truncate(label, EDGE_LABEL_MAX_EM)) + GLYPH_PREFIX_EM.get(glyph, 0.0)
    return em * EDGE_FONT + 10


def compute_layout(kg: dict[str, Any], detail_level: str = "standard") -> dict[str, Any]:
    """Compute a layout_plan from a knowledge graph, deterministically."""
    kg_nodes: list[dict[str, Any]] = kg.get("nodes", [])
    kg_edges: list[dict[str, Any]] = kg.get("edges", [])
    communities: dict[str, dict[str, Any]] = {
        c["id"]: c for c in kg.get("communities", [])
    }

    if not kg_nodes:
        raise ValueError("knowledge_graph has no nodes")

    # --- エッジを正規化し、ラベルを読める長さへ丸める ---
    edges_out: list[dict[str, Any]] = []
    for idx, e in enumerate(kg_edges):
        glyph = e.get("glyph", "arrow")
        if glyph not in VALID_GLYPHS:
            glyph = "arrow"
        edges_out.append({
            "id": e.get("id") or f"r{idx + 1:03d}",
            "from": e["from"],
            "to": e["to"],
            "label": truncate(e.get("label", ""), EDGE_LABEL_MAX_EM),
            "glyph": glyph,
        })

    # --- コミュニティごとにノードをまとめる (入力順を保持 = 決定的) ---
    groups: dict[str, list[dict[str, Any]]] = {}
    for n in kg_nodes:
        groups.setdefault(n.get("community_id") or "comm_default", []).append(n)

    # ノードごとの寸法を先に決める
    sizes = {n["id"]: node_size(n["label"]) for n in kg_nodes}

    # 島が多いときは横一列に伸ばさず格子に折り返す
    islands_per_row = max(1, math.ceil(math.sqrt(len(groups))))
    cursor_x, cursor_y, row_height = ORIGIN_X, ORIGIN_Y, 0.0

    nodes_out: list[dict[str, Any]] = []
    islands_out: list[dict[str, Any]] = []

    for island_idx, (cid, members) in enumerate(groups.items()):
        if island_idx > 0 and island_idx % islands_per_row == 0:
            cursor_x = ORIGIN_X
            cursor_y += row_height + ISLAND_GAP_Y
            row_height = 0.0

        cols = max(1, math.ceil(math.sqrt(len(members))))
        rows = math.ceil(len(members) / cols)
        grid = {  # (row, col) -> node
            (i // cols, i % cols): n for i, n in enumerate(members)
        }
        member_ids = {n["id"] for n in members}

        # 列幅 = その列で最も広いノード
        col_w = [
            max((sizes[grid[(r, c)]["id"]][0] for r in range(rows) if (r, c) in grid),
                default=NODE_W_MIN)
            for c in range(cols)
        ]
        row_h = [
            max((sizes[grid[(r, c)]["id"]][1] for c in range(cols) if (r, c) in grid),
                default=NODE_H_MIN)
            for r in range(rows)
        ]

        # 隣接ノード間に必要なスキマ = そこに載るエッジラベルの幅
        pos_of = {grid[k]["id"]: k for k in grid}
        col_gap = [COL_MARGIN] * max(1, cols - 1)
        row_gap = [ROW_MARGIN] * max(1, rows - 1)
        for e in edges_out:
            if e["from"] not in member_ids or e["to"] not in member_ids:
                continue
            (r1, c1), (r2, c2) = pos_of[e["from"]], pos_of[e["to"]]
            width = edge_label_px(e["label"], e["glyph"])
            if r1 == r2 and abs(c1 - c2) == 1:              # 横に隣接
                i = min(c1, c2)
                col_gap[i] = max(col_gap[i], width + COL_MARGIN)
            elif c1 == c2 and abs(r1 - r2) == 1:            # 縦に隣接
                i = min(r1, r2)
                row_gap[i] = max(row_gap[i], EDGE_FONT * LINE_H + ROW_MARGIN)

        # 列・行の開始位置を積み上げる
        col_x, x = [], 0.0
        for c in range(cols):
            col_x.append(x)
            x += col_w[c] + (col_gap[c] if c < cols - 1 else 0)
        inner_w = x
        row_y, y = [], 0.0
        for r in range(rows):
            row_y.append(y)
            y += row_h[r] + (row_gap[r] if r < rows - 1 else 0)
        inner_h = y

        width = 2 * ISLAND_PAD + inner_w
        height = 2 * ISLAND_PAD + inner_h
        x0, y0 = cursor_x, cursor_y
        row_height = max(row_height, height)

        for (r, c), n in grid.items():
            w, h = sizes[n["id"]]
            nodes_out.append({
                "id": n["id"],
                "label": n["label"],
                # セル内で中央寄せ (列幅より狭いノードが左に寄らないように)
                "x": round(x0 + ISLAND_PAD + col_x[c] + (col_w[c] - w) / 2),
                "y": round(y0 + ISLAND_PAD + row_y[r] + (row_h[r] - h) / 2),
                "size": w,
                "height": h,
                "community_id": cid,
                "style": {"rough": True},
            })

        meta = communities.get(cid, {})
        islands_out.append({
            "community_id": cid,
            "name": meta.get("name", cid),
            "bbox": [x0, y0, round(x0 + width), round(y0 + height)],
            "is_gap": bool(meta.get("is_gap", False)),
        })
        cursor_x += width + ISLAND_GAP_X

    return {
        "detail_level": detail_level,
        "nodes": nodes_out,
        "edges": edges_out,
        "islands": islands_out,
        "provenance": {
            "graph_version": kg.get("graph_version", "kg_unknown"),
            "generated_for": kg.get("generated_for", "layout_engine"),
            "layout_engine": "cc_core.layout/0.2 text-aware-grid",
        },
    }
