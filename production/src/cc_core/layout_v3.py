"""レイアウト v3 (semantic layout) — 島内配置 (バッチ L1)。

設計書: docs/layout-v3-design.md (§0 モジュール構成 / §1c KK / §2 スケール +
スイープ + 段階フォールバック / §6 summary)。

このモジュールは `CC_LAYOUT_ENGINE=semantic` のときだけ動く。既定 (grid) では
`cc_core.layout` の従来コードパスがそのまま走り、生成物はバイト等価のまま。

L1 の範囲:
  - 島内配置 = KK (igraph.layout_kamada_kawai)。ノード ≤3 の島は横一列
  - 抽象座標 → px = p75 スケール + 決定的な制約スイープ + 段階フォールバック
  - 島どうしの配置は**現行のシェルフ方式を流用** (メタ KK + パッキングは L2)
  - 骨格選択 (層状 / 木) は L2、サイズ係数とティントは L3

決定性の担保 (憲法):
  - 乱数・seed を一切使わない。igraph の KK は初期配置が円周固定で、同じ入力から
    同じ座標が出ることをこの venv (igraph 1.0.0) で実測済み (プロセス跨ぎ一致)
  - 走査順はすべて id の辞書順か入力順。dict の挿入順にも依存しない
"""

from __future__ import annotations

import math
import os
from typing import Any

from cc_core.layout import (
    COL_MARGIN,
    EDGE_LABEL_MAX_EM,
    ISLAND_GAP_X,
    ISLAND_GAP_Y,
    ISLAND_PAD,
    NODE_H_MIN,
    ORIGIN_X,
    ORIGIN_Y,
    _compute_layout_grid,
    edge_label_px,
    node_size,
)
from cc_core.logging_util import get_logger
from cc_core.normalize import VALID_GLYPHS
from cc_core.overlap import ELLIPSE_SHRINK
from cc_core.textmetrics import truncate

logger = get_logger("cc_core.layout_v3")

# --- エンジン選択 (L3 で既定を semantic へ倒すまでは grid) ---
ENGINE_ENV = "CC_LAYOUT_ENGINE"
ENGINE_GRID = "grid"
ENGINE_SEMANTIC = "semantic"
LAYOUT_ENGINE_ID = "cc_core.layout/3.0 semantic"

# --- §2 のパラメータ ---
ROW_MAX_NODES = 3            # これ以下の島は横一列 (§1c)
EDGE_CLEARANCE = 24.0        # 必要長に足す余白
NODE_MARGIN = 16.0           # ノード矩形どうしに空ける最小の隙間
SCALE_MIN = 80.0             # スケールの下限
SCALE_MEDIAN_FACTOR = 3.0    # スケールの上限 = 3 × median
SCALE_NO_EDGE = 240.0        # エッジ 0 本の島
SWEEP_MAX_PASSES = 30        # スイープの上限パス数
SCALE_RETRY_FACTOR = 1.15    # 解けないときのスケール増分
SCALE_RETRY_MAX = 3          # 増分の回数
_EPS = 1e-6
_PUSH_EPS = 0.5              # 押し出しに足す余裕 (round() 後も余白を残す)

# island["layout_mode"] の値。grid エンジンでは**このキー自体を付けない**
# (既定の生成物をバイト等価に保つため)。
MODE_SEMANTIC = "semantic"
MODE_GRID_FALLBACK = "grid_fallback"

_NODE_RESERVED = ("id", "label", "x", "y", "size", "height", "community_id", "style")


# --------------------------------------------------------------------------
# エンジン選択
# --------------------------------------------------------------------------

def engine_name() -> str:
    """`CC_LAYOUT_ENGINE` を**呼び出し時に**読む (テストで monkeypatch できる)。"""
    raw = (os.environ.get(ENGINE_ENV) or ENGINE_GRID).strip().lower()
    return ENGINE_SEMANTIC if raw == ENGINE_SEMANTIC else ENGINE_GRID


def semantic_enabled() -> bool:
    return engine_name() == ENGINE_SEMANTIC


# --------------------------------------------------------------------------
# 幾何のこまごま
# --------------------------------------------------------------------------

def _quantile(sorted_vals: list[float], q: float) -> float:
    """線形補間の分位点 (numpy 既定と同じ定義。外部依存を足さないため自前)。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[int(pos)]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _diag_radius(size: tuple[float, float]) -> float:
    """ノード外接矩形の対角半径 (§2 の「両端ノードの対角半径」)。"""
    return math.hypot(size[0], size[1]) / 2.0


def _required_length(edge: dict[str, Any],
                     sizes: dict[str, tuple[float, float]]) -> float:
    """§2 の必要長(e) = ラベル px 幅 + 両端の対角半径 + 24px。

    ラベルが無いエッジでもノードどうしが重ならない距離は要るので、ラベル幅 0 の
    ものとして同じ式を使う (スイープの拘束対象はラベル付きだけ — §2)。
    """
    return (edge_label_px(edge.get("label", ""), edge.get("glyph", "arrow"))
            + _diag_radius(sizes[edge["from"]]) + _diag_radius(sizes[edge["to"]])
            + EDGE_CLEARANCE)


def _half_extents(size: tuple[float, float]) -> tuple[float, float]:
    """当たり判定に使う半幅・半高 (楕円の縮小 + マージンの半分)。"""
    return (size[0] * ELLIPSE_SHRINK / 2.0 + NODE_MARGIN / 2.0,
            size[1] * ELLIPSE_SHRINK / 2.0 + NODE_MARGIN / 2.0)


def _unit(dx: float, dy: float) -> tuple[float, float]:
    """中心連結線の単位ベクトル。重なって退化したら +x 方向 (決定的)。"""
    d = math.hypot(dx, dy)
    if d < _EPS:
        return (1.0, 0.0)
    return (dx / d, dy / d)


# --------------------------------------------------------------------------
# §1c KK 抽象座標
# --------------------------------------------------------------------------

def _kk_coords(member_ids: list[str],
               island_edges: list[dict[str, Any]]) -> list[tuple[float, float]] | None:
    """igraph の Kamada-Kawai 抽象座標。多重辺・自己ループは畳んで渡す。

    igraph が無い / 例外のときは None を返し、呼び出し側が grid へ退避する。
    """
    try:
        import igraph as ig
    except Exception as exc:  # pragma: no cover - 環境依存の退避路
        logger.warning("igraph unavailable (%s); island falls back to grid", exc)
        return None

    index = {nid: i for i, nid in enumerate(member_ids)}
    pairs = sorted({
        (min(index[e["from"]], index[e["to"]]), max(index[e["from"]], index[e["to"]]))
        for e in island_edges if e["from"] != e["to"]
    })
    try:
        graph = ig.Graph(n=len(member_ids), edges=[list(p) for p in pairs])
        layout = graph.layout_kamada_kawai()
    except Exception as exc:  # pragma: no cover - 環境依存の退避路
        logger.warning("kamada_kawai failed (%s); island falls back to grid", exc)
        return None
    return [(float(p[0]), float(p[1])) for p in layout]


# --------------------------------------------------------------------------
# §2 制約スイープ
# --------------------------------------------------------------------------

def _sweep(member_ids: list[str],
           centers: dict[str, list[float]],
           sizes: dict[str, tuple[float, float]],
           labeled: list[dict[str, Any]],
           required: dict[str, float]) -> bool:
    """決定的な制約スイープ。1 パスで違反 0 になれば True。

    走査順は (id_a, id_b) の辞書順 → エッジ id の辞書順で固定。座標は両端を
    対称に動かすので、どのノードを「基準」にするかで結果が変わらない。
    """
    ordered = sorted(member_ids)
    for _ in range(SWEEP_MAX_PASSES):
        violations = 0

        # 1) ノード矩形の重なり → 中心連結線に沿って対称に押し出す
        for i in range(len(ordered)):
            a = ordered[i]
            ha_w, ha_h = _half_extents(sizes[a])
            for j in range(i + 1, len(ordered)):
                b = ordered[j]
                hb_w, hb_h = _half_extents(sizes[b])
                dx = centers[b][0] - centers[a][0]
                dy = centers[b][1] - centers[a][1]
                need_x, need_y = ha_w + hb_w, ha_h + hb_h
                if abs(dx) >= need_x or abs(dy) >= need_y:
                    continue
                violations += 1
                ux, uy = _unit(dx, dy)
                # u 方向に t 動かすと |dx| は |ux|·t 増える。x か y の
                # どちらかが分離すれば矩形は離れるので、小さい方の t を採る。
                tx = (need_x - abs(dx)) / abs(ux) if abs(ux) > _EPS else math.inf
                ty = (need_y - abs(dy)) / abs(uy) if abs(uy) > _EPS else math.inf
                t = min(tx, ty) + _PUSH_EPS
                centers[a][0] -= ux * t / 2.0
                centers[a][1] -= uy * t / 2.0
                centers[b][0] += ux * t / 2.0
                centers[b][1] += uy * t / 2.0

        # 2) ラベル付きエッジの中心間距離 < 必要長 → 対称に引き離す
        for e in labeled:
            a, b = e["from"], e["to"]
            dx = centers[b][0] - centers[a][0]
            dy = centers[b][1] - centers[a][1]
            d = math.hypot(dx, dy)
            need = required[e["id"]]
            if d >= need:
                continue
            violations += 1
            ux, uy = _unit(dx, dy)
            t = (need - d) + _PUSH_EPS
            centers[a][0] -= ux * t / 2.0
            centers[a][1] -= uy * t / 2.0
            centers[b][0] += ux * t / 2.0
            centers[b][1] += uy * t / 2.0

        if violations == 0:
            return True
    return False


def _semantic_centers(member_ids: list[str],
                      island_edges: list[dict[str, Any]],
                      sizes: dict[str, tuple[float, float]],
                      ) -> dict[str, list[float]] | None:
    """KK → スケール → スイープ (段階フォールバック込み)。解けなければ None。"""
    coords = _kk_coords(member_ids, island_edges)
    if coords is None:
        return None

    abstract = {nid: coords[i] for i, nid in enumerate(member_ids)}
    required = {e["id"]: _required_length(e, sizes) for e in island_edges}
    labeled = sorted((e for e in island_edges if e.get("label")),
                     key=lambda e: str(e["id"]))

    # --- スケール = clamp(p75(必要長/抽象長), 80, 3×median) ---
    ratios: list[float] = []
    for e in island_edges:
        ax, ay = abstract[e["from"]]
        bx, by = abstract[e["to"]]
        d = math.hypot(bx - ax, by - ay)
        if d > _EPS:
            ratios.append(required[e["id"]] / d)
    if ratios:
        ratios.sort()
        hi = max(SCALE_MIN, SCALE_MEDIAN_FACTOR * _quantile(ratios, 0.5))
        base_scale = min(max(_quantile(ratios, 0.75), SCALE_MIN), hi)
    else:
        base_scale = SCALE_NO_EDGE

    # --- 段階フォールバック: scale ×1.15 で最初からやり直す (≤3 回) ---
    for attempt in range(SCALE_RETRY_MAX + 1):
        scale = base_scale * (SCALE_RETRY_FACTOR ** attempt)
        centers = {nid: [abstract[nid][0] * scale, abstract[nid][1] * scale]
                   for nid in member_ids}
        if _sweep(member_ids, centers, sizes, labeled, required):
            return centers
    return None


# --------------------------------------------------------------------------
# §1c ノード ≤3 の島は横一列 (現行グリッドの 1 行と同じ間隔規則)
# --------------------------------------------------------------------------

def _row_centers(member_ids: list[str],
                 island_edges: list[dict[str, Any]],
                 sizes: dict[str, tuple[float, float]]) -> dict[str, list[float]]:
    """横一列。列間の隙間は「そこに載るエッジラベルの実幅」— grid と同じ規則。"""
    col_of = {nid: i for i, nid in enumerate(member_ids)}
    gaps = [COL_MARGIN] * max(1, len(member_ids) - 1)
    for e in island_edges:
        c1, c2 = col_of[e["from"]], col_of[e["to"]]
        if abs(c1 - c2) != 1:
            continue
        i = min(c1, c2)
        gaps[i] = max(gaps[i], edge_label_px(e.get("label", ""),
                                             e.get("glyph", "arrow")) + COL_MARGIN)

    row_h = max((sizes[nid][1] for nid in member_ids), default=NODE_H_MIN)
    centers: dict[str, list[float]] = {}
    x = 0.0
    for i, nid in enumerate(member_ids):
        w, _h = sizes[nid]
        centers[nid] = [x + w / 2.0, row_h / 2.0]
        x += w + (gaps[i] if i < len(member_ids) - 1 else 0.0)
    return centers


# --------------------------------------------------------------------------
# 島 1 つ分の組み立て
# --------------------------------------------------------------------------

def _place_from_centers(members: list[dict[str, Any]],
                        centers: dict[str, list[float]],
                        sizes: dict[str, tuple[float, float]],
                        cid: str, x0: int, y0: int
                        ) -> tuple[list[dict[str, Any]], list[int]]:
    """中心座標 → 正の整数座標 + 島 bbox (ノード外接 + ISLAND_PAD)。"""
    rel: dict[str, tuple[int, int]] = {}
    for n in members:
        nid = n["id"]
        w, h = sizes[nid]
        rel[nid] = (centers[nid][0] - w / 2.0, centers[nid][1] - h / 2.0)
    min_x = min(v[0] for v in rel.values())
    min_y = min(v[1] for v in rel.values())
    rel = {k: (round(v[0] - min_x), round(v[1] - min_y)) for k, v in rel.items()}

    inner_w = max(rel[n["id"]][0] + sizes[n["id"]][0] for n in members)
    inner_h = max(rel[n["id"]][1] + sizes[n["id"]][1] for n in members)

    nodes_out: list[dict[str, Any]] = []
    for n in members:
        nid = n["id"]
        w, h = sizes[nid]
        # 上位層が付けた属性は grid と同じ規則で引き継ぐ
        node = {k: v for k, v in n.items() if k not in _NODE_RESERVED}
        node.update({
            "id": nid,
            "label": n["label"],
            "x": x0 + ISLAND_PAD + rel[nid][0],
            "y": y0 + ISLAND_PAD + rel[nid][1],
            "size": w,
            "height": h,
            "community_id": cid,
            "style": n.get("style") or {"rough": True},
        })
        nodes_out.append(node)

    bbox = [x0, y0,
            round(x0 + inner_w + 2 * ISLAND_PAD),
            round(y0 + inner_h + 2 * ISLAND_PAD)]
    return nodes_out, bbox


def _grid_island(members: list[dict[str, Any]],
                 island_edges: list[dict[str, Any]],
                 cid: str, meta: dict[str, Any], detail_level: str,
                 x0: int, y0: int) -> tuple[list[dict[str, Any]], list[int]]:
    """その島だけ grid で組む (§2 の最終フォールバック)。

    grid 実装は温存が憲法なので、**同じ関数をそのまま呼んで**島 1 つ分の
    レイアウトを作り、平行移動する。写経すると grid の間隔規則が二重管理になる。
    """
    sub_kg = {
        "graph_version": "kg_island",
        "nodes": members,
        "edges": island_edges,
        "communities": [dict(meta, id=cid)] if meta else [{"id": cid, "name": cid}],
    }
    sub = _compute_layout_grid(sub_kg, detail_level=detail_level)
    bx0, by0, bx1, by1 = sub["islands"][0]["bbox"]
    dx, dy = x0 - bx0, y0 - by0
    nodes_out = []
    for n in sub["nodes"]:
        node = dict(n)
        node["x"] = round(node["x"] + dx)
        node["y"] = round(node["y"] + dy)
        nodes_out.append(node)
    return nodes_out, [x0, y0, round(bx1 + dx), round(by1 + dy)]


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def compute_layout_v3(kg: dict[str, Any],
                      detail_level: str = "standard") -> dict[str, Any]:
    """semantic レイアウト。`cc_core.layout.compute_layout` から分岐して呼ばれる。"""
    kg_nodes: list[dict[str, Any]] = kg.get("nodes", [])
    kg_edges: list[dict[str, Any]] = kg.get("edges", [])
    communities: dict[str, dict[str, Any]] = {
        c["id"]: c for c in kg.get("communities", [])
    }
    if not kg_nodes:
        raise ValueError("knowledge_graph has no nodes")

    # エッジの正規化は grid と同一 (表示に関わる項目だけ決める)
    edges_out: list[dict[str, Any]] = []
    for idx, e in enumerate(kg_edges):
        glyph = e.get("glyph", "arrow")
        if glyph not in VALID_GLYPHS:
            glyph = "arrow"
        edge = {k: v for k, v in e.items()
                if k not in ("id", "from", "to", "label", "glyph")}
        edge.update({
            "id": e.get("id") or f"r{idx + 1:03d}",
            "from": e["from"],
            "to": e["to"],
            "label": truncate(e.get("label", ""), EDGE_LABEL_MAX_EM),
            "glyph": glyph,
        })
        edges_out.append(edge)

    groups: dict[str, list[dict[str, Any]]] = {}
    for n in kg_nodes:
        groups.setdefault(n.get("community_id") or "comm_default", []).append(n)

    sizes = {n["id"]: node_size(n["label"]) for n in kg_nodes}

    # 島どうしの配置は L1 では現行のシェルフ方式のまま (パッキングは L2)
    islands_per_row = max(1, math.ceil(math.sqrt(len(groups))))
    cursor_x, cursor_y, row_height = ORIGIN_X, ORIGIN_Y, 0

    nodes_out: list[dict[str, Any]] = []
    islands_out: list[dict[str, Any]] = []

    for island_idx, (cid, members) in enumerate(groups.items()):
        if island_idx > 0 and island_idx % islands_per_row == 0:
            cursor_x = ORIGIN_X
            cursor_y += row_height + ISLAND_GAP_Y
            row_height = 0

        member_ids = [n["id"] for n in members]
        member_set = set(member_ids)
        island_edges = [e for e in edges_out
                        if e["from"] in member_set and e["to"] in member_set]
        meta = communities.get(cid, {})

        centers: dict[str, list[float]] | None
        if len(members) <= ROW_MAX_NODES:
            centers = _row_centers(member_ids, island_edges, sizes)
        else:
            centers = _semantic_centers(member_ids, island_edges, sizes)

        if centers is None:
            logger.warning(
                "island %s: constraint sweep did not converge; grid fallback", cid)
            island_nodes, bbox = _grid_island(
                members, island_edges, cid, meta, detail_level,
                cursor_x, cursor_y)
            mode = MODE_GRID_FALLBACK
        else:
            island_nodes, bbox = _place_from_centers(
                members, centers, sizes, cid, cursor_x, cursor_y)
            mode = MODE_SEMANTIC

        nodes_out.extend(island_nodes)
        islands_out.append({
            "community_id": cid,
            "name": meta.get("name", cid),
            "bbox": bbox,
            "is_gap": bool(meta.get("is_gap", False)),
            "layout_mode": mode,
        })
        row_height = max(row_height, bbox[3] - bbox[1])
        cursor_x = bbox[2] + ISLAND_GAP_X

    return {
        "detail_level": detail_level,
        "nodes": nodes_out,
        "edges": edges_out,
        "islands": islands_out,
        "provenance": {
            "graph_version": kg.get("graph_version", "kg_unknown"),
            "generated_for": kg.get("generated_for", "layout_engine"),
            "layout_engine": LAYOUT_ENGINE_ID,
        },
    }


# --------------------------------------------------------------------------
# §6 summary
# --------------------------------------------------------------------------

def layout_summary(plan: dict[str, Any]) -> dict[str, Any]:
    """summary["layout"] の中身。フォールバック件数を黙らせないための窓口。

    3 レベル同梱 plan なら全レベルの島を合算する (どのレベルで崩れたかは
    ログに出る。summary は「起きたかどうか」を伝えるのが役目)。
    """
    level_plans = plan.get("_level_plans") or {}
    targets = [level_plans[k] for k in sorted(level_plans)] or [plan]

    semantic = fallback = 0
    engine = ENGINE_GRID
    for p in targets:
        if p.get("provenance", {}).get("layout_engine") == LAYOUT_ENGINE_ID:
            engine = ENGINE_SEMANTIC
        for isl in p.get("islands", []):
            mode = isl.get("layout_mode")
            if mode == MODE_GRID_FALLBACK:
                fallback += 1
            elif mode == MODE_SEMANTIC:
                semantic += 1
    return {"engine": engine,
            "islands": {"semantic": semantic, "grid_fallback": fallback}}
