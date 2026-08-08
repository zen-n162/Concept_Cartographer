"""レイアウトの可読性 (重なり) テスト。

2026-08-05 の実障害: エッジラベルがノード間のスキマより広く、11/17 本の
ラベルがノードに重なって読めなくなった。レイアウトは間隔をラベル幅から
決めるべき、という回帰テスト。

2026-08-07 (v2): ラベル配置を一括プランナーへ昇格 (レイアウト重なり設計書
裁定 AA/AB/AC)。以降のテストは「実際に描かれる位置」で重なりを測る。
"""

import copy
import json
import math
import random
import re
import sys
from pathlib import Path

import pytest

from cc_core.layout import EDGE_FONT, compute_layout, edge_label_px, node_size
from cc_core.overlap import (
    ISLAND_TITLE_BAND,
    LabelPlacement,
    _candidates,
    _text_variants,
    check_overlaps,
    clear_label_plan_cache,
    edge_label_rect,
    node_rect,
    plan_label_layout,
    resolve_label_offset,
)
from cc_core.textmetrics import display_width, truncate, wrap_to_lines
from cc_core.validate import validate_layout_plan

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
GRAPHS = Path(__file__).resolve().parents[1] / "graphs"
REAL_SESSIONS = ("20260807_170447", "20260807_143804", "20260807_010128")


def _kg():
    return json.loads((FIXTURES / "kg_min.json").read_text(encoding="utf-8"))


def _dense_kg(n_nodes: int = 12, label: str = "長めの概念ラベル名称") -> dict:
    """同一行に長いラベルのエッジが並ぶ、重なりが起きやすい構成。"""
    nodes = [{"id": f"c{i:03d}", "label": f"{label}{i}", "community_id": "m1"}
             for i in range(n_nodes)]
    edges = [{"id": f"r{i:03d}", "from": f"c{i:03d}", "to": f"c{i+1:03d}",
              "label": "関係の説明テキスト", "glyph": "arrow"}
             for i in range(n_nodes - 1)]
    return {"graph_version": "kg_dense", "nodes": nodes, "edges": edges,
            "communities": [{"id": "m1", "name": "密なテーマ"}]}


# --- textmetrics ---

def test_display_width_counts_fullwidth_as_one_em():
    assert display_width("概念") == pytest.approx(2.0)
    assert display_width("ab") == pytest.approx(1.1)
    assert display_width("") == 0


def test_truncate_respects_budget():
    out = truncate("非常に長い関係ラベルの説明文", 8.0)
    assert display_width(out) <= 8.0
    assert out.endswith("…")
    assert truncate("短い", 8.0) == "短い"


def test_wrap_splits_japanese_anywhere():
    lines = wrap_to_lines("あいうえおかきくけこ", 4.0, max_lines=3)
    assert all(display_width(x) <= 4.0 for x in lines)
    assert "".join(lines).rstrip("…") in "あいうえおかきくけこ"


# --- node sizing ---

def test_node_grows_with_label_length():
    w_short, _ = node_size("NV中心")
    w_long, _ = node_size("非常に長い概念名称のノードラベル")
    assert w_long > w_short


def test_node_size_is_clamped():
    w, h = node_size("あ" * 200)
    assert w <= 300 and h >= 66


# --- layout spacing ---

def test_every_labeled_edge_has_room_or_is_retreated(monkeypatch):
    """全ラベル付きエッジで「両端の間隔 ≥ 必要長」か、プランナーが退避済みか。

    元は「同一行の隣接ノード間のスキマがラベルより広いこと」という grid 前提の
    テストだった (レイアウト v3 §7)。semantic は層状・木・KK で組むので「同じ行」
    という概念が無い。エンジンに依らず守るべき不変条件はこちら:

      ラベルは自然な中点にそのまま置けるだけの距離がある。無ければ
      プランナーが逃がしてある (unresolved = 逃げ場なしは 1 本も出さない)。

    「間隔」はエッジの向きに沿って測る (grid の「横のスキマ」を任意方向へ
    一般化したもの)。楕円の半径は向きによって変わるので、その向きでの半径を
    使って両端の楕円のあいだの実効的な空きを出す。

    どちらのエンジンでも成り立つので、両方で回して二重に確かめる。
    """
    def _radius_towards(node: dict, ux: float, uy: float) -> float:
        """楕円中心から (ux, uy) 方向の縁までの距離。"""
        rx, ry = node["size"] / 2, node["height"] / 2
        return 1.0 / math.hypot(ux / rx, uy / ry)

    for engine in ("semantic", "grid"):
        monkeypatch.setenv("CC_LAYOUT_ENGINE", engine)
        plan = compute_layout(_dense_kg())
        nodes = {n["id"]: n for n in plan["nodes"]}
        clear_label_plan_cache()
        placements = plan_label_layout(plan)

        checked = 0
        for e in plan["edges"]:
            if not e.get("label"):
                continue
            checked += 1
            a, b = nodes[e["from"]], nodes[e["to"]]
            dx = (b["x"] + b["size"] / 2) - (a["x"] + a["size"] / 2)
            dy = (b["y"] + b["height"] / 2) - (a["y"] + a["height"] / 2)
            span = math.hypot(dx, dy)
            assert span > 0, f"{engine}: edge {e['id']} の両端が同じ位置にある"
            ux, uy = dx / span, dy / span
            free = span - _radius_towards(a, ux, uy) - _radius_towards(b, -ux, -uy)
            if free >= edge_label_px(e["label"], e["glyph"]):
                continue                       # 中点にそのまま置ける
            p = placements.get(e["id"])
            assert p is not None and p.retreated, (
                f"{engine}: edge {e['id']} は空き {free:.0f}px がラベル幅"
                f"{edge_label_px(e['label'], e['glyph']):.0f}px に足りないのに"
                f"退避もしていない")
        assert checked > 0, f"{engine}: ラベル付きエッジが 1 本も無い"
        assert not any(p.unresolved for p in placements.values()), \
            f"{engine}: 逃げ場の無いラベルが残った"
    clear_label_plan_cache()


def test_dense_layout_has_no_label_overlap():
    plan = compute_layout(_dense_kg())
    nodes = {n["id"]: n for n in plan["nodes"]}
    report = check_overlaps(plan)
    unresolved = [c for c in report.label_on_node
                  if resolve_label_offset(
                      next(e for e in plan["edges"] if e["id"] == c["edge"]), nodes) is None]
    assert not unresolved, unresolved


def test_no_node_overlap_and_all_inside_islands():
    for kg in (_kg(), _dense_kg()):
        report = check_overlaps(compute_layout(kg))
        assert not report.node_on_node
        assert not report.node_outside_island


def test_layout_still_valid_and_deterministic():
    plan = compute_layout(_kg())
    assert validate_layout_plan(plan).valid
    assert compute_layout(_kg()) == plan


def test_long_edge_over_intermediate_node_gets_offset():
    """中間ノードを飛び越すエッジのラベルは退避位置が与えられる。"""
    kg = {
        "graph_version": "kg_skip",
        "nodes": [{"id": f"c{i}", "label": f"ノード{i}", "community_id": "m1"}
                  for i in range(3)],
        "edges": [{"id": "r1", "from": "c0", "to": "c2",
                   "label": "飛び越す関係", "glyph": "arrow"}],
        "communities": [{"id": "m1", "name": "テスト"}],
    }
    plan = compute_layout(kg)
    nodes = {n["id"]: n for n in plan["nodes"]}
    # c0,c1,c2 が横一列なら c0→c2 の中点は c1 の上に来る
    if nodes["c0"]["y"] == nodes["c1"]["y"] == nodes["c2"]["y"]:
        assert resolve_label_offset(plan["edges"][0], nodes) is not None


# ==========================================================================
# v2: 一括プランナー (レイアウト重なり設計書 §3)
# ==========================================================================

def _synth_kg(n: int, *, seed: int = 7, crowded: bool = False,
              cross_ratio: float = 0.15) -> dict:
    """合成グラフ。

    crowded=False: 島の中で鎖状につながり、島をまたぐ関係が少し混じる
                   (実セッションと同じ形。ここは clean を維持すること)
    crowded=True:  ほぼ全ての関係が島をまたぐ敵対的な形

    注意: crowded=True は grid では逃げ場が無くなるが、レイアウト v3 (semantic)
    は同じグラフを**解いてしまう** (島を広げてラベルの居場所を作る)。裁定 AC の
    「短縮 → unresolved 報告」を調べる 4 本は、エンジンに依らず必然的に詰む
    `_hopeless_kg` へ移した。
    """
    rnd = random.Random(seed)
    comms = max(1, n // 25)
    per = max(1, n // comms)
    labels = ["関係の説明テキスト", "促進する", "ノイズ影響を緩和", "支える"]
    glyphs = ["arrow", "wave", "isa", "double"]

    def cid(i: int) -> str:
        return f"m{i % comms}" if crowded else f"m{i // per}"

    nodes = [{"id": f"c{i:04d}", "label": f"概念ラベル{i}", "community_id": cid(i)}
             for i in range(n)]
    edges = []
    for i in range(n):
        for k in (1, 3):
            j = (i + k) % n
            if i == j or (not crowded and cid(i) != cid(j)):
                continue
            edges.append({"id": f"r{len(edges):04d}", "from": f"c{i:04d}",
                          "to": f"c{j:04d}", "label": rnd.choice(labels),
                          "glyph": rnd.choice(glyphs)})
    if not crowded:
        for i in range(int(n * cross_ratio)):
            a, b = rnd.randrange(n), rnd.randrange(n)
            if a == b or cid(a) == cid(b):
                continue
            edges.append({"id": f"x{i:04d}", "from": f"c{a:04d}", "to": f"c{b:04d}",
                          "label": "橋渡しの関係", "glyph": "wave"})
    return {"graph_version": f"kg_s{n}", "nodes": nodes, "edges": edges,
            "communities": [{"id": cid(i), "name": f"テーマ{i}"}
                            for i in range(0, n, per)]}


def _hopeless_kg(pairs: int = 3, dup: int = 14) -> dict:
    """**構造的に**逃げ場が無いグラフ (裁定 AC の検査用)。

    同じノード対を極端に長いラベル付きの多重エッジで結ぶ。ラベルの自然な位置
    (両端の中点) は全部同じ 1 点で、候補列も完全に一致するので、どのレイアウト
    エンジンで置こうと n-1 本は必ずどこかへ重なる — つまり「島を広げれば解ける」
    という v3 の逃げ道が原理的に無い。grid / semantic どちらでも同じ結論になる。
    """
    label = "非常に長い関係の説明テキストです"
    nodes: list[dict] = []
    edges: list[dict] = []
    for p in range(pairs):
        nodes += [{"id": f"h{p}a", "label": f"とても長い概念のラベル{p}A",
                   "community_id": f"g{p}"},
                  {"id": f"h{p}b", "label": f"とても長い概念のラベル{p}B",
                   "community_id": f"g{p}"}]
        edges += [{"id": f"m{p}_{k:02d}", "from": f"h{p}a", "to": f"h{p}b",
                   "label": label, "glyph": "arrow"} for k in range(dup)]
    return {"graph_version": "kg_hopeless", "nodes": nodes, "edges": edges,
            "communities": [{"id": f"g{p}", "name": f"島{p}"} for p in range(pairs)]}


def _load_session(name: str) -> dict:
    path = GRAPHS / f"layout_plan_session_{name}.json"
    if not path.exists():
        pytest.skip(f"実セッションの plan が無い: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _natural_center(edge: dict, nodes: dict) -> tuple[float, float]:
    r = edge_label_rect(edge, nodes)
    assert r is not None
    return ((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)


# --- 決定性 -------------------------------------------------------------

def test_planner_is_deterministic():
    """同じ入力からは常に同じ配置が出る (キャッシュを挟んでも変わらない)。"""
    plan = compute_layout(_synth_kg(60))
    clear_label_plan_cache()
    first = {k: v.to_dict() for k, v in plan_label_layout(plan).items()}
    clear_label_plan_cache()
    second = {k: v.to_dict() for k, v in plan_label_layout(copy.deepcopy(plan)).items()}
    assert first == second
    # キャッシュ経由でも同一
    assert {k: v.to_dict() for k, v in plan_label_layout(plan).items()} == first
    # 処理順に依らない = 返る辞書はエッジ id 順
    assert list(first) == sorted(first)


def test_planner_follows_the_candidate_order():
    """空いていれば自然な中点。動かす場合も必ず候補列の中から選ぶ。"""
    plan = compute_layout(_synth_kg(40))
    nodes = {n["id"]: n for n in plan["nodes"]}
    edges = {e["id"]: e for e in plan["edges"]}
    placements = plan_label_layout(plan)

    moved = 0
    for eid, pl in placements.items():
        edge = edges[eid]
        cands = _candidates(edge, nodes, pl.width, pl.height)
        # 候補列の先頭は「自然な中点」= Excalidraw の bound text の位置
        assert cands[0] == pytest.approx(_natural_center(edge, nodes))
        # 確定位置は必ず候補列のどれか
        assert any(abs(cx - pl.x) < 1e-6 and abs(cy - pl.y) < 1e-6
                   for cx, cy in cands), f"{eid} が候補列の外に置かれた"
        if pl.retreated:
            moved += 1
            assert (pl.x, pl.y) != pytest.approx(cands[0])
        else:
            assert (pl.x, pl.y) == pytest.approx(cands[0])
    assert moved > 0, "退避が 1 本も起きない構成ではこのテストの意味が無い"


# --- 障害物 3 種 (裁定 AA) ----------------------------------------------

def test_obstacle_node_ellipse():
    """中間ノードを飛び越すエッジのラベルは、そのノードを避けて置かれる。

    c0→c2 の中点はちょうど c1 の中心に重なる配置を直接組む
    (compute_layout の格子は 3 ノードを 2x2 に置くので横一列にならない)。
    """
    from cc_core.overlap import _intersects

    nodes = [{"id": f"c{i}", "label": f"ノード{i}", "x": i * 300, "y": 0,
              "size": 170, "height": 66, "community_id": "m1",
              "style": {"rough": True}} for i in range(3)]
    view = {
        "nodes": nodes,
        "edges": [{"id": "r1", "from": "c0", "to": "c2",
                   "label": "飛び越す関係", "glyph": "arrow"}],
        "islands": [],
    }
    # 前提: 自然な中点は c1 の楕円の中
    assert _intersects(edge_label_rect(view["edges"][0], {n["id"]: n for n in nodes}),
                       node_rect(nodes[1]))

    clear_label_plan_cache()
    pl = plan_label_layout(view)["r1"]
    assert pl.retreated
    for n in nodes:
        assert not _intersects(pl.rect, node_rect(n)), f"{n['id']} に重なっている"


def test_obstacle_island_title_band():
    """島タイトル帯 (bbox 上端 28px) はラベルの障害物になる。"""
    # 中点が帯の中に落ちる配置を作る。ノード自身は帯の x 範囲の外に置き、
    # 「帯だけが理由で動いた」ことを切り分ける
    base_nodes = [
        {"id": "a", "label": "A", "x": 0, "y": -19, "size": 40, "height": 66,
         "community_id": "m1", "style": {"rough": True}},
        {"id": "b", "label": "B", "x": 580, "y": -19, "size": 40, "height": 66,
         "community_id": "m1", "style": {"rough": True}},
    ]
    edge = {"id": "r1", "from": "a", "to": "b", "label": "帯にかかる", "glyph": "arrow"}
    without = {"nodes": base_nodes, "edges": [edge], "islands": []}
    band_island = {"community_id": "m1", "name": "帯",
                   "bbox": [200, 0, 400, 400], "is_gap": False}
    with_band = {"nodes": base_nodes, "edges": [edge], "islands": [band_island]}

    clear_label_plan_cache()
    free = plan_label_layout(without)["r1"]
    assert not free.retreated, "島が無ければ中点に置かれるはず"
    assert free.y == pytest.approx(14.0)  # 帯 (y 0..28) の中

    blocked = plan_label_layout(with_band)["r1"]
    assert blocked.retreated, "島タイトル帯を障害物として見ていない"
    band = (200.0, 0.0, 400.0, ISLAND_TITLE_BAND)
    from cc_core.overlap import _intersects
    assert not _intersects(blocked.rect, band)


def test_obstacle_already_placed_label():
    """先に置いたラベルは、後のラベルにとって障害物になる (v1 の見落とし)。"""
    # 中点がほぼ重なる 2 本の平行エッジ
    nodes = [
        {"id": "a", "label": "A", "x": 0, "y": 0, "size": 40, "height": 40,
         "community_id": "m1", "style": {"rough": True}},
        {"id": "b", "label": "B", "x": 600, "y": 0, "size": 40, "height": 40,
         "community_id": "m1", "style": {"rough": True}},
        {"id": "c", "label": "C", "x": 0, "y": 8, "size": 40, "height": 40,
         "community_id": "m1", "style": {"rough": True}},
        {"id": "d", "label": "D", "x": 600, "y": 8, "size": 40, "height": 40,
         "community_id": "m1", "style": {"rough": True}},
    ]
    view = {
        "nodes": nodes,
        "edges": [
            {"id": "r1", "from": "a", "to": "b", "label": "上の関係", "glyph": "arrow"},
            {"id": "r2", "from": "c", "to": "d", "label": "下の関係", "glyph": "arrow"},
        ],
        "islands": [],
    }
    clear_label_plan_cache()
    pls = plan_label_layout(view)
    from cc_core.overlap import _intersects
    assert not _intersects(pls["r1"].rect, pls["r2"].rect), "ラベル同士が重なったまま"
    assert pls["r1"].retreated or pls["r2"].retreated


# --- truncate と unresolved (裁定 AC) -----------------------------------

def test_truncate_never_shrinks_more_than_40_percent():
    """短縮候補は必ず元の表示幅の 60% 以上を保つ。"""
    text = "非常に長い関係ラベルの説明文がここに入る"
    base = display_width(text)
    variants = list(_text_variants(text))
    assert variants[0] == text, "先頭は必ず元の文字列"
    assert len(variants) > 1, "短縮候補が 1 つも作られていない"
    for v in variants[1:]:
        assert display_width(v) < base, "短縮になっていない"
        assert display_width(v) >= base * 0.6, f"40% を超えて縮んだ: {v!r}"


def test_truncate_kicks_in_when_every_candidate_is_blocked():
    """逃げ場が無いラベルは短縮して再挑戦する。"""
    plan = compute_layout(_hopeless_kg())
    placements = plan_label_layout(plan)
    truncated = [p for p in placements.values() if p.truncated]
    assert truncated, "敵対的な構成でも短縮が 1 本も起きていない"
    for p in truncated:
        assert p.text == p.truncated
        assert p.text.endswith("…")


def test_unresolved_is_reported_instead_of_silently_overlapping():
    """全滅したラベルは最少交差に置いたうえで必ず報告する (黙って重ねない)。"""
    plan = compute_layout(_hopeless_kg())
    report = check_overlaps(plan)
    assert report.unresolved_labels, "unresolved が 1 件も報告されていない"
    # v1 で未検査だった label_on_label がここで初めて可視化される
    assert report.label_on_label, "敵対的構成でラベル同士の衝突が検出されない"
    assert not report.clean
    reported = {u["edge"] for u in report.unresolved_labels}
    placements = plan_label_layout(plan)
    assert reported == {eid for eid, p in placements.items() if p.unresolved}
    for u in report.unresolved_labels:
        assert u["blocked_by"], "何に塞がれたのか報告されていない"
    # 報告された分はすべて to_dict にも載る (summary へ渡る形)
    assert report.to_dict()["unresolved_labels"] == report.unresolved_labels


# --- 二面 (canvas / SVG) の位置一致 (設計書 §2) -------------------------

def test_adapter_and_svg_place_labels_identically():
    """canvas と SVG が同じプランナー結果を使い、同じ座標に置く。"""
    import asyncio

    from cc_core.adapter import edge_element_id, render_layout_plan
    from cc_core.svg_export import MARGIN, _bounds, build_svg

    plan = _load_session("20260807_170447")
    nodes = {n["id"]: n for n in plan["nodes"]}
    placements = plan_label_layout(plan)
    assert any(p.retreated for p in placements.values()), "退避が無いと検査にならない"

    class _Recorder:
        def __init__(self) -> None:
            self.created: dict[str, dict] = {}

        async def call(self, tool: str, args: dict | None = None):
            if tool == "create_element" and args:
                self.created[args["id"]] = args
            return "{}"

    client = _Recorder()
    result = asyncio.run(render_layout_plan(plan, client))
    assert result.success, result.errors

    svg = build_svg(plan)
    x0, y0, _x1, _y1 = _bounds(plan)
    ox, oy = MARGIN - x0, MARGIN - y0
    svg_pos = {
        m.group(1): (float(m.group(2)), float(m.group(3)))
        for m in re.finditer(
            r'<text data-edge-id="([^"]+)"[^>]*?\sx="(-?[\d.]+)" y="(-?[\d.]+)"', svg)
    }

    for eid, pl in placements.items():
        # SVG: text の x/y はラベル矩形の中心 (:.0f 丸めのぶん 1px 許容)
        assert eid in svg_pos, f"SVG に {eid} のラベルが無い"
        sx, sy = svg_pos[eid]
        assert abs((sx - ox) - pl.x) <= 1.0 and abs((sy - oy) - pl.y) <= 1.0, \
            f"{eid}: SVG {sx - ox, sy - oy} != planner {pl.x, pl.y}"

        label_el = client.created.get(edge_element_id(eid) + "-label")
        if pl.retreated:
            assert label_el is not None, f"{eid}: 退避ラベルが canvas に無い"
            # canvas: text は左上原点。中心へ戻すと planner と一致する
            cx = label_el["x"] + pl.width / 2
            cy = label_el["y"] + pl.height / 2
            assert (cx, cy) == pytest.approx((pl.x, pl.y))
        else:
            # 退避しないラベルは線の bound text (= 自然な中点)
            assert label_el is None
            assert client.created[edge_element_id(eid)].get("text")
            assert (pl.x, pl.y) == pytest.approx(_natural_center(
                next(e for e in plan["edges"] if e["id"] == eid), nodes))


# --- 実測: 実セッション 3 つ (受け入れ基準 2) ---------------------------

@pytest.mark.parametrize("session", REAL_SESSIONS)
def test_real_sessions_have_no_label_overlap(session):
    """実セッションで label_on_node / label_on_label = 0 件。

    v1 は 170447/143804 で各 3 件、010128 で 1 件を報告していた。
    """
    from cc_core.detail import project

    plan = _load_session(session)
    for view in [plan] + [project(plan, lv) for lv in plan.get("levels", {})]:
        report = check_overlaps(view)
        assert report.label_on_node == [], f"{session}: {report.label_on_node}"
        assert report.label_on_label == [], f"{session}: {report.label_on_label}"
        assert report.unresolved_labels == []


@pytest.mark.parametrize("session", REAL_SESSIONS)
def test_node_coordinates_are_never_touched(session):
    """裁定 AB: ラベル配置はノード座標・寸法を 1px も動かさない。"""
    from cc_core.svg_export import build_svg

    plan = _load_session(session)
    before = copy.deepcopy(plan["nodes"])
    plan_label_layout(plan)
    check_overlaps(plan)
    build_svg(plan)
    assert plan["nodes"] == before
    for a, b in zip(before, plan["nodes"]):
        assert (a["x"], a["y"], a["size"], a.get("height")) == \
               (b["x"], b["y"], b["size"], b.get("height"))


# --- スケール (合成 100/200/400) ----------------------------------------

@pytest.mark.parametrize("n", [100, 200, 400])
def test_synthetic_scale_stays_clean(n):
    """100/200/400 ノードでもラベル/ノード・ラベル/ラベルとも 0 件。"""
    plan = compute_layout(_synth_kg(n))
    report = check_overlaps(plan)
    assert report.label_on_node == [], report.label_on_node[:5]
    assert report.label_on_label == [], report.label_on_label[:5]
    assert report.node_on_node == []
    assert report.unresolved_labels == []
    assert report.clean


# --- 後方互換 -----------------------------------------------------------

def test_r15_generation_plan_still_works():
    """R1.5 世代の plan (levels/height 無し) でもプランナーが動く。"""
    plan = json.loads((FIXTURES / "layout_plan_min.json").read_text(encoding="utf-8"))
    assert "levels" not in plan and "height" not in plan["nodes"][0]
    placements = plan_label_layout(plan)
    assert placements
    for pl in placements.values():
        assert isinstance(pl, LabelPlacement)
        assert math.isfinite(pl.x) and math.isfinite(pl.y)
        assert pl.height == pytest.approx(EDGE_FONT * 1.25)
    assert check_overlaps(plan).clean


def test_resolve_label_offset_keeps_backward_compatibility():
    """単発呼び出しは従来動作、プランナー通過後はその結果を返す。"""
    plan = compute_layout(_synth_kg(40))
    nodes = {n["id"]: n for n in plan["nodes"]}
    edges = {e["id"]: e for e in plan["edges"]}

    # 1) キャッシュが無い状態 = 従来の逐次アルゴリズム
    clear_label_plan_cache()
    legacy = {eid: resolve_label_offset(e, nodes) for eid, e in edges.items()}
    assert any(v is None for v in legacy.values())

    # 2) プランナーを通した後は二面と同じ位置を返す
    placements = plan_label_layout(plan)
    for eid, e in edges.items():
        got = resolve_label_offset(e, nodes)
        pl = placements[eid]
        if pl.retreated:
            assert got == pytest.approx((pl.x, pl.y))
        else:
            assert got is None
    assert legacy != {eid: (None if not placements[eid].retreated
                            else (placements[eid].x, placements[eid].y))
                      for eid in edges}, "プランナーが従来と同じ答えしか出していない"


def test_unresolved_labels_survive_the_layout_plan_schema():
    """plan に載せる形が schema を通る (root は additionalProperties:false)。"""
    plan = compute_layout(_hopeless_kg())
    report = check_overlaps(plan)
    assert report.unresolved_labels
    # pipeline が書く形と同じ (level を足す)
    plan["unresolved_labels"] = [{"level": "standard", **u}
                                 for u in report.unresolved_labels]
    result = validate_layout_plan(plan)
    assert result.valid, result.errors[:3]


def test_render_cli_warns_about_unresolved_labels(tmp_path, monkeypatch, capsys):
    """受け入れ基準 4: 逃げ場が無いときは描画経路でも黙らず警告する。"""
    from cc_orchestrator import chat

    plan = compute_layout(_hopeless_kg())
    plan_file = tmp_path / "layout_plan_session_crowded.json"
    plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    class _FakeExecutor:
        def __init__(self, target: str = "local") -> None:
            pass

        def tool_render_layout_plan(self, args: dict) -> dict:
            return {"success": True, "created": ["a"], "errors": []}

    monkeypatch.setattr(chat, "ToolExecutor", _FakeExecutor)
    monkeypatch.setattr(sys, "argv", ["chat.py", "--render", str(plan_file)])
    chat.main()

    out = capsys.readouterr().out
    assert "ラベルの重なり" in out
    assert "逃げ場なし" in out
