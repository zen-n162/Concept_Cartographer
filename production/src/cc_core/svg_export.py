"""layout_plan -> SVG のヘッドレス書き出し (実運用計画 §8)。

PoC の PNG/SVG 出力は Excalidraw の canvas API に依存し、ブラウザ接続が
無いと失敗する【実測】。実運用ではサーバ側で完結する必要があるため、
layout_plan から直接 SVG を描く。

描画規則は cc_core.adapter / excalidraw_file と同一に保つ:
  - glyph 配色・破線・半透明 (ギャップと対立候補は非断定表示)
  - 集約ノードは別配色で「畳まれている」ことを示す
  - エッジラベルは cc_core.overlap の一括プランナーが決めた位置
    (adapter と同じ関数を呼ぶので canvas と SVG で位置が一致する)
手描き風の揺らぎは SVG フィルタ (feTurbulence) で近似する。
"""

from __future__ import annotations

import html
import zlib
from pathlib import Path
from typing import Any

from cc_core.adapter import GAP_OPACITY, GLYPH_STYLES, NODE_HEIGHT_RATIO
from cc_core.layout import EDGE_FONT, NODE_FONT
from cc_core.logging_util import get_logger
from cc_core.overlap import plan_label_layout
from cc_core.textmetrics import wrap_to_lines

logger = get_logger("cc_core.svg_export")

MARGIN = 40
FONT_STACK = "'Hiragino Maru Gothic ProN','Hiragino Sans','Yu Gothic',sans-serif"
AGGREGATE_FILL = "#e7f5ff"
AGGREGATE_STROKE = "#1971c2"


def _esc(text: str) -> str:
    return html.escape(str(text), quote=True)


def _node_h(node: dict[str, Any]) -> float:
    return float(node.get("height", max(60.0, node["size"] * NODE_HEIGHT_RATIO)))


def _bounds(plan: dict[str, Any]) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for isl in plan.get("islands", []):
        x0, y0, x1, y1 = isl["bbox"]
        xs += [x0, x1]
        ys += [y0, y1]
    for n in plan["nodes"]:
        xs += [n["x"], n["x"] + n["size"]]
        ys += [n["y"], n["y"] + _node_h(n)]
    if not xs:
        return 0.0, 0.0, 800.0, 600.0
    return min(xs), min(ys), max(xs), max(ys)


def _dash(style: str, width: float) -> str:
    if style == "dashed":
        return f' stroke-dasharray="{width * 3:.0f} {width * 2.4:.0f}"'
    if style == "dotted":
        return f' stroke-dasharray="{width:.0f} {width * 1.8:.0f}"'
    return ""


def build_svg(plan: dict[str, Any], *, rough: bool = True) -> str:
    """layout_plan から SVG 文字列を生成する (決定的)。"""
    x0, y0, x1, y1 = _bounds(plan)
    w = x1 - x0 + MARGIN * 2
    h = y1 - y0 + MARGIN * 2
    ox, oy = MARGIN - x0, MARGIN - y0

    nodes = {n["id"]: n for n in plan["nodes"]}
    gap_comms = {i["community_id"] for i in plan.get("islands", []) if i.get("is_gap")}
    # ラベル位置は adapter (canvas) と同じ一括プランナーから取る (設計書 §2)
    placements = plan_label_layout(plan)
    parts: list[str] = []

    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}" '
        f'viewBox="0 0 {w:.0f} {h:.0f}" font-family="{FONT_STACK}">'
    )
    parts.append("<defs>")
    if rough:
        # 手描き風の揺らぎ (Excalidraw の rough.js を SVG フィルタで近似)
        parts.append(
            '<filter id="rough" x="-3%" y="-3%" width="106%" height="106%">'
            '<feTurbulence type="fractalNoise" baseFrequency="0.03" numOctaves="2" '
            'seed="7" result="n"/>'
            '<feDisplacementMap in="SourceGraphic" in2="n" scale="2.2"/></filter>'
        )
    for name, style in GLYPH_STYLES.items():
        if style["endArrowhead"]:
            parts.append(
                f'<marker id="ah-{name}" viewBox="0 0 10 10" refX="9" refY="5" '
                f'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
                f'<path d="M0 0 L10 5 L0 10 z" fill="{style["strokeColor"]}"/></marker>'
            )
    parts.append("</defs>")
    parts.append(f'<rect width="{w:.0f}" height="{h:.0f}" fill="#ffffff"/>')

    grp = ' filter="url(#rough)"' if rough else ""

    # --- 1) islands ---
    for isl in plan.get("islands", []):
        ix0, iy0, ix1, iy1 = isl["bbox"]
        is_gap = bool(isl.get("is_gap"))
        color = "#868e96" if is_gap else "#495057"
        op = GAP_OPACITY / 100 if is_gap else 1.0
        parts.append(
            f'<g data-island-id="{_esc(isl["community_id"])}"{grp}>'
            f'<rect x="{ix0 + ox:.0f}" y="{iy0 + oy:.0f}" '
            f'width="{ix1 - ix0:.0f}" height="{iy1 - iy0:.0f}" rx="12" fill="none" '
            f'stroke="{color}" stroke-width="1" opacity="{op:.2f}"'
            f'{_dash("dashed" if is_gap else "solid", 1)}/></g>'
        )
        label = ("❓ " if is_gap else "") + isl["name"]
        parts.append(
            f'<text x="{ix0 + ox + 10:.0f}" y="{iy0 + oy + 22:.0f}" font-size="16" '
            f'fill="{color}" opacity="{op:.2f}">{_esc(label)}</text>'
        )

    # --- 2) edges (ノードより先に描いて背面へ) ---
    for edge in plan.get("edges", []):
        a, b = nodes.get(edge["from"]), nodes.get(edge["to"])
        if not a or not b:
            continue
        g = GLYPH_STYLES.get(edge["glyph"], GLYPH_STYLES["arrow"])
        ax, ay = a["x"] + a["size"] / 2 + ox, a["y"] + _node_h(a) / 2 + oy
        bx, by = b["x"] + b["size"] / 2 + ox, b["y"] + _node_h(b) / 2 + oy
        op = g["opacity"] / 100
        marker = f' marker-end="url(#ah-{edge["glyph"]})"' if g["endArrowhead"] else ""
        # data-edge-id / class は Web UI のクリック委譲 (根拠ポップオーバー) 用。
        # 線とラベルの両方に付ける — 細い線は当たり判定が小さく、実際には
        # ラベルを押されることが多いため。
        eid = f' data-edge-id="{_esc(edge["id"])}" class="cc-edge"'
        # ユーザーが編集/追加した関係は UI でバッジを出す (編集/学習設計書 §8.2)
        if str(edge.get("origin") or "").startswith("user"):
            eid += ' data-origin="user"'
        parts.append(
            f'<g{eid}{grp}><line x1="{ax:.0f}" y1="{ay:.0f}" x2="{bx:.0f}" '
            f'y2="{by:.0f}" '
            f'stroke="{g["strokeColor"]}" stroke-width="{g["strokeWidth"]}" '
            f'opacity="{op:.2f}"{_dash(g["strokeStyle"], g["strokeWidth"])}{marker}/></g>'
        )
        placement = placements.get(edge["id"])
        # プランナーが短縮したときは短縮後の文字列を描く (裁定 AC)
        raw = placement.text if placement is not None else edge.get("label", "")
        label = f'{g["label_prefix"]}{raw}'.strip()
        if not label:
            continue
        if placement is None:
            lx, ly = (ax + bx) / 2, (ay + by) / 2
        else:
            lx, ly = placement.x + ox, placement.y + oy
        parts.append(
            f'<text{eid} x="{lx:.0f}" y="{ly:.0f}" font-size="{EDGE_FONT}" '
            f'text-anchor="middle" fill="{g["strokeColor"]}" opacity="{op:.2f}" '
            f'paint-order="stroke" stroke="#ffffff" stroke-width="3" '
            f'stroke-linejoin="round">{_esc(label)}</text>'
        )

    # --- 3) nodes ---
    for node in plan["nodes"]:
        nh = _node_h(node)
        cx, cy = node["x"] + node["size"] / 2 + ox, node["y"] + nh / 2 + oy
        in_gap = node["community_id"] in gap_comms
        is_agg = node.get("kind") == "aggregate"
        style = node.get("style", {})
        fill = style.get("backgroundColor",
                         AGGREGATE_FILL if is_agg else "#fff9db")
        stroke = style.get("strokeColor",
                           AGGREGATE_STROKE if is_agg else "#1e1e1e")
        op = GAP_OPACITY / 100 if in_gap else 1.0
        # 集約ノードは Web UI で展開 (ドリルダウン) の対象になるため
        # data-aggregate-id も付ける。ラベル文字にも同じ属性を付けるのは、
        # 文字を押されたときにノードとして拾えるようにするため。
        nid = (f' data-node-id="{_esc(node["id"])}" class="cc-node" '
               f'data-kind="{"aggregate" if is_agg else "concept"}"')
        if is_agg:
            nid += f' data-aggregate-id="{_esc(node.get("aggregate_id", node["id"]))}"'
        if str(node.get("origin") or "").startswith("user"):
            nid += ' data-origin="user"'
        parts.append(
            f'<g{nid}{grp}><ellipse cx="{cx:.0f}" cy="{cy:.0f}" '
            f'rx="{node["size"] / 2:.0f}" '
            f'ry="{nh / 2:.0f}" fill="{fill}" stroke="{stroke}" stroke-width="1.4" '
            f'opacity="{op:.2f}"{_dash("dashed" if in_gap or is_agg else "solid", 1.4)}'
            f'/></g>'
        )
        # ラベルは楕円の内接幅に合わせて折り返す (レイアウトと同じ規則)
        max_em = (node["size"] * 0.58) / NODE_FONT
        lines = wrap_to_lines(node["label"], max_em, max_lines=3)
        start = cy - (len(lines) - 1) * NODE_FONT * 0.62
        for i, line in enumerate(lines):
            parts.append(
                f'<text{nid} x="{cx:.0f}" '
                f'y="{start + i * NODE_FONT * 1.25 + 5:.0f}" '
                f'font-size="{NODE_FONT}" text-anchor="middle" fill="#1e1e1e" '
                f'opacity="{op:.2f}">{_esc(line)}</text>'
            )

    parts.append("</svg>")
    svg = "".join(parts)
    logger.info("svg built nodes=%d edges=%d size=%.0fx%.0f",
                len(plan["nodes"]), len(plan.get("edges", [])), w, h)
    return svg


def write_svg(plan: dict[str, Any], path: str | Path, *, rough: bool = True) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_svg(plan, rough=rough), encoding="utf-8")
    return out
