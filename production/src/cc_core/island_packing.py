"""レイアウト v3 — 島どうしの配置 (バッチ L2)。

設計書: docs/layout-v3-design.md §3 (メタ KK + 接触候補点パッキング) と
§3a (レベル間アンカー)。

L1 は島を「シェルフ」(左から並べて折り返す) に置いていた。これだと
島の並びが `community_id` の登場順だけで決まり、**関係の強い島どうしが
隣に来ない**うえ、レベルを切り替えると島の相対方位が入れ替わる。

このモジュールは 2 段構えでそれを直す:

  1. **メタ KK** — 頂点 = 島、辺 = 島間エッジが 1 本でもある組。KK が
     「関係の強い島は近く」という目標座標を決める
  2. **接触候補点パッキング** — 目標座標はふつう重なるので、面積の大きい
     島から順に「既に置いた島の 4 辺にぴったり接する位置」へ寄せて詰める。
     目標からの距離が最小の候補を採るので、KK の意味的な方位が残る

レベル間アンカー (§3a) は detailed の配置を基準方位として渡す仕組み。
detailed → standard → overview の順に計算し、島の相対方位を保つ。

決定性 (憲法): 乱数なし。走査順は community_id の辞書順、面積降順の
タイブレークも id 順、候補の同点は (y, x) → 候補 index で決める。
"""

from __future__ import annotations

import math
from typing import Any, Iterable, NamedTuple

from cc_core.layout import ISLAND_GAP_X, ISLAND_GAP_Y, ORIGIN_X, ORIGIN_Y
from cc_core.logging_util import get_logger

logger = get_logger("cc_core.island_packing")

SPIRAL_STEP = 32.0        # §3 の保険: 候補が全滅したときに動かす刻み
SPIRAL_MAX_STEPS = 4096   # 保険の保険 (無限ループを作らない)
_EPS = 1e-9


class Island(NamedTuple):
    """パッキングに必要な島の情報だけを持つ (中身のノードには関知しない)。"""

    cid: str
    width: float
    height: float

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def diag(self) -> float:
        return math.hypot(self.width, self.height)


# --------------------------------------------------------------------------
# §3-1,2 メタ KK と目標座標
# --------------------------------------------------------------------------

def _meta_kk(order: list[str],
             links: set[tuple[str, str]]) -> list[tuple[float, float]] | None:
    """島グラフの KK 座標。孤立頂点もそのまま渡す (KK が外周へ置く)。"""
    try:
        import igraph as ig
    except Exception as exc:  # pragma: no cover - 環境依存の退避路
        logger.warning("igraph unavailable (%s); island targets fall back to a shelf",
                       exc)
        return None

    index = {cid: i for i, cid in enumerate(order)}
    pairs = sorted({(min(index[a], index[b]), max(index[a], index[b]))
                    for a, b in links if a != b and a in index and b in index})
    try:
        graph = ig.Graph(n=len(order), edges=[list(p) for p in pairs])
        layout = graph.layout_kamada_kawai()
    except Exception as exc:  # pragma: no cover - 環境依存の退避路
        logger.warning("meta kamada_kawai failed (%s); shelf targets", exc)
        return None
    return [(float(p[0]), float(p[1])) for p in layout]


def _shelf_targets(islands: dict[str, Island],
                   order: list[str]) -> dict[str, tuple[float, float]]:
    """KK が使えないときの目標座標 (L1 と同じ格子)。決定的であればよい。"""
    per_row = max(1, math.ceil(math.sqrt(len(order))))
    step_x = max((islands[c].width for c in order), default=1.0) + ISLAND_GAP_X
    step_y = max((islands[c].height for c in order), default=1.0) + ISLAND_GAP_Y
    return {cid: ((i % per_row) * step_x, (i // per_row) * step_y)
            for i, cid in enumerate(order)}


def _kk_targets(islands: dict[str, Island],
                order: list[str],
                links: set[tuple[str, str]]) -> dict[str, tuple[float, float]]:
    """§3-2: KK 座標を「全島の面積合計と同じ広さ」へ正規化した目標位置。

    **設計書からの逸脱 (検収で確定)**: 式のとおり `対角長合計 / 広がり` にすると
    目標空間の一辺が「全島を 1 列に並べた長さ」になり、2 次元では √n 倍広い。
    島どうしが最初から離れているのでパッキング (§3-3) が一度も働かず、地図が
    間延びする (実測: kg_sample 4312×2662px、400 ノード 59272×49293px)。
    目標空間の一辺 = √(島面積合計) がいちばん詰まる: 目標は重なるくらい近く、
    実際の間隔は ISLAND_GAP インフレート付きの接触パッキングが保証するので、
    近すぎて壊れることはない (実測: 同 2489×1485px / 13244×15219px。
    √n 補正案よりさらに 4 割狭く、重なりゼロは維持)。
    """
    if len(order) == 1:
        return {order[0]: (0.0, 0.0)}

    coords = _meta_kk(order, links)
    if coords is None:
        return _shelf_targets(islands, order)

    span_x = max(p[0] for p in coords) - min(p[0] for p in coords)
    span_y = max(p[1] for p in coords) - min(p[1] for p in coords)
    extent = max(span_x, span_y)
    if extent < _EPS:                      # 全島が同じ点 (辺 0 本の 1 頂点等)
        return _shelf_targets(islands, order)
    area_sum = sum(islands[c].width * islands[c].height for c in order)
    scale = math.sqrt(area_sum) / extent
    return {cid: (coords[i][0] * scale, coords[i][1] * scale)
            for i, cid in enumerate(order)}


# --------------------------------------------------------------------------
# §3a レベル間アンカー
# --------------------------------------------------------------------------

def anchors_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """detailed の plan から「島の相対方位」を取り出す (§3a)。

    `anchor[cid] = 島中心 − 全体重心`。全体重心は島中心の平均 (面積で重み付け
    しない — 小さい島も方位の基準としては対等に扱う)。
    """
    boxes = [(i["community_id"], i["bbox"]) for i in plan.get("islands", [])]
    if not boxes:
        return {"offsets": {}, "area": 0.0}
    centers = {cid: ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0) for cid, b in boxes}
    gx = sum(c[0] for c in centers.values()) / len(centers)
    gy = sum(c[1] for c in centers.values()) / len(centers)
    area = sum((b[2] - b[0]) * (b[3] - b[1]) for _cid, b in boxes)
    return {"offsets": {cid: (c[0] - gx, c[1] - gy) for cid, c in centers.items()},
            "area": float(area)}


def _anchor_targets(islands: dict[str, Island],
                    order: list[str],
                    anchors: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """§3a: 目標位置 = anchor × √(このレベルの島面積合計 / detailed の合計)。

    重心は原点に置く (最後に全体を ORIGIN へ平行移動するので絶対位置は無関係)。
    detailed に無かった島 (想定外の新島) は重心 = (0, 0) を目標にする。
    """
    offsets: dict[str, tuple[float, float]] = anchors.get("offsets") or {}
    base_area = float(anchors.get("area") or 0.0)
    area = sum(islands[c].area for c in order)
    ratio = math.sqrt(area / base_area) if base_area > _EPS else 1.0
    return {cid: ((offsets[cid][0] * ratio, offsets[cid][1] * ratio)
                  if cid in offsets else (0.0, 0.0))
            for cid in order}


# --------------------------------------------------------------------------
# §3-3 接触候補点パッキング
# --------------------------------------------------------------------------

def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _overlaps(a: tuple[float, float, float, float],
              b: tuple[float, float, float, float]) -> bool:
    return not (a[2] <= b[0] + _EPS or b[2] <= a[0] + _EPS
                or a[3] <= b[1] + _EPS or b[3] <= a[1] + _EPS)


def _candidates(target: tuple[float, float], w: float, h: float,
                placed: list[tuple[float, float, float, float]]
                ) -> list[tuple[float, float]]:
    """目標位置 + 配置済み矩形の 4 辺にフラッシュ接触する位置 (§3-3)。

    `placed` は ISLAND_GAP_X/Y でインフレート済みの矩形。接触方向と直交する
    軸は「接触が成立する区間」へ目標を clamp する — こうすると目標に一番近い
    「ぴったり隣」が候補に必ず入る。
    """
    out: list[tuple[float, float]] = [target]
    tx, ty = target
    for x0, y0, x1, y1 in placed:
        vy = _clamp(ty, y0 - h, y1)     # 縦に重なりが残る区間
        vx = _clamp(tx, x0 - w, x1)     # 横に重なりが残る区間
        out.append((x1, vy))            # 東 (右にぴったり)
        out.append((vx, y1))            # 南
        out.append((x0 - w, vy))        # 西
        out.append((vx, y0 - h))        # 北
    return out


def _spiral(target: tuple[float, float], w: float, h: float,
            placed: list[tuple[float, float, float, float]]
            ) -> tuple[float, float]:
    """保険: 32px 刻みで E→S→W→N へ広げながら空きを探す (§3-3)。

    候補点が全滅する形 (完全に囲まれた目標) でも必ず終わるように、方向ごとに
    距離を伸ばす十字スパイラルにしてある。島の総幅は有限なので東へ伸ばせば
    必ず空く。
    """
    tx, ty = target
    for step in range(1, SPIRAL_MAX_STEPS + 1):
        d = step * SPIRAL_STEP
        for dx, dy in ((d, 0.0), (0.0, d), (-d, 0.0), (0.0, -d)):
            cand = (tx + dx, ty + dy)
            rect = (cand[0], cand[1], cand[0] + w, cand[1] + h)
            if not any(_overlaps(rect, p) for p in placed):
                return cand
    # pragma: no cover - 到達不能 (東へ伸ばせば必ず空く)
    logger.warning("island packing spiral exhausted; keeping the target position")
    return target


def pack_islands(islands: Iterable[Island],
                 links: Iterable[tuple[str, str]] | None = None,
                 anchors: dict[str, Any] | None = None,
                 ) -> dict[str, tuple[int, int]]:
    """島の左上座標を決める (§3)。返り値は community_id → (x, y) の整数座標。

    - `links`: 島間エッジがある組 (向きは問わない)
    - `anchors`: detailed 由来の方位 (§3a)。None ならメタ KK で目標を作る
    """
    items = list(islands)
    if not items:
        return {}
    by_cid = {i.cid: i for i in items}
    order = sorted(by_cid)                      # 正準順 = community_id 昇順

    if anchors:
        targets = _anchor_targets(by_cid, order, anchors)
    else:
        targets = _kk_targets(by_cid, order, set(links or ()))

    # 面積降順 (同点は community_id 順) に詰める
    placing = sorted(order, key=lambda c: (-by_cid[c].area, c))

    placed: list[tuple[float, float, float, float]] = []   # インフレート済み
    pos: dict[str, tuple[float, float]] = {}
    for cid in placing:
        w, h = by_cid[cid].width, by_cid[cid].height
        tcx, tcy = targets[cid]
        target = (tcx - w / 2.0, tcy - h / 2.0)             # 左上に直す

        best_key: tuple[float, float, float, int] | None = None
        best: tuple[float, float] | None = None
        for idx, (cx, cy) in enumerate(_candidates(target, w, h, placed)):
            rect = (cx, cy, cx + w, cy + h)
            if any(_overlaps(rect, p) for p in placed):
                continue
            d2 = (cx + w / 2.0 - tcx) ** 2 + (cy + h / 2.0 - tcy) ** 2
            key = (d2, cy, cx, idx)
            if best_key is None or key < best_key:
                best_key, best = key, (cx, cy)
        if best is None:
            best = _spiral(target, w, h, placed)

        pos[cid] = best
        placed.append((best[0] - ISLAND_GAP_X, best[1] - ISLAND_GAP_Y,
                       best[0] + w + ISLAND_GAP_X, best[1] + h + ISLAND_GAP_Y))

    # §3-4: 全体を (ORIGIN_X, ORIGIN_Y) へ寄せる
    min_x = min(p[0] for p in pos.values())
    min_y = min(p[1] for p in pos.values())
    return {cid: (round(p[0] - min_x + ORIGIN_X), round(p[1] - min_y + ORIGIN_Y))
            for cid, p in pos.items()}
