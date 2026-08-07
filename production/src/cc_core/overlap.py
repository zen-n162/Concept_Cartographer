"""レイアウトの重なり検査とエッジラベルの一括配置プランナー。

「文字が読めない」は主観に見えるが、原因は幾何的に特定できる:
  - エッジラベルがノードに重なる (Excalidraw はラベルを両端の中点に置く)
  - エッジラベルどうしが重なる
  - ノードどうしが重なる
  - ノードが島の枠からはみ出す
これらを layout_plan の座標だけで判定し、描画前に検出できるようにする。

【v2 / 2026-08-07】ラベル配置をエッジ単位の逐次判断から**ビュー全体の
決定的プランナー** `plan_label_layout` に昇格した (設計書 裁定 AA)。
逐次判断だと「自分より後に置かれるラベル」を知らないため、ラベル同士が
重なる。障害物を 3 種 (ノード楕円 / 島タイトル帯 / 配置済みラベル) に揃え、
制約の強いラベルから順に置くことで、同じ入力からは常に同じ配置が出る。

ノード座標には一切手を入れない (裁定 AB)。解決はラベル配置だけで行う。
全候補が塞がっていたら短縮 → 最少交差位置 + `unresolved` 報告 (裁定 AC)。
"""

from __future__ import annotations

import hashlib
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterator

from cc_core.layout import (
    EDGE_FONT,
    GLYPH_PREFIX_EM,
    LINE_H,
    NODE_H_MIN,
)
from cc_core.textmetrics import display_width, truncate

Rect = tuple[float, float, float, float]  # (x0, y0, x1, y1)

# 楕円は外接矩形より内側なので、当たり判定を少し縮める
ELLIPSE_SHRINK = 0.95  # 楕円の縁への接触も検出できるよう厳しめに取る

# --- プランナーの定数 (設計書 §1 の候補列) ---
LABEL_H = EDGE_FONT * LINE_H     # ラベル高 h = 15.0px
ISLAND_TITLE_BAND = 28.0         # 島 bbox 上端から 28px はタイトルの領域
SIDE_MARGIN = 12.0               # ノードの脇に逃がすときの隙間
# 候補列 1: 線分上の位置 t × 法線方向オフセット (単位はラベル高 h)。
# Cartesian product は t を外側に回す = 中点を保ったまま法線方向へ逃げるのを
# 先に試す (従来の resolve_label_offset の挙動に近い順序)。
T_STEPS = (0.5, 0.38, 0.62, 0.26, 0.74)
NORMAL_STEPS = (0.0, 1.1, -1.1, 2.2, -2.2)
# 候補列 3: truncate の段階。元の表示幅に対する割合。
TRUNCATE_STEPS = (0.9, 0.8, 0.7, 0.6)
TRUNCATE_MIN_RATIO = 0.6         # 40% を超えて縮めない (裁定 AC)

_GRID_CELL = 128.0               # 障害物の空間ハッシュのセル幅


@dataclass
class OverlapReport:
    label_on_node: list[dict[str, Any]] = field(default_factory=list)
    label_on_label: list[dict[str, Any]] = field(default_factory=list)
    node_on_node: list[dict[str, Any]] = field(default_factory=list)
    node_outside_island: list[str] = field(default_factory=list)
    unresolved_labels: list[dict[str, Any]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.label_on_node or self.label_on_label
                    or self.node_on_node or self.node_outside_island)

    def to_dict(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "label_on_node": self.label_on_node,
            "label_on_label": self.label_on_label,
            "node_on_node": self.node_on_node,
            "node_outside_island": self.node_outside_island,
            "unresolved_labels": self.unresolved_labels,
        }


def _intersects(a: Rect, b: Rect) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _intersect_area(a: Rect, b: Rect) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def node_rect(node: dict[str, Any], shrink: float = ELLIPSE_SHRINK) -> Rect:
    w = node["size"] * shrink
    h = node.get("height", max(NODE_H_MIN, node["size"] * 0.55)) * shrink
    cx = node["x"] + node["size"] / 2
    cy = node["y"] + node.get("height", max(NODE_H_MIN, node["size"] * 0.55)) / 2
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def _node_height(node: dict[str, Any]) -> float:
    return float(node.get("height", max(NODE_H_MIN, node["size"] * 0.55)))


def _node_center(node: dict[str, Any]) -> tuple[float, float]:
    return node["x"] + node["size"] / 2, node["y"] + _node_height(node) / 2


def _label_size(text: str, glyph: str) -> tuple[float, float]:
    """描かれるラベル矩形の (幅, 高さ)。glyph 接頭記号の分を含む。"""
    em = display_width(text) + GLYPH_PREFIX_EM.get(glyph, 0.0)
    return em * EDGE_FONT, LABEL_H


def edge_label_rect(edge: dict[str, Any], nodes: dict[str, dict]) -> Rect | None:
    """Excalidraw がエッジラベルを描く矩形 (両端ノード中心の中点に配置される)。"""
    label = edge.get("label", "")
    if not label:
        return None
    a, b = nodes[edge["from"]], nodes[edge["to"]]
    (ax, ay), (bx, by) = _node_center(a), _node_center(b)
    mx, my = (ax + bx) / 2, (ay + by) / 2
    w, h = _label_size(label, edge["glyph"])
    return (mx - w / 2, my - h / 2, mx + w / 2, my + h / 2)


# --------------------------------------------------------------------------
# 障害物の空間索引
# --------------------------------------------------------------------------

class _Obstacles:
    """矩形障害物の一様グリッド索引。

    100〜400 ノード規模で候補を総当たりすると O(候補 × 障害物) になり
    実用にならないため、セル単位のバケットで絞り込む。
    """

    __slots__ = ("cell", "items", "buckets")

    def __init__(self, cell: float = _GRID_CELL) -> None:
        self.cell = cell
        self.items: list[tuple[Rect, str]] = []
        self.buckets: dict[tuple[int, int], list[int]] = {}

    def _cells(self, r: Rect) -> Iterator[tuple[int, int]]:
        cell = self.cell
        cx0 = math.floor(r[0] / cell)
        cx1 = math.floor((r[2] - 1e-9) / cell)
        cy0 = math.floor(r[1] / cell)
        cy1 = math.floor((r[3] - 1e-9) / cell)
        for cx in range(int(cx0), int(cx1) + 1):
            for cy in range(int(cy0), int(cy1) + 1):
                yield (cx, cy)

    def add(self, rect: Rect, tag: str) -> None:
        idx = len(self.items)
        self.items.append((rect, tag))
        for c in self._cells(rect):
            self.buckets.setdefault(c, []).append(idx)

    def _nearby(self, rect: Rect) -> set[int]:
        out: set[int] = set()
        for c in self._cells(rect):
            hit = self.buckets.get(c)
            if hit:
                out.update(hit)
        return out

    def penalty(self, rect: Rect) -> float:
        """重なり面積の合計。0 なら衝突なし。"""
        items = self.items
        return sum(_intersect_area(rect, items[i][0]) for i in self._nearby(rect))

    def blockers(self, rect: Rect) -> list[str]:
        items = self.items
        return sorted({items[i][1] for i in self._nearby(rect)
                       if _intersects(rect, items[i][0])})


def _static_obstacles(view: dict[str, Any]) -> _Obstacles:
    """ノード楕円 + 島タイトル帯 (裁定 AA の障害物 1・2)。"""
    obs = _Obstacles()
    for n in view.get("nodes", []):
        obs.add(node_rect(n), f"node:{n['id']}")
    for isl in view.get("islands", []):
        bbox = isl.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        x0, y0, x1, _y1 = bbox
        obs.add((float(x0), float(y0), float(x1), float(y0) + ISLAND_TITLE_BAND),
                f"island:{isl.get('community_id', '?')}")
    return obs


# --------------------------------------------------------------------------
# 一括プランナー (裁定 AA)
# --------------------------------------------------------------------------

@dataclass
class LabelPlacement:
    """1 本のエッジラベルの確定位置。x/y はラベル矩形の**中心**。"""

    x: float
    y: float
    width: float
    height: float
    text: str                        # 実際に描く文字列 (glyph 接頭辞は含まない)
    truncated: str | None = None     # 短縮したときの文字列 (しなければ None)
    retreated: bool = False          # 自然な中点から動かしたか
    unresolved: bool = False         # 逃げ場が無く最少交差で妥協したか
    blocked_by: list[str] = field(default_factory=list)

    @property
    def rect(self) -> Rect:
        return (self.x - self.width / 2, self.y - self.height / 2,
                self.x + self.width / 2, self.y + self.height / 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "x": self.x, "y": self.y,
            "width": self.width, "height": self.height,
            "text": self.text,
            "truncated": self.truncated,
            "retreated": self.retreated,
            "unresolved": self.unresolved,
            "blocked_by": self.blocked_by,
        }


def _text_variants(text: str) -> Iterator[str]:
    """元の文字列 → 段階的に短縮した文字列 (裁定 AC の候補列 3)。

    40% を超えて縮む候補は使わない。`truncate` は末尾に … を足すので、
    刻みによっては上限を割る文字列が出る — その候補は捨てる。
    """
    yield text
    base = display_width(text)
    if base <= 0:
        return
    seen = {text}
    floor = base * TRUNCATE_MIN_RATIO
    for ratio in TRUNCATE_STEPS:
        cand = truncate(text, base * ratio)
        if cand in seen or display_width(cand) < floor:
            continue
        seen.add(cand)
        yield cand


def _candidates(edge: dict[str, Any], nodes: dict[str, dict],
                w: float, h: float) -> list[tuple[float, float]]:
    """設計書 §1 の候補列 1 (線分上) と 2 (ノードの脇) を順に返す。"""
    a, b = nodes[edge["from"]], nodes[edge["to"]]
    (ax, ay), (bx, by) = _node_center(a), _node_center(b)
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    if length < 1e-9:            # 自己ループ等の退化: 法線を真上に取る
        px, py = 0.0, -1.0
    else:
        px, py = -dy / length, dx / length

    out: list[tuple[float, float]] = []
    # 1) 線分上 t × 法線方向オフセット
    for t in T_STEPS:
        mx, my = ax + dx * t, ay + dy * t
        for k in NORMAL_STEPS:
            out.append((mx + px * k * h, my + py * k * h))
    # 2) from/to ノードの脇 (楕円の右端 + マージン / 左端 − ラベル幅 − マージン)
    for n in (a, b):
        nh = _node_height(n)
        cy = n["y"] + nh / 2
        out.append((n["x"] + n["size"] + SIDE_MARGIN + w / 2, cy))
        out.append((n["x"] - SIDE_MARGIN - w / 2, cy))
    return out


def _place_one(edge: dict[str, Any], nodes: dict[str, dict],
               static: _Obstacles, placed: _Obstacles) -> LabelPlacement:
    original = edge.get("label", "")
    glyph = edge.get("glyph", "arrow")
    best: tuple[float, int, float, float, str, float, float] | None = None
    idx = 0

    for variant in _text_variants(original):
        w, h = _label_size(variant, glyph)
        for cx, cy in _candidates(edge, nodes, w, h):
            rect = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
            pen = static.penalty(rect) + placed.penalty(rect)
            if pen <= 0.0:
                return LabelPlacement(
                    x=cx, y=cy, width=w, height=h, text=variant,
                    truncated=None if variant == original else variant,
                    # idx == 0 = 元の文字列を自然な中点に置いた状態 =
                    # Excalidraw の bound text をそのまま使える
                    retreated=idx != 0,
                )
            if best is None or pen < best[0]:
                best = (pen, idx, cx, cy, variant, w, h)
            idx += 1

    # 全候補が塞がっている: 最少交差の位置に置いたうえで報告する (裁定 AC)
    assert best is not None  # 候補列は常に非空
    pen, _, cx, cy, variant, w, h = best
    rect = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
    return LabelPlacement(
        x=cx, y=cy, width=w, height=h, text=variant,
        truncated=None if variant == original else variant,
        retreated=True, unresolved=True,
        blocked_by=sorted(set(static.blockers(rect) + placed.blockers(rect))),
    )


def plan_label_layout(view: dict[str, Any]) -> dict[str, LabelPlacement]:
    """ビュー全体のエッジラベル配置を一括で決める (裁定 AA)。

    同じ入力からは必ず同じ結果が出る:
      - 処理順は「静的障害物に対して空いている候補が少ないラベル」から。
        同点はエッジ id の辞書順
      - 候補列・障害物の並びも固定
    """
    cached = _cache_get(view)
    if cached is not None:
        return cached

    nodes = {n["id"]: n for n in view.get("nodes", [])}
    edges = [e for e in view.get("edges", [])
             if e.get("label") and e.get("from") in nodes and e.get("to") in nodes]

    static = _static_obstacles(view)

    # --- 1 パス目: 制約の強さ (静的障害物に対する空き候補数) を測る ---
    freedom: dict[str, int] = {}
    for e in edges:
        w, h = _label_size(e["label"], e.get("glyph", "arrow"))
        free = 0
        for cx, cy in _candidates(e, nodes, w, h):
            if static.penalty((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)) <= 0.0:
                free += 1
        freedom[e["id"]] = free

    order = sorted(edges, key=lambda e: (freedom[e["id"]], str(e["id"])))

    # --- 2 パス目: 制約の強い順に確定し、確定したものを障害物に足す ---
    placed = _Obstacles()
    result: dict[str, LabelPlacement] = {}
    for e in order:
        pl = _place_one(e, nodes, static, placed)
        result[e["id"]] = pl
        placed.add(pl.rect, f"label:{e['id']}")

    # 返す辞書はエッジ id 順に整える (JSON 化しても差分が出ない)
    ordered = {eid: result[eid] for eid in sorted(result)}
    _cache_put(view, ordered)
    return ordered


# --------------------------------------------------------------------------
# プランナー結果のキャッシュ
#   - adapter (canvas) と svg_export が**同じ結果**を引くための共有点
#   - resolve_label_offset の後方互換もここを経由する
# --------------------------------------------------------------------------

_CACHE_MAX = 8
_CACHE: OrderedDict[str, tuple[str, dict[str, LabelPlacement]]] = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _digest(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


def _nodes_key(nodes: dict[str, dict]) -> str:
    """ノード幾何だけから決まるキー (resolve_label_offset からも計算できる)。"""
    return _digest(";".join(
        f"{nid}:{n['x']}:{n['y']}:{n['size']}:{_node_height(n)}"
        for nid, n in sorted(nodes.items())
    ))


def _rest_key(view: dict[str, Any]) -> str:
    isl = ";".join(
        f"{i.get('community_id')}:{i.get('bbox')}" for i in view.get("islands", [])
    )
    edg = ";".join(
        f"{e.get('id')}:{e.get('from')}:{e.get('to')}:{e.get('label', '')}:"
        f"{e.get('glyph', 'arrow')}"
        for e in view.get("edges", [])
    )
    return _digest(isl + "|" + edg)


def _cache_get(view: dict[str, Any]) -> dict[str, LabelPlacement] | None:
    nodes = {n["id"]: n for n in view.get("nodes", [])}
    nk, rk = _nodes_key(nodes), _rest_key(view)
    with _CACHE_LOCK:
        hit = _CACHE.get(nk)
        if hit is not None and hit[0] == rk:
            _CACHE.move_to_end(nk)
            return hit[1]
    return None


def _cache_put(view: dict[str, Any], result: dict[str, LabelPlacement]) -> None:
    nodes = {n["id"]: n for n in view.get("nodes", [])}
    nk, rk = _nodes_key(nodes), _rest_key(view)
    with _CACHE_LOCK:
        _CACHE[nk] = (rk, result)
        _CACHE.move_to_end(nk)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)


def clear_label_plan_cache() -> None:
    """テスト用: プランナー結果のキャッシュを捨てる。"""
    with _CACHE_LOCK:
        _CACHE.clear()


def resolve_label_offset(edge: dict[str, Any], nodes: dict[str, dict],
                         max_steps: int = 6) -> tuple[float, float] | None:
    """エッジラベルを中点に置くと他ノードに重なる場合の退避位置を返す。

    中点で衝突しないなら None (= Excalidraw の bound text をそのまま使う)。

    【後方互換】同じノード集合で `plan_label_layout` を通したことがあれば、
    その結果を引いて二面 (canvas / SVG) と同じ位置を返す。プランナーを
    通していない単発呼び出しは従来どおりの逐次アルゴリズムで答える。
    """
    with _CACHE_LOCK:
        hit = _CACHE.get(_nodes_key(nodes))
    if hit is not None:
        pl = hit[1].get(edge.get("id"))
        if pl is not None:
            return (pl.x, pl.y) if pl.retreated else None

    lr = edge_label_rect(edge, nodes)
    if lr is None:
        return None
    rects = {nid: node_rect(n) for nid, n in nodes.items()}
    if not any(_intersects(lr, r) for r in rects.values()):
        return None

    a, b = nodes[edge["from"]], nodes[edge["to"]]
    (ax, ay), (bx, by) = _node_center(a), _node_center(b)
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
    """**実際に描かれる位置**での重なりを報告する。

    v1 は常に中点の矩形で測っていたため、退避後に解決済みのラベルまで
    「重なっている」と報告し (実測: 1 セッション 3 件)、逆に退避先での
    ラベル同士の衝突は見逃していた。v2 はプランナーの確定位置で測る。
    """
    report = OverlapReport()
    nodes = {n["id"]: n for n in plan["nodes"]}
    rects = {nid: node_rect(n) for nid, n in nodes.items()}
    placements = plan_label_layout(plan)

    # エッジラベル vs ノード
    # 両端ノードも障害物に含める (v1 と同じ判定。ラベルが自分の端点に
    # かぶっても読めないことに変わりはない)
    for edge in plan.get("edges", []):
        pl = placements.get(edge["id"])
        if pl is None:
            continue
        lr = pl.rect
        for nid, nr in rects.items():
            if _intersects(lr, nr):
                report.label_on_node.append({"edge": edge["id"], "node": nid})

    # エッジラベル vs エッジラベル (v2 で追加)
    label_ids = sorted(placements)
    for i in range(len(label_ids)):
        for j in range(i + 1, len(label_ids)):
            a, b = label_ids[i], label_ids[j]
            if _intersects(placements[a].rect, placements[b].rect):
                report.label_on_label.append({"a": a, "b": b})

    # 逃げ場が無かったラベル (裁定 AC: 黙って重ねない)
    for eid in label_ids:
        pl = placements[eid]
        if pl.unresolved:
            report.unresolved_labels.append({
                "edge": eid,
                "truncated": pl.truncated,
                "blocked_by": pl.blocked_by,
            })

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
        h = _node_height(n)
        if not (x0 <= n["x"] and n["x"] + n["size"] <= x1
                and y0 <= n["y"] and n["y"] + h <= y1):
            report.node_outside_island.append(nid)

    return report
