"""レイアウトの重なり検査 (可読性の自動チェック)。

「文字が読めない」は主観に見えるが、原因は幾何的に特定できる:
  - エッジラベルがノードに重なる (Excalidraw はラベルを両端の中点に置く)
  - ノードどうしが重なる
  - ノードが島の枠からはみ出す
これらを layout_plan の座標だけで判定し、描画前に検出できるようにする。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cc_core.layout import (
    EDGE_FONT,
    GLYPH_PREFIX_EM,
    LINE_H,
    NODE_H_MIN,
)
from cc_core.textmetrics import display_width

Rect = tuple[float, float, float, float]  # (x0, y0, x1, y1)

# 楕円は外接矩形より内側なので、当たり判定を少し縮める
ELLIPSE_SHRINK = 0.95  # 楕円の縁への接触も検出できるよう厳しめに取る


@dataclass
class OverlapReport:
    label_on_node: list[dict[str, Any]] = field(default_factory=list)
    node_on_node: list[dict[str, Any]] = field(default_factory=list)
    node_outside_island: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.label_on_node or self.node_on_node or self.node_outside_island)

    def to_dict(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "label_on_node": self.label_on_node,
            "node_on_node": self.node_on_node,
            "node_outside_island": self.node_outside_island,
        }


def _intersects(a: Rect, b: Rect) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def node_rect(node: dict[str, Any], shrink: float = ELLIPSE_SHRINK) -> Rect:
    w = node["size"] * shrink
    h = node.get("height", max(NODE_H_MIN, node["size"] * 0.55)) * shrink
    cx = node["x"] + node["size"] / 2
    cy = node["y"] + node.get("height", max(NODE_H_MIN, node["size"] * 0.55)) / 2
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def edge_label_rect(edge: dict[str, Any], nodes: dict[str, dict]) -> Rect | None:
    """Excalidraw がエッジラベルを描く矩形 (両端ノード中心の中点に配置される)。"""
    label = edge.get("label", "")
    if not label:
        return None
    a, b = nodes[edge["from"]], nodes[edge["to"]]

    def center(n: dict) -> tuple[float, float]:
        h = n.get("height", max(NODE_H_MIN, n["size"] * 0.55))
        return n["x"] + n["size"] / 2, n["y"] + h / 2

    (ax, ay), (bx, by) = center(a), center(b)
    mx, my = (ax + bx) / 2, (ay + by) / 2
    em = display_width(label) + GLYPH_PREFIX_EM.get(edge["glyph"], 0.0)
    w, h = em * EDGE_FONT, EDGE_FONT * LINE_H
    return (mx - w / 2, my - h / 2, mx + w / 2, my + h / 2)


def resolve_label_offset(edge: dict[str, Any], nodes: dict[str, dict],
                         max_steps: int = 6) -> tuple[float, float] | None:
    """エッジラベルを中点に置くと他ノードに重なる場合の退避位置を返す。

    中点で衝突しないなら None (= Excalidraw の bound text をそのまま使う)。
    衝突する場合は線に垂直な向きへ段階的にずらし、最初に衝突しなくなる
    絶対座標 (中心) を返す。中間ノードを飛び越す長いエッジで起きる。
    """
    lr = edge_label_rect(edge, nodes)
    if lr is None:
        return None
    rects = {nid: node_rect(n) for nid, n in nodes.items()}
    if not any(_intersects(lr, r) for r in rects.values()):
        return None

    a, b = nodes[edge["from"]], nodes[edge["to"]]

    def center(n: dict) -> tuple[float, float]:
        h = n.get("height", max(NODE_H_MIN, n["size"] * 0.55))
        return n["x"] + n["size"] / 2, n["y"] + h / 2

    (ax, ay), (bx, by) = center(a), center(b)
    mx, my = (ax + bx) / 2, (ay + by) / 2
    dx, dy = bx - ax, by - ay
    length = max(1.0, (dx * dx + dy * dy) ** 0.5)
    # 線に垂直な単位ベクトル
    px, py = -dy / length, dx / length
    lw, lh = lr[2] - lr[0], lr[3] - lr[1]
    step = max(lh + 10, 34)

    for i in range(1, max_steps + 1):
        for sign in (1, -1):
            cx, cy = mx + px * step * i * sign, my + py * step * i * sign
            cand = (cx - lw / 2, cy - lh / 2, cx + lw / 2, cy + lh / 2)
            if not any(_intersects(cand, r) for r in rects.values()):
                return (cx, cy)
    return (mx, my - step * (max_steps + 1))  # 見つからなければ上へ大きく退避


def check_overlaps(plan: dict[str, Any]) -> OverlapReport:
    report = OverlapReport()
    nodes = {n["id"]: n for n in plan["nodes"]}
    rects = {nid: node_rect(n) for nid, n in nodes.items()}

    # エッジラベル vs ノード
    for edge in plan.get("edges", []):
        lr = edge_label_rect(edge, nodes)
        if lr is None:
            continue
        for nid, nr in rects.items():
            if nid in (edge["from"], edge["to"]) and not _intersects(lr, nr):
                continue
            if _intersects(lr, nr):
                report.label_on_node.append({"edge": edge["id"], "node": nid})

    # ノード vs ノード
    ids = list(rects)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if _intersects(rects[ids[i]], rects[ids[j]]):
                report.node_on_node.append({"a": ids[i], "b": ids[j]})

    # ノードが島の外へ出ていないか
    islands = {i["community_id"]: i for i in plan.get("islands", [])}
    for nid, n in nodes.items():
        isl = islands.get(n["community_id"])
        if not isl:
            continue
        x0, y0, x1, y1 = isl["bbox"]
        h = n.get("height", max(NODE_H_MIN, n["size"] * 0.55))
        if not (x0 <= n["x"] and n["x"] + n["size"] <= x1
                and y0 <= n["y"] and n["y"] + h <= y1):
            report.node_outside_island.append(nid)

    return report
