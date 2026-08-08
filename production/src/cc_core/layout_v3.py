"""レイアウト v3 (semantic layout) — 島内配置 (バッチ L1 + L2)。

設計書: docs/layout-v3-design.md (§0 モジュール構成 / §1 骨格選択 / §1a 層状 /
§1b 木 / §1c KK / §2 スケール + スイープ + 段階フォールバック / §6 summary)。

このモジュールは `CC_LAYOUT_ENGINE=semantic` のときだけ動く。既定 (grid) では
`cc_core.layout` の従来コードパスがそのまま走り、生成物はバイト等価のまま。

範囲:
  - L1: KK 島 (igraph.layout_kamada_kawai)・ノード ≤3 の横一列・p75 スケール +
    決定的な制約スイープ + 段階フォールバック
  - L2: 骨格選択 (§1 の表)・層状 (Sugiyama 左→右)・木 (RT 抽象=上)・
    島どうしの配置を `island_packing` へ (シェルフ方式を置き換え)
  - L3: サイズ係数とティント

決定性の担保 (憲法):
  - 乱数・seed を一切使わない。igraph の KK / Sugiyama / RT は初期配置が固定で、
    同じ入力から同じ座標が出ることをこの venv (igraph 1.0.0) で実測済み
  - 走査順はすべて id の辞書順か入力順。dict の挿入順にも依存しない
  - 閉路切断は confidence 昇順 (同点は edge id 順)、根の選択は id 順
"""

from __future__ import annotations

import math
import os
from typing import Any, NamedTuple

from cc_core.island_packing import Island, pack_islands
from cc_core.layout import (
    COL_MARGIN,
    EDGE_FONT,
    EDGE_LABEL_MAX_EM,
    ISLAND_PAD,
    LINE_H,
    NODE_H_MIN,
    NODE_W_MIN,
    ROW_MARGIN,
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

# --- §1 骨格の種類 ---
SKELETON_ROW = "row"          # ノード ≤3 の横一列
SKELETON_LAYERED = "layered"  # 因果島 = Sugiyama 層状・左→右
SKELETON_TREE = "tree"        # 階層島 = Reingold-Tilford・抽象=上
SKELETON_KK = "kk"            # 近接島 = Kamada-Kawai

CAUSAL_GLYPHS = frozenset({"arrow", "precedes"})
HIER_GLYPHS = frozenset({"isa", "partof"})
DEFAULT_CONFIDENCE = 0.5      # §1a: confidence 欠損はこの値として扱う

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
# (既定の生成物をバイト等価に保つため)。退避の理由は区別して記録する
# (L1 検収の申し送り: 「igraph が無い」と「制約が解けない」は打ち手が違う)。
MODE_SEMANTIC = "semantic"
MODE_GRID_FALLBACK = "grid_fallback"
MODE_GRID_NO_IGRAPH = "grid_fallback_no_igraph"
_FALLBACK_MODES = (MODE_GRID_FALLBACK, MODE_GRID_NO_IGRAPH)

# 島内配置が失敗した理由 → layout_mode
FAIL_NO_IGRAPH = "no_igraph"
FAIL_SWEEP = "sweep"
_FAIL_TO_MODE = {FAIL_NO_IGRAPH: MODE_GRID_NO_IGRAPH, FAIL_SWEEP: MODE_GRID_FALLBACK}

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

def _igraph():
    """igraph を遅延 import する。無い環境では None (呼び出し側が grid へ退避)。"""
    try:
        import igraph as ig
    except Exception as exc:  # pragma: no cover - 環境依存の退避路
        logger.warning("igraph unavailable (%s); island falls back to grid", exc)
        return None
    return ig


def _kk_coords(member_ids: list[str],
               island_edges: list[dict[str, Any]]) -> list[tuple[float, float]] | None:
    """igraph の Kamada-Kawai 抽象座標。多重辺・自己ループは畳んで渡す。

    igraph が無い / 例外のときは None を返し、呼び出し側が grid へ退避する。
    """
    ig = _igraph()
    if ig is None:
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
           required: dict[str, float]) -> int | None:
    """決定的な制約スイープ。収束したら**使ったパス数**、駄目なら None。

    走査順は (id_a, id_b) の辞書順 → エッジ id の辞書順で固定。座標は両端を
    対称に動かすので、どのノードを「基準」にするかで結果が変わらない。
    パス数は summary["layout"]["sweeps_max"] に出す (§6) — 収束ぎりぎりの
    グラフが増えていないかを運用中に見張るため。
    """
    ordered = sorted(member_ids)
    for used in range(1, SWEEP_MAX_PASSES + 1):
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
            return used
    return None


class Placement(NamedTuple):
    """島 1 つ分の中心座標と、そこへ至るまでに使った手数・失敗理由。"""

    centers: dict[str, list[float]] | None
    sweeps: int = 0
    fail: str | None = None


def _abstract_to_px(member_ids: list[str],
                    island_edges: list[dict[str, Any]],
                    sizes: dict[str, tuple[float, float]],
                    abstract: dict[str, tuple[float, float]]) -> Placement:
    """§2: 抽象座標 → スケール → スイープ (段階フォールバック込み)。

    KK (§1c) と RT (§1b) の共通経路。層状 (§1a) は grid の列間隔規則で px を
    決めるのでここを通らない。
    """
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
        used = _sweep(member_ids, centers, sizes, labeled, required)
        if used is not None:
            return Placement(centers, used)
    return Placement(None, SWEEP_MAX_PASSES, FAIL_SWEEP)


def _semantic_centers(member_ids: list[str],
                      island_edges: list[dict[str, Any]],
                      sizes: dict[str, tuple[float, float]]) -> Placement:
    """§1c 近接島: KK → §2 の px 化。"""
    coords = _kk_coords(member_ids, island_edges)
    if coords is None:
        return Placement(None, 0, FAIL_NO_IGRAPH)
    abstract = {nid: coords[i] for i, nid in enumerate(member_ids)}
    return _abstract_to_px(member_ids, island_edges, sizes, abstract)


# --------------------------------------------------------------------------
# §1 骨格選択
# --------------------------------------------------------------------------

def _skeleton_kind(members: list[dict[str, Any]],
                   island_edges: list[dict[str, Any]]) -> str:
    """§1 の表を上から評価して、この島をどう組むかを決める。

    E = 島内の**ラベル付き**エッジ数 (設計書 §1 の定義)。causal / hier も同じ
    母集団で数える。E=0 (ラベルが 1 本も無い島) では ceil(E/2)=0 となって
    どの条件も真になってしまうので、骨格が実在すること (≥1 本) も要求する。
    """
    if len(members) <= ROW_MAX_NODES:
        return SKELETON_ROW
    labeled = [e for e in island_edges if e.get("label")]
    half = math.ceil(len(labeled) / 2)
    causal = sum(1 for e in labeled if e["glyph"] in CAUSAL_GLYPHS)
    hier = sum(1 for e in labeled if e["glyph"] in HIER_GLYPHS)
    if causal and causal >= half:
        return SKELETON_LAYERED
    if hier and hier >= half:
        return SKELETON_TREE
    return SKELETON_KK


# --------------------------------------------------------------------------
# §1a 層状 (因果島) — Sugiyama・左→右
# --------------------------------------------------------------------------

def _confidence(edge: dict[str, Any]) -> float:
    """confidence を float で。欠損・壊れた値は 0.5 (§1a)。"""
    try:
        raw = edge.get("confidence")
        return DEFAULT_CONFIDENCE if raw is None else float(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONFIDENCE


def _reaches(succ: dict[str, list[str]], src: str, dst: str) -> bool:
    """src から dst へ到達できるか (深さ優先・訪問順は追加順で決定的)。"""
    stack, seen = [src], {src}
    while stack:
        cur = stack.pop()
        if cur == dst:
            return True
        for nxt in succ.get(cur, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False


def _acyclic_skeleton(skeleton: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """§1a-1 閉路切断: confidence の低いものから外して DAG にする。

    実装は「confidence 降順 (同点は edge id の辞書順) に採用し、閉路を作る辺
    だけ捨てる」貪欲法。閉路の中で**最後に検討される = 最も confidence の低い**
    辺が捨てられるので、設計書の「昇順に低いものから外す」と同じ結果になる。
    """
    ordered = sorted(skeleton, key=lambda e: (-_confidence(e), str(e["id"])))
    succ: dict[str, list[str]] = {}
    kept: list[dict[str, Any]] = []
    for e in ordered:
        if e["from"] == e["to"]:
            continue
        if _reaches(succ, e["to"], e["from"]):     # 逆流が既にある = 閉路になる
            logger.debug("cycle edge dropped: %s (%s→%s)", e["id"], e["from"], e["to"])
            continue
        succ.setdefault(e["from"], []).append(e["to"])
        kept.append(e)
    return kept


def _longest_path_layers(member_ids: list[str],
                         dag: list[dict[str, Any]]) -> dict[str, int]:
    """§1a-2 層割当 = 源からの最長路。孤立ノードは層 0。"""
    succ: dict[str, list[str]] = {nid: [] for nid in member_ids}
    indeg: dict[str, int] = {nid: 0 for nid in member_ids}
    for e in dag:
        succ[e["from"]].append(e["to"])
        indeg[e["to"]] += 1

    layer = {nid: 0 for nid in member_ids}
    ready = sorted(nid for nid in member_ids if indeg[nid] == 0)
    while ready:
        cur = ready.pop(0)                       # id の小さい方から (決定的)
        for nxt in succ[cur]:
            layer[nxt] = max(layer[nxt], layer[cur] + 1)
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
                ready.sort()
    return layer


def _barycenter_order(member_ids: list[str], dag: list[dict[str, Any]],
                      layer: dict[str, int]) -> dict[str, float] | None:
    """§1a-2: 層内順序だけ igraph の Sugiyama に任せる (x 座標を順序キーに使う)。"""
    ig = _igraph()
    if ig is None:
        return None
    index = {nid: i for i, nid in enumerate(member_ids)}
    edges = [[index[e["from"]], index[e["to"]]] for e in dag]
    try:
        graph = ig.Graph(n=len(member_ids), edges=edges, directed=True)
        coords = graph.layout_sugiyama(layers=[layer[nid] for nid in member_ids])
    except Exception as exc:  # pragma: no cover - 環境依存の退避路
        logger.warning("layout_sugiyama failed (%s); island falls back to grid", exc)
        return None
    if len(coords) < len(member_ids):  # pragma: no cover - 仕様変更の検知用
        logger.warning("layout_sugiyama returned %d coords for %d nodes",
                       len(coords), len(member_ids))
        return None
    return {nid: float(coords[index[nid]][0]) for nid in member_ids}


def _layered_centers(member_ids: list[str],
                     island_edges: list[dict[str, Any]],
                     sizes: dict[str, tuple[float, float]]) -> Placement:
    """§1a 因果島。層 = 列として **grid の列間隔ロジックを再利用**して px 化する。

    こうすると隣接層のエッジラベルは grid と同じ水準で必ず収まり、スイープは
    要らない (構成的に重なりが出ない)。骨格エッジは必ず from.x < to.x になる。
    """
    skeleton = [e for e in island_edges
                if e["glyph"] in CAUSAL_GLYPHS and e["from"] != e["to"]]
    dag = _acyclic_skeleton(skeleton)
    layer = _longest_path_layers(member_ids, dag)
    bary = _barycenter_order(member_ids, dag, layer)
    if bary is None:
        return Placement(None, 0, FAIL_NO_IGRAPH)

    n_cols = max(layer.values()) + 1
    cols: list[list[str]] = [[] for _ in range(n_cols)]
    for nid in sorted(member_ids, key=lambda n: (bary[n], n)):
        cols[layer[nid]].append(nid)

    # --- 列幅と列間 gap (grid と同じ規則) ---
    col_w = [max((sizes[nid][0] for nid in col), default=NODE_W_MIN)
             for col in cols]
    col_gap = [float(COL_MARGIN)] * max(1, n_cols - 1)
    labeled_pairs: dict[tuple[str, str], float] = {}
    for e in island_edges:
        width = edge_label_px(e.get("label", ""), e.get("glyph", "arrow"))
        c1, c2 = layer[e["from"]], layer[e["to"]]
        if abs(c1 - c2) == 1:
            col_gap[min(c1, c2)] = max(col_gap[min(c1, c2)], width + COL_MARGIN)
        elif c1 == c2 and width:
            key = (min(e["from"], e["to"]), max(e["from"], e["to"]))
            labeled_pairs[key] = max(labeled_pairs.get(key, 0.0), width)

    col_x: list[float] = []
    x = 0.0
    for c in range(n_cols):
        col_x.append(x)
        x += col_w[c] + (col_gap[c] if c < n_cols - 1 else 0.0)

    # --- 層内の縦積み (行間は grid の ROW_MARGIN 規則) ---
    stacks: list[list[tuple[str, float]]] = []
    col_h: list[float] = []
    for col in cols:
        offsets: list[tuple[str, float]] = []
        y = 0.0
        for i, nid in enumerate(col):
            offsets.append((nid, y))
            y += sizes[nid][1]
            if i < len(col) - 1:
                pair = (min(nid, col[i + 1]), max(nid, col[i + 1]))
                y += (EDGE_FONT * LINE_H + ROW_MARGIN if pair in labeled_pairs
                      else ROW_MARGIN)
        stacks.append(offsets)
        col_h.append(y)

    total_h = max(col_h) if col_h else 0.0
    centers: dict[str, list[float]] = {}
    for c, offsets in enumerate(stacks):
        top = (total_h - col_h[c]) / 2.0          # 列を縦中央に揃える
        for nid, off in offsets:
            centers[nid] = [col_x[c] + col_w[c] / 2.0,
                            top + off + sizes[nid][1] / 2.0]
    return Placement(centers)


# --------------------------------------------------------------------------
# §1b 木 (階層島) — Reingold-Tilford・抽象=上
# --------------------------------------------------------------------------

def _hier_parent_of(island_edges: list[dict[str, Any]]
                    ) -> list[tuple[str, str]]:
    """isa/partof を (親, 子) の組に直す。

    向きの決め方は描画の流儀に合わせてある: isa は UML と同じく白抜き三角が
    指す側 (= `to`) が上位クラス、partof は `to` が全体。つまり親 = `to`。
    """
    return [(e["to"], e["from"]) for e in island_edges
            if e["glyph"] in HIER_GLYPHS and e["from"] != e["to"]]


def _tree_roots(member_ids: list[str],
                pairs: list[tuple[str, str]]) -> list[str]:
    """§1b 根 = 親側の到達点 (入次数 0 = 誰の子でもないノード。同点は id 順)。

    階層に閉路があって根が取れない成分は、その成分の最小 id を根に立てる
    (黙って落とすとノードが消えるため)。
    """
    children = {c for _p, c in pairs}
    roots = [nid for nid in sorted(member_ids) if nid not in children]

    adj: dict[str, set[str]] = {nid: set() for nid in member_ids}
    for p, c in pairs:
        adj[p].add(c)
        adj[c].add(p)
    seen: set[str] = set()
    for start in list(roots) + sorted(member_ids):
        if start in seen:
            continue
        if start not in roots:
            roots.append(start)                  # 閉路成分の代表 (最小 id)
        stack = [start]
        seen.add(start)
        while stack:
            cur = stack.pop()
            for nxt in sorted(adj[cur]):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
    return roots


def _tree_centers(member_ids: list[str],
                  island_edges: list[dict[str, Any]],
                  sizes: dict[str, tuple[float, float]]) -> Placement:
    """§1b 階層島。RT の抽象座標 (y = 深さ) を §2 の経路で px にする。"""
    ig = _igraph()
    if ig is None:
        return Placement(None, 0, FAIL_NO_IGRAPH)

    pairs = _hier_parent_of(island_edges)
    roots = _tree_roots(member_ids, pairs)
    index = {nid: i for i, nid in enumerate(member_ids)}
    edges = sorted({(index[p], index[c]) for p, c in pairs})

    n = len(member_ids)
    virtual = len(roots) != 1                     # 森は仮想根でまとめる (§1b)
    if virtual:
        edges = edges + [(n, index[r]) for r in roots]
    try:
        graph = ig.Graph(n=n + (1 if virtual else 0),
                         edges=[list(p) for p in edges], directed=False)
        coords = graph.layout_reingold_tilford(root=[n if virtual else index[roots[0]]])
    except Exception as exc:  # pragma: no cover - 環境依存の退避路
        logger.warning("layout_reingold_tilford failed (%s); grid fallback", exc)
        return Placement(None, 0, FAIL_NO_IGRAPH)

    # 仮想根の座標は捨てる。y は深さ (小さいほど抽象) なのでそのまま使う
    abstract = {nid: (float(coords[index[nid]][0]), float(coords[index[nid]][1]))
                for nid in member_ids}
    return _abstract_to_px(member_ids, island_edges, sizes, abstract)


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
                        cid: str
                        ) -> tuple[list[dict[str, Any]], tuple[int, int]]:
    """中心座標 → 島原点からの相対整数座標 + 島の外寸 (ノード外接 + ISLAND_PAD)。

    島の**絶対位置は island_packing が後から決める** (§3) ので、ここでは
    (0, 0) を島の左上とした相対座標だけを返す。
    """
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
            "x": ISLAND_PAD + rel[nid][0],
            "y": ISLAND_PAD + rel[nid][1],
            "size": w,
            "height": h,
            "community_id": cid,
            "style": n.get("style") or {"rough": True},
        })
        nodes_out.append(node)

    size = (round(inner_w + 2 * ISLAND_PAD), round(inner_h + 2 * ISLAND_PAD))
    return nodes_out, size


def _grid_island(members: list[dict[str, Any]],
                 island_edges: list[dict[str, Any]],
                 cid: str, meta: dict[str, Any], detail_level: str,
                 ) -> tuple[list[dict[str, Any]], tuple[int, int]]:
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
    nodes_out = []
    for n in sub["nodes"]:
        node = dict(n)
        node["x"] = round(node["x"] - bx0)
        node["y"] = round(node["y"] - by0)
        nodes_out.append(node)
    return nodes_out, (round(bx1 - bx0), round(by1 - by0))


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def compute_layout_v3(kg: dict[str, Any],
                      detail_level: str = "standard",
                      *, anchors: dict[str, Any] | None = None) -> dict[str, Any]:
    """semantic レイアウト。`cc_core.layout.compute_layout` から分岐して呼ばれる。

    `anchors` は §3a のレベル間アンカー (detailed の配置から作った方位)。
    None なら島の目標位置はメタ KK が決める。
    """
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
    community_of = {n["id"]: (n.get("community_id") or "comm_default")
                    for n in kg_nodes}

    # --- 1) 島ごとに中身を組む (座標は島原点からの相対) ---
    laid: list[tuple[str, list[dict[str, Any]], tuple[int, int], str, int]] = []
    for cid, members in groups.items():
        member_ids = [n["id"] for n in members]
        member_set = set(member_ids)
        island_edges = [e for e in edges_out
                        if e["from"] in member_set and e["to"] in member_set]
        meta = communities.get(cid, {})

        kind = _skeleton_kind(members, island_edges)
        if kind == SKELETON_ROW:
            placed = Placement(_row_centers(member_ids, island_edges, sizes))
        elif kind == SKELETON_LAYERED:
            placed = _layered_centers(member_ids, island_edges, sizes)
        elif kind == SKELETON_TREE:
            placed = _tree_centers(member_ids, island_edges, sizes)
        else:
            placed = _semantic_centers(member_ids, island_edges, sizes)

        if placed.centers is None:
            logger.warning("island %s (%s): %s; grid fallback", cid, kind, placed.fail)
            island_nodes, size = _grid_island(
                members, island_edges, cid, meta, detail_level)
            mode = _FAIL_TO_MODE.get(placed.fail or "", MODE_GRID_FALLBACK)
        else:
            island_nodes, size = _place_from_centers(
                members, placed.centers, sizes, cid)
            mode = MODE_SEMANTIC
        laid.append((cid, island_nodes, size, mode, placed.sweeps))

    # --- 2) 島どうしの配置 (§3 メタ KK + 接触候補点パッキング) ---
    links = {(a, b) for a, b in (
        (community_of.get(e["from"], ""), community_of.get(e["to"], ""))
        for e in edges_out) if a and b and a != b}
    origins = pack_islands(
        [Island(cid, float(size[0]), float(size[1])) for cid, _n, size, _m, _s in laid],
        links={(min(a, b), max(a, b)) for a, b in links},
        anchors=anchors)

    nodes_out: list[dict[str, Any]] = []
    islands_out: list[dict[str, Any]] = []
    for cid, island_nodes, size, mode, sweeps in laid:
        x0, y0 = origins[cid]
        for node in island_nodes:
            node["x"] += x0
            node["y"] += y0
            nodes_out.append(node)
        islands_out.append({
            "community_id": cid,
            "name": communities.get(cid, {}).get("name", cid),
            "bbox": [x0, y0, x0 + size[0], y0 + size[1]],
            "is_gap": bool(communities.get(cid, {}).get("is_gap", False)),
            "layout_mode": mode,
            "sweeps": sweeps,
        })

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

    semantic = fallback = no_igraph = sweeps_max = 0
    engine = ENGINE_GRID
    for p in targets:
        if p.get("provenance", {}).get("layout_engine") == LAYOUT_ENGINE_ID:
            engine = ENGINE_SEMANTIC
        for isl in p.get("islands", []):
            mode = isl.get("layout_mode")
            if mode in _FALLBACK_MODES:
                fallback += 1
                no_igraph += mode == MODE_GRID_NO_IGRAPH
            elif mode == MODE_SEMANTIC:
                semantic += 1
            sweeps_max = max(sweeps_max, int(isl.get("sweeps") or 0))
    islands = {"semantic": semantic, "grid_fallback": fallback}
    # 退避の理由は打ち手が違う (igraph 不在 = 環境の問題 / スイープ不能 =
    # グラフの問題)。起きたときだけ内訳を足す — 常用時の summary は §6 のまま。
    if no_igraph:
        islands["grid_fallback_no_igraph"] = no_igraph
    return {"engine": engine, "islands": islands, "sweeps_max": sweeps_max}
