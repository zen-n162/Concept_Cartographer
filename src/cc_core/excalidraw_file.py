"""layout_plan -> .excalidraw ファイル (MCP を介さない直接シリアライズ)。

用途: Foundry ポータル完結モード。Foundry の code_interpreter 内でこのロジックを
実行し、.excalidraw をその場で生成してユーザーへ添付する (private な
VM-Excalidraw-MCP へ到達できないポータルからでも概念地図が得られる)。

描画規則は cc_core.adapter と同一に保つこと (glyph 色・破線・半透明・描画順)。
生成物は決定的 (乱数を使わず id から seed を導出)。
"""

from __future__ import annotations

import json
import zlib
from typing import Any

from cc_core.adapter import (
    GAP_OPACITY,
    GLYPH_STYLES,
    NODE_HEIGHT_RATIO,
    edge_element_id,
    island_element_id,
    island_label_id,
    node_element_id,
)

FONT_HAND = 1  # Excalifont / virgil (手描き風)


def _seed(key: str) -> int:
    return zlib.crc32(key.encode("utf-8")) % 2_000_000_000


def _base(el_id: str, el_type: str, x: float, y: float, w: float, h: float,
          **over: Any) -> dict[str, Any]:
    el = {
        "id": el_id, "type": el_type,
        "x": x, "y": y, "width": w, "height": h,
        "angle": 0, "strokeColor": "#1e1e1e", "backgroundColor": "transparent",
        "fillStyle": "solid", "strokeWidth": 1, "strokeStyle": "solid",
        "roughness": 2, "opacity": 100, "groupIds": [], "frameId": None,
        "roundness": None, "seed": _seed(el_id), "version": 1,
        "versionNonce": _seed(el_id + "n"), "isDeleted": False,
        "boundElements": [], "updated": 1, "link": None, "locked": False,
    }
    el.update(over)
    return el


def _text(el_id: str, x: float, y: float, text: str, size: int = 16,
          color: str = "#1e1e1e", opacity: int = 100,
          container: str | None = None) -> dict[str, Any]:
    width = max(20.0, len(text) * size * 0.62)
    el = _base(el_id, "text", x, y, width, size * 1.25,
               strokeColor=color, opacity=opacity)
    el.update({
        "text": text, "originalText": text, "fontSize": size,
        "fontFamily": FONT_HAND, "textAlign": "center" if container else "left",
        "verticalAlign": "middle" if container else "top",
        "containerId": container, "lineHeight": 1.25, "baseline": size,
        "autoResize": True,
    })
    return el


def build_scene(plan: dict[str, Any]) -> dict[str, Any]:
    """layout_plan から .excalidraw シーン (dict) を生成する。"""
    elements: list[dict[str, Any]] = []
    gap_comms = {i["community_id"] for i in plan.get("islands", []) if i.get("is_gap")}

    # 1) islands
    for island in plan.get("islands", []):
        x0, y0, x1, y1 = island["bbox"]
        is_gap = bool(island.get("is_gap"))
        color = "#868e96" if is_gap else "#495057"
        opacity = GAP_OPACITY if is_gap else 100
        elements.append(_base(
            island_element_id(island["community_id"]), "rectangle",
            x0, y0, x1 - x0, y1 - y0,
            strokeColor=color, strokeStyle="dashed" if is_gap else "solid",
            opacity=opacity))
        elements.append(_text(
            island_label_id(island["community_id"]), x0 + 10, y0 + 8,
            ("❓ " if is_gap else "") + island["name"], 16, color, opacity))

    # 2) nodes (ラベルは bound text として別要素にする)
    for node in plan["nodes"]:
        style = node.get("style", {})
        in_gap = node["community_id"] in gap_comms
        nid = node_element_id(node["id"])
        tid = f"{nid}-text"
        w = node["size"]
        h = node.get("height", max(60.0, node["size"] * NODE_HEIGHT_RATIO))
        elements.append(_base(
            nid, "ellipse", node["x"], node["y"], w, h,
            strokeColor=style.get("strokeColor", "#1e1e1e"),
            backgroundColor=style.get("backgroundColor", "#fff9db"),
            strokeStyle="dashed" if in_gap else "solid",
            roughness=2 if style.get("rough", True) else 0,
            opacity=GAP_OPACITY if in_gap else 100,
            boundElements=[{"id": tid, "type": "text"}]))
        t = _text(tid, node["x"] + 8, node["y"] + h / 2 - 10, node["label"], 14,
                  "#1e1e1e", GAP_OPACITY if in_gap else 100, container=nid)
        t["width"] = w - 16
        elements.append(t)

    # 3) edges (ノード中心を結ぶ矢印 + バインド)
    centers = {
        n["id"]: (n["x"] + n["size"] / 2,
                  n["y"] + n.get("height", max(60.0, n["size"] * NODE_HEIGHT_RATIO)) / 2)
        for n in plan["nodes"]
    }
    by_id = {e["id"]: e for e in elements}
    for edge in plan.get("edges", []):
        g = GLYPH_STYLES[edge["glyph"]]
        eid = edge_element_id(edge["id"])
        sx, sy = centers[edge["from"]]
        ex, ey = centers[edge["to"]]
        arrow = _base(eid, "arrow", sx, sy, ex - sx, ey - sy,
                      strokeColor=g["strokeColor"], strokeStyle=g["strokeStyle"],
                      strokeWidth=g["strokeWidth"], opacity=g["opacity"],
                      roundness={"type": 2})
        arrow.update({
            "points": [[0, 0], [ex - sx, ey - sy]],
            "lastCommittedPoint": None,
            "startBinding": {"elementId": node_element_id(edge["from"]),
                             "focus": 0, "gap": 4},
            "endBinding": {"elementId": node_element_id(edge["to"]),
                           "focus": 0, "gap": 4},
            "startArrowhead": None,
            "endArrowhead": g["endArrowhead"],
            "elbowed": False,
        })
        label = f"{g['label_prefix']}{edge.get('label', '')}".strip()
        if label:
            tid = f"{eid}-text"
            arrow["boundElements"] = [{"id": tid, "type": "text"}]
        elements.append(arrow)
        if label:
            t = _text(tid, (sx + ex) / 2, (sy + ey) / 2, label, 12,
                      g["strokeColor"], g["opacity"], container=eid)
            elements.append(t)
        # ノード側にも binding を記録
        for endpoint in (edge["from"], edge["to"]):
            n_el = by_id.get(node_element_id(endpoint))
            if n_el is not None:
                n_el["boundElements"].append({"id": eid, "type": "arrow"})

    return {
        "type": "excalidraw",
        "version": 2,
        "source": "concept-cartographer",
        "elements": elements,
        "appState": {"viewBackgroundColor": "#ffffff", "gridSize": None},
        "files": {},
    }


def write_scene(plan: dict[str, Any], path: str) -> str:
    scene = build_scene(plan)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(scene, f, ensure_ascii=False, indent=2)
    return path
