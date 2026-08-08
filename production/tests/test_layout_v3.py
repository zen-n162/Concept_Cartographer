"""レイアウト v3 バッチ L1 のテスト (docs/layout-v3-design.md §7)。

L1 の受け入れは 2 本立て:
  1. **既定 (grid) の生成物が 1 バイトも変わらない** — フラグを立てるまで
     本番挙動は完全不変、が憲法 (§8-1)
  2. flag=semantic のとき、決定的で・重なりゼロで・島の中に収まり・
     解けなければ黙らずに grid へ退避する
"""

from __future__ import annotations

import json
import math
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from test_overlap import _synth_kg                     # noqa: E402  合成 KG を流用

from cc_core import layout_v3                          # noqa: E402
from cc_core.community import analyze                  # noqa: E402
from cc_core.detail import _level_kg, build_multilevel_plan, project  # noqa: E402
from cc_core.layout import compute_layout              # noqa: E402
from cc_core.overlap import check_overlaps, clear_label_plan_cache  # noqa: E402
from cc_core.validate import validate_layout_plan      # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def semantic(monkeypatch):
    """v3 を有効にする。engine は呼び出しのたびに読まれる (§0)。"""
    monkeypatch.setenv("CC_LAYOUT_ENGINE", "semantic")
    clear_label_plan_cache()
    yield
    clear_label_plan_cache()


def _kg(n: int = 12, comms: int = 3) -> dict:
    nodes = [{"id": f"n{i:03d}", "label": f"概念ラベル{i}",
              "community_id": f"c{i % comms}"} for i in range(n)]
    edges = [{"id": f"e{i:03d}", "from": f"n{i:03d}", "to": f"n{(i + comms) % n:03d}",
              "label": "関係の説明", "glyph": "arrow"} for i in range(n)]
    return {"graph_version": "kg_v3", "nodes": nodes, "edges": edges,
            "communities": [{"id": f"c{i}", "name": f"テーマ{i}"} for i in range(comms)]}


def _dense_island_kg(k: int = 40) -> dict:
    """スイープが 30 パスでは解けない敵対的な密集島 (§2 の段階フォールバック)。

    全対エッジ + 長いラベルなので必要長どうしが押し合い、決定的に振動する。
    k=40 が実測での発火点 (K30 は解ける)。

    glyph は **wave** — スイープを通るのは KK 島だけで、因果 (arrow) にすると
    L2 の骨格選択が層状へ回してしまい、この島は「解ける」ようになる (層状は
    grid の列間隔規則で px を決めるのでスイープを使わない)。
    """
    label = "非常に長い関係の説明テキストです"
    nodes = [{"id": f"d{i:03d}", "label": f"とても長い概念のラベル{i}",
              "community_id": "c0"} for i in range(k)]
    edges = [{"id": f"e{i:03d}_{j:03d}", "from": f"d{i:03d}", "to": f"d{j:03d}",
              "label": label, "glyph": "wave"}
             for i in range(k) for j in range(i + 1, k)]
    return {"graph_version": "kg_dense", "nodes": nodes, "edges": edges,
            "communities": [{"id": "c0", "name": "密集"}]}


# --------------------------------------------------------------------------
# 1. 既定 (grid) が完全に不変であること
# --------------------------------------------------------------------------

def test_grid_output_is_byte_identical_to_the_pre_v3_baseline(monkeypatch):
    """grid エンジンの生成物が v3 導入前と**バイト単位で**一致する (§8-1)。

    fixtures/grid_golden_plan.json は v3 の実装前 (ba34d10) のコードで
    出力したもの。compute_layout と build_multilevel_plan の両方を含む。
    L3 で既定が semantic に倒れてもこの契約 (退避先の grid は不変) は残るので、
    エンジンは環境に頼らず明示的に固定する。
    """
    monkeypatch.setenv("CC_LAYOUT_ENGINE", "grid")
    kg = json.loads((FIXTURES / "kg_sample.json").read_text(encoding="utf-8"))
    fresh = json.dumps({
        "compute_layout": compute_layout(kg, detail_level="standard"),
        "build_multilevel_plan": build_multilevel_plan(kg, default_level="standard"),
    }, ensure_ascii=False, indent=2, sort_keys=False)
    golden = (FIXTURES / "grid_golden_plan.json").read_text(encoding="utf-8")
    assert fresh == golden


def test_default_engine_stays_on_the_grid_path(monkeypatch):
    """フラグ未設定・空・未知の値はすべて grid (誤記で本番が変わらないこと)。"""
    for value in (None, "", "  ", "GRID", "kk", "semantic-ish"):
        if value is None:
            monkeypatch.delenv("CC_LAYOUT_ENGINE", raising=False)
        else:
            monkeypatch.setenv("CC_LAYOUT_ENGINE", value)
        assert layout_v3.engine_name() == "grid"
        plan = compute_layout(_kg())
        assert plan["provenance"]["layout_engine"] == "cc_core.layout/0.2 text-aware-grid"
        assert all("layout_mode" not in i for i in plan["islands"])


# --------------------------------------------------------------------------
# 2. 決定性 (§8-2)
# --------------------------------------------------------------------------

def test_compute_layout_is_deterministic_under_semantic(semantic):
    kg = _kg(24, comms=4)
    a = json.dumps(compute_layout(kg), ensure_ascii=False, sort_keys=False)
    b = json.dumps(compute_layout(kg), ensure_ascii=False, sort_keys=False)
    assert a == b
    assert layout_v3.LAYOUT_ENGINE_ID in a


def test_build_multilevel_plan_is_deterministic_under_semantic(semantic):
    kg = json.loads((FIXTURES / "kg_sample.json").read_text(encoding="utf-8"))
    a = json.dumps(build_multilevel_plan(kg), ensure_ascii=False)
    b = json.dumps(build_multilevel_plan(kg), ensure_ascii=False)
    assert a == b


# --------------------------------------------------------------------------
# 3. スケール 100/200/400 で重なりゼロ + unresolved 0 (§8-3)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("n", [100, 200, 400])
def test_semantic_scale_stays_clean(semantic, n):
    plan = compute_layout(_synth_kg(n))
    clear_label_plan_cache()
    report = check_overlaps(plan)
    assert report.node_on_node == [], report.node_on_node[:5]
    assert report.label_on_node == [], report.label_on_node[:5]
    assert report.label_on_label == [], report.label_on_label[:5]
    assert report.node_outside_island == [], report.node_outside_island[:5]
    assert report.unresolved_labels == [], report.unresolved_labels[:5]
    assert report.clean


# --------------------------------------------------------------------------
# 4-6. 島の中に収まる / 島が重ならない / 正座標
# --------------------------------------------------------------------------

def test_every_node_sits_inside_its_own_island(semantic):
    plan = compute_layout(_synth_kg(200))
    islands = {i["community_id"]: i["bbox"] for i in plan["islands"]}
    for n in plan["nodes"]:
        x0, y0, x1, y1 = islands[n["community_id"]]
        assert x0 <= n["x"] and n["x"] + n["size"] <= x1, n["id"]
        assert y0 <= n["y"] and n["y"] + n["height"] <= y1, n["id"]


def test_islands_do_not_overlap(semantic):
    boxes = [i["bbox"] for i in compute_layout(_synth_kg(200))["islands"]]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            assert (a[2] <= b[0] or b[2] <= a[0]
                    or a[3] <= b[1] or b[3] <= a[1]), (a, b)


def test_coordinates_are_positive_integers(semantic):
    plan = compute_layout(_synth_kg(100))
    for n in plan["nodes"]:
        assert isinstance(n["x"], int) and isinstance(n["y"], int), n["id"]
        assert n["x"] > 0 and n["y"] > 0, n["id"]
    for i in plan["islands"]:
        assert all(isinstance(v, int) and v > 0 for v in i["bbox"]), i


# --------------------------------------------------------------------------
# 7. 解けない島は grid へ退避し、その事実が残る (§2 / §8-6)
# --------------------------------------------------------------------------

def test_unsolvable_island_falls_back_to_grid_and_is_recorded(semantic):
    plan = compute_layout(_dense_island_kg())
    island = plan["islands"][0]
    assert island["layout_mode"] == "grid_fallback"
    # 退避先は grid そのものなので、可読性の契約はそのまま生きている
    assert {n["community_id"] for n in plan["nodes"]} == {"c0"}
    x0, y0, x1, y1 = island["bbox"]
    for n in plan["nodes"]:
        assert x0 <= n["x"] and n["x"] + n["size"] <= x1
        assert y0 <= n["y"] and n["y"] + n["height"] <= y1
    # summary に必ず出る (黙らない)。sweeps_max = 使い切った 30 パス (§6)
    assert layout_v3.layout_summary(plan) == {
        "engine": "semantic", "islands": {"semantic": 0, "grid_fallback": 1},
        "sweeps_max": layout_v3.SWEEP_MAX_PASSES}


def test_layout_mode_survives_the_layout_plan_schema(semantic):
    """3 点セットのうち schema 側。閉じたオブジェクトなので登録漏れは即死する。"""
    plan = compute_layout(_kg())
    assert all(i["layout_mode"] == "semantic" for i in plan["islands"])
    check = validate_layout_plan(plan)
    assert check.valid, check.errors[:3]


def test_layout_mode_and_summary_reach_the_web_view_and_the_cli(semantic, tmp_path,
                                                                monkeypatch, capsys):
    """3 点セットの残り 2 つ: sessions.py の島通過と summary/CLI への露出。

    Web は plan から作った view しか見ないので、そこまで届いて初めて
    「どう組まれた島か」を出せる。
    """
    from cc_orchestrator import chat, pipeline
    from cc_web import sessions

    monkeypatch.chdir(tmp_path)
    (tmp_path / "kg.json").write_text(
        (FIXTURES / "kg_sample.json").read_text(encoding="utf-8"), encoding="utf-8")
    summary = pipeline.run_pipeline(
        "今週の研究を概念地図として整理して", target="file",
        kg_file=str(tmp_path / "kg.json"), offline=True, layers=False,
        verify_causal=False, export_svg=False)

    assert summary["layout"]["engine"] == "semantic"
    assert summary["layout"]["islands"]["grid_fallback"] == 0
    assert summary["layout"]["saved"]          # 既存の保存先報告も残っている

    view = sessions.view_of(summary["session"], "standard")
    assert view["islands"] and all(i["layout_mode"] == "semantic"
                                   for i in view["islands"])

    # 退避 0 件のときは CLI に 1 行も増やさない / 起きたら必ず出す
    chat._print_summary(summary)
    assert "グリッドへ退避" not in capsys.readouterr().out
    noisy = dict(summary, layout={"engine": "semantic",
                                  "islands": {"semantic": 3, "grid_fallback": 2}})
    chat._print_summary(noisy)
    assert "2/5 島は制約が解けずグリッドへ退避" in capsys.readouterr().out


def test_layout_summary_counts_islands_across_levels(semantic):
    kg = json.loads((FIXTURES / "kg_sample.json").read_text(encoding="utf-8"))
    plan = build_multilevel_plan(kg)
    got = layout_v3.layout_summary(plan)
    assert got["engine"] == "semantic"
    assert got["islands"]["grid_fallback"] == 0
    assert got["islands"]["semantic"] == sum(
        len(project(plan, lv)["islands"]) for lv in ("detailed", "overview", "standard"))
    # grid で作った plan は退避 0・engine=grid と報告される
    assert layout_v3.layout_summary(
        {"islands": [{"community_id": "c0", "name": "x", "bbox": [0, 0, 1, 1]}]}
    ) == {"engine": "grid", "islands": {"semantic": 0, "grid_fallback": 0},
          "sweeps_max": 0}


# --------------------------------------------------------------------------
# 8. importance の配管 (§4 — サイズ反映は L3、ここは「届くこと」だけ)
# --------------------------------------------------------------------------

def test_importance_reaches_the_level_kg_that_layout_sees():
    kg = json.loads((FIXTURES / "kg_sample.json").read_text(encoding="utf-8"))
    analysis = analyze(kg)
    level_kg, _aggs = _level_kg(kg, analysis, "standard")
    scored = [n for n in level_kg["nodes"] if "importance" in n]
    assert scored, "レイアウトが importance を読めない (配管が切れている)"
    for n in scored:
        assert set(n["importance"]) == {"betweenness", "frequency", "novelty", "total"}
        assert 0.0 <= n["importance"]["total"] <= 1.0
    # plan 側の値と一致する (二重に持っても食い違わない)
    plan = build_multilevel_plan(kg)
    by_id = {n["id"]: n for n in plan["_level_plans"]["standard"]["nodes"]}
    for n in scored:
        assert by_id[n["id"]]["importance"] == n["importance"]


# --------------------------------------------------------------------------
# 9. ノード ≤3 の島は横一列 (§1c)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("k", [1, 2, 3])
def test_small_islands_are_laid_out_in_a_single_row(semantic, k):
    nodes = [{"id": f"s{i}", "label": f"概念{i}", "community_id": "c0"}
             for i in range(k)]
    edges = [{"id": f"e{i}", "from": f"s{i}", "to": f"s{i + 1}",
              "label": "関係の説明", "glyph": "arrow"} for i in range(k - 1)]
    plan = compute_layout({"graph_version": "kg_small", "nodes": nodes, "edges": edges,
                           "communities": [{"id": "c0", "name": "小島"}]})
    placed = sorted(plan["nodes"], key=lambda n: n["x"])
    assert [n["id"] for n in placed] == [f"s{i}" for i in range(k)]
    # 縦中心が揃っている = 一列
    centers = {n["y"] + n["height"] / 2 for n in placed}
    assert len(centers) == 1
    # 隣どうしの隙間はエッジラベルが載る幅を確保している (grid と同じ規則)
    for a, b in zip(placed, placed[1:]):
        assert b["x"] - (a["x"] + a["size"]) >= 28

# ==========================================================================
# バッチ L2 (docs/layout-v3-design.md §1 骨格選択 / §1a 層状 / §1b 木 /
#             §3 島パッキング / §3a レベル間アンカー / §6 sweeps_max)
# ==========================================================================

from cc_core import island_packing                     # noqa: E402
from cc_core.community import LEVEL_ORDER, analyze     # noqa: E402
from cc_core.layout import (COL_MARGIN, ISLAND_GAP_X,  # noqa: E402
                            ISLAND_GAP_Y, edge_label_px)


def _causal_kg() -> dict:
    """因果の骨格がはっきりした島 (分岐と合流つき・閉路なし)。"""
    edges = [("a0", "a1"), ("a1", "a2"), ("a2", "a3"), ("a1", "a4"),
             ("a4", "a3"), ("a3", "a5"), ("a0", "a2")]
    return {
        "graph_version": "kg_causal",
        "nodes": [{"id": f"a{i}", "label": f"原因と結果の概念{i}",
                   "community_id": "c0"} for i in range(6)],
        "edges": [{"id": f"e{i:02d}", "from": a, "to": b, "label": "促進する",
                   "glyph": "arrow"} for i, (a, b) in enumerate(edges)],
        "communities": [{"id": "c0", "name": "因果テーマ"}],
    }


def _hier_kg() -> dict:
    """isa の階層島。from = 子 / to = 親 (UML と同じ向き)。"""
    pairs = [("t1", "t0"), ("t2", "t0"), ("t3", "t1"), ("t4", "t1"), ("t5", "t2")]
    return {
        "graph_version": "kg_hier",
        "nodes": [{"id": f"t{i}", "label": f"分類の概念{i}", "community_id": "c0"}
                  for i in range(6)],
        "edges": [{"id": f"h{i:02d}", "from": a, "to": b, "label": "の一種",
                   "glyph": "isa"} for i, (a, b) in enumerate(pairs)],
        "communities": [{"id": "c0", "name": "階層テーマ"}],
    }


def _island_centers(plan: dict) -> dict[str, tuple[float, float]]:
    return {i["community_id"]: ((i["bbox"][0] + i["bbox"][2]) / 2,
                                (i["bbox"][1] + i["bbox"][3]) / 2)
            for i in plan["islands"]}


# --------------------------------------------------------------------------
# 10. §1a 層状 (因果島)
# --------------------------------------------------------------------------

def test_causal_island_is_layered_and_every_arrow_moves_right(semantic):
    """因果島は層状になり、骨格エッジは必ず左→右に進む (ユーザー決定)。"""
    plan = compute_layout(_causal_kg())
    nodes = {n["id"]: n for n in plan["nodes"]}
    assert plan["islands"][0]["layout_mode"] == "semantic"
    # 層状は grid の列間隔規則で決まるのでスイープを使わない
    assert plan["islands"][0]["sweeps"] == 0

    for e in plan["edges"]:
        assert nodes[e["from"]]["x"] < nodes[e["to"]]["x"], e["id"]
    # 最長路 a0→a1→a2/a4→a3→a5 の 5 層ぶん、列が立っている
    assert len({n["x"] for n in plan["nodes"]}) >= 4
    # 同じ層のノードは同じ列 (x が一致) に積まれる
    assert nodes["a2"]["x"] == nodes["a4"]["x"]


def test_layered_columns_keep_the_grid_gap_rule_for_edge_labels(semantic):
    """隣接層のスキマは grid と同じく「そこに載るラベルの幅」以上 (§1a-3)。"""
    kg = _causal_kg()
    for e in kg["edges"]:
        e["label"] = "とても長い関係の説明"
    plan = compute_layout(kg)
    nodes = {n["id"]: n for n in plan["nodes"]}
    checked = 0
    for e in plan["edges"]:
        a, b = nodes[e["from"]], nodes[e["to"]]
        gap = b["x"] - (a["x"] + a["size"])
        need = edge_label_px(e["label"], e["glyph"])
        if gap < need + COL_MARGIN:            # 層を跨ぐエッジは余裕が増えるだけ
            assert gap >= need, f"{e['id']}: スキマ {gap} < ラベル幅 {need}"
        checked += 1
    assert checked == len(kg["edges"])


def test_cycle_breaking_drops_the_lowest_confidence_edge(semantic):
    """§1a-1 閉路切断は confidence 昇順・同点は edge id 順で決定的。"""
    def edge(eid, a, b, conf=None):
        e = {"id": eid, "from": a, "to": b, "label": "促す", "glyph": "arrow"}
        if conf is not None:
            e["confidence"] = conf
        return e

    ring = [edge("z1", "n0", "n1", 0.9), edge("z2", "n1", "n2", 0.8),
            edge("z3", "n2", "n0", 0.3)]
    kept = layout_v3._acyclic_skeleton(ring)
    assert [e["id"] for e in kept] == ["z1", "z2"]      # 一番弱い z3 が外れる

    # confidence 欠損は 0.5 扱い (0.4 より強く、0.6 より弱い)
    ring2 = [edge("y1", "n0", "n1", 0.6), edge("y2", "n1", "n2"),
             edge("y3", "n2", "n0", 0.4)]
    assert [e["id"] for e in layout_v3._acyclic_skeleton(ring2)] == ["y1", "y2"]

    # 同点は edge id の辞書順で残す (後ろの id が外れる)
    ring3 = [edge("b", "n0", "n1", 0.5), edge("a", "n1", "n0", 0.5)]
    assert [e["id"] for e in layout_v3._acyclic_skeleton(ring3)] == ["a"]

    # 閉路つきでも plan は決定的で、残った骨格は左→右
    kg = {"graph_version": "kg_ring",
          "nodes": [{"id": f"n{i}", "label": f"循環する概念{i}",
                     "community_id": "c0"} for i in range(3)] +
                   [{"id": "n3", "label": "循環する概念3", "community_id": "c0"}],
          "edges": ring + [edge("z4", "n2", "n3", 0.7)],
          "communities": [{"id": "c0", "name": "循環"}]}
    first = json.dumps(compute_layout(kg), ensure_ascii=False)
    assert first == json.dumps(compute_layout(kg), ensure_ascii=False)
    nodes = {n["id"]: n for n in json.loads(first)["nodes"]}
    assert nodes["n0"]["x"] < nodes["n1"]["x"] < nodes["n2"]["x"] < nodes["n3"]["x"]


# --------------------------------------------------------------------------
# 11. §1b 木 (階層島)
# --------------------------------------------------------------------------

def test_hierarchical_island_puts_the_abstract_parent_on_top(semantic):
    """isa/partof の島は木になり、親 (= to 側) が必ず上に来る (§1b)。"""
    plan = compute_layout(_hier_kg())
    nodes = {n["id"]: n for n in plan["nodes"]}
    assert plan["islands"][0]["layout_mode"] == "semantic"

    def cy(nid: str) -> float:
        return nodes[nid]["y"] + nodes[nid]["height"] / 2

    for e in plan["edges"]:
        assert cy(e["to"]) < cy(e["from"]), f"{e['id']}: 親が下に来ている"
    # 根はいちばん上、葉はいちばん下
    assert cy("t0") == min(cy(n) for n in nodes)
    assert cy("t3") > cy("t1") > cy("t0")


def test_skeleton_selection_follows_the_fifty_percent_boundary(semantic):
    """§1 の表を上から評価する。境界 (ちょうど半分) は骨格側が勝つ。"""
    members = [{"id": f"n{i}"} for i in range(4)]

    def edges(*glyphs, label="関係"):
        return [{"id": f"e{i}", "from": "n0", "to": "n1",
                 "label": label, "glyph": g} for i, g in enumerate(glyphs)]

    kind = layout_v3._skeleton_kind
    assert kind(members[:3], edges("arrow")) == "row"          # ≤3 が最優先
    assert kind(members, edges("arrow", "arrow", "wave", "wave")) == "layered"
    assert kind(members, edges("arrow", "wave", "wave", "wave")) == "kk"
    assert kind(members, edges("isa", "partof", "wave", "wave")) == "tree"
    assert kind(members, edges("isa", "wave", "wave", "wave")) == "kk"
    # 因果と階層が拮抗したら因果が勝つ (表が上から評価だから)
    assert kind(members, edges("arrow", "arrow", "isa", "isa")) == "layered"
    # ラベルの無いエッジは E に数えない → 骨格が実在しなければ KK
    assert kind(members, edges("arrow", "wave", label="")) == "kk"


# --------------------------------------------------------------------------
# 12. §3 島パッキング
# --------------------------------------------------------------------------

def test_packing_separates_islands_by_exactly_the_declared_gap():
    """目標が完全に重なっても、接触候補点で ISLAND_GAP ぴったりに詰める (§3-3)。"""
    islands = [island_packing.Island("c0", 400.0, 300.0),
               island_packing.Island("c1", 400.0, 300.0),
               island_packing.Island("c2", 200.0, 200.0)]
    # アンカーを全部 (0,0) にすると 3 島の目標位置が 1 点に重なる
    anchors = {"offsets": {i.cid: (0.0, 0.0) for i in islands}, "area": 1.0}
    pos = island_packing.pack_islands(islands, anchors=anchors)
    boxes = {i.cid: (pos[i.cid][0], pos[i.cid][1],
                     pos[i.cid][0] + i.width, pos[i.cid][1] + i.height)
             for i in islands}
    ids = sorted(boxes)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = boxes[ids[i]], boxes[ids[j]]
            assert (a[2] <= b[0] or b[2] <= a[0]
                    or a[3] <= b[1] or b[3] <= a[1]), (a, b)
    # 面積最大の 2 島は隣どうしにぴったり (= 宣言どおりの隙間) 付いている
    gaps = [abs(boxes["c1"][0] - boxes["c0"][2]), abs(boxes["c0"][0] - boxes["c1"][2]),
            abs(boxes["c1"][1] - boxes["c0"][3]), abs(boxes["c0"][1] - boxes["c1"][3])]
    assert ISLAND_GAP_X in gaps or ISLAND_GAP_Y in gaps
    # 全体は ORIGIN から始まる (§3-4)
    assert min(b[0] for b in boxes.values()) == 60
    assert min(b[1] for b in boxes.values()) == 80


def test_island_packing_is_deterministic_and_keeps_islands_apart(semantic):
    """メタ KK → パッキングが二度同じ答えを出し、島は重ならない (§3)。"""
    from test_overlap import _synth_kg as synth

    a = compute_layout(synth(200))
    b = compute_layout(synth(200))
    assert json.dumps(a, ensure_ascii=False) == json.dumps(b, ensure_ascii=False)

    boxes = [i["bbox"] for i in a["islands"]]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            p, q = boxes[i], boxes[j]
            assert (p[2] <= q[0] or q[2] <= p[0]
                    or p[3] <= q[1] or q[3] <= p[1]), (p, q)
    # 関係のある島は「詰まって」いる: 隣接ギャップが宣言値どおりの組がある
    touching = sum(
        1 for i in range(len(boxes)) for j in range(len(boxes)) if i != j
        and (boxes[j][0] - boxes[i][2] == ISLAND_GAP_X
             or boxes[j][1] - boxes[i][3] == ISLAND_GAP_Y))
    assert touching, "どの島も接触しておらず、パッキングが働いていない"


# --------------------------------------------------------------------------
# 13. §3a レベル間アンカー
# --------------------------------------------------------------------------

def test_island_directions_survive_a_level_switch(semantic):
    """同じ島がレベルを跨いでも地図の反対側へ飛ばない (実測バグの回帰・§8-5)。"""
    from test_overlap import _synth_kg as synth

    plan = build_multilevel_plan(synth(120))
    levels = {lv: _island_centers(plan["_level_plans"][lv]) for lv in LEVEL_ORDER}
    base = levels["detailed"]

    def offsets(centers, keys):
        gx = sum(centers[k][0] for k in keys) / len(keys)
        gy = sum(centers[k][1] for k in keys) / len(keys)
        return {k: (centers[k][0] - gx, centers[k][1] - gy) for k in keys}

    for level in ("standard", "overview"):
        common = sorted(set(base) & set(levels[level]))
        assert len(common) >= 4, level
        ref, cur = offsets(base, common), offsets(levels[level], common)
        radius = max(math.hypot(*ref[k]) for k in common)
        for k in common:
            r = math.hypot(*ref[k])
            if r < 0.25 * radius:
                continue          # 重心の近くは方位そのものが意味を持たない
            angle = math.degrees(math.atan2(ref[k][1], ref[k][0])
                                 - math.atan2(cur[k][1], cur[k][0])) % 360
            assert min(angle, 360 - angle) < 45, (level, k, angle)
        # 左右の並び順も保たれている
        agree = sum(1 for i in range(len(common)) for j in range(i + 1, len(common))
                    if (ref[common[i]][0] - ref[common[j]][0])
                    * (cur[common[i]][0] - cur[common[j]][0]) > 0)
        total = len(common) * (len(common) - 1) // 2
        assert agree >= 0.8 * total, (level, agree, total)


def test_anchor_targets_shrink_with_the_square_root_of_the_area(semantic):
    """§3a: 他レベルの目標 = anchor × √(面積比)。未知の島は重心へ。"""
    plan = compute_layout(_kg(12, comms=3))
    anchors = island_packing.anchors_from_plan(plan)
    assert set(anchors["offsets"]) == {i["community_id"] for i in plan["islands"]}
    # 重心からの変位なので総和はゼロ
    assert sum(v[0] for v in anchors["offsets"].values()) == pytest.approx(0, abs=1e-6)

    islands = [island_packing.Island(cid, 100.0, 100.0)
               for cid in sorted(anchors["offsets"])]
    quarter = dict(anchors, area=anchors["area"] * 4)     # 1/4 の面積 → 1/2 の距離
    targets = island_packing._anchor_targets(
        {i.cid: i for i in islands}, [i.cid for i in islands], quarter)
    for cid, (dx, dy) in anchors["offsets"].items():
        ratio = math.sqrt(len(islands) * 100.0 * 100.0 / (anchors["area"] * 4))
        assert targets[cid] == pytest.approx((dx * ratio, dy * ratio))

    # detailed に無かった島は重心 (0, 0) が目標 / 消えた島は無視される
    extra = island_packing.Island("comm_new", 100.0, 100.0)
    grown = island_packing._anchor_targets(
        {extra.cid: extra}, [extra.cid],
        {"offsets": dict(anchors["offsets"], gone=(9e9, 9e9)), "area": anchors["area"]})
    assert grown["comm_new"] == (0.0, 0.0)


def test_detailed_is_computed_first_but_the_plan_keeps_the_canonical_order(semantic):
    """§3a の計算順変更が plan の並びに漏れないこと (既存テストが依存)。"""
    kg = json.loads((FIXTURES / "kg_sample.json").read_text(encoding="utf-8"))
    plan = build_multilevel_plan(kg)
    assert tuple(plan["_level_plans"]) == LEVEL_ORDER
    assert tuple(plan["levels"]) == LEVEL_ORDER
    # detailed 先行でも各レベルの中身は「そのレベルだけを見た」ときと同じ島集合
    analysis = analyze(kg)
    for level in LEVEL_ORDER:
        level_kg, _aggs = _level_kg(kg, analysis, level)
        alone = compute_layout(level_kg, detail_level=level)
        assert ([i["community_id"] for i in alone["islands"]]
                == [i["community_id"] for i in plan["_level_plans"][level]["islands"]])


# --------------------------------------------------------------------------
# 14. §6 summary (sweeps_max と退避理由)
# --------------------------------------------------------------------------

def test_summary_reports_the_sweep_high_water_mark(semantic):
    """sweeps_max = 島ごとのスイープ回数の最大 (§6・L1 検収の指摘)。"""
    from test_overlap import _synth_kg as synth

    plan = build_multilevel_plan(synth(120))
    got = layout_v3.layout_summary(plan)
    per_island = [isl.get("sweeps", 0) for lv in LEVEL_ORDER
                  for isl in plan["_level_plans"][lv]["islands"]]
    assert got["sweeps_max"] == max(per_island)
    assert got["sweeps_max"] <= layout_v3.SWEEP_MAX_PASSES
    # KK 島が 1 つでもあれば 1 パス以上は必ず回る
    assert any(s > 0 for s in per_island)


def test_missing_igraph_is_recorded_as_a_distinct_fallback_reason(semantic, monkeypatch):
    """igraph 不在とスイープ不能を summary で見分けられる (L1 の申し送り)。"""
    monkeypatch.setattr(layout_v3, "_igraph", lambda: None)
    plan = compute_layout(_kg(12, comms=3))
    assert all(i["layout_mode"] == "grid_fallback_no_igraph" for i in plan["islands"])
    summary = layout_v3.layout_summary(plan)
    assert summary["islands"] == {"semantic": 0, "grid_fallback": 3,
                                  "grid_fallback_no_igraph": 3}
    # 退避先は grid そのものなので、ノードは島の中に収まったまま
    islands = {i["community_id"]: i["bbox"] for i in plan["islands"]}
    for n in plan["nodes"]:
        x0, y0, x1, y1 = islands[n["community_id"]]
        assert x0 <= n["x"] and n["x"] + n["size"] <= x1
        assert y0 <= n["y"] and n["y"] + n["height"] <= y1


@pytest.mark.parametrize("shape", ["causal", "hier"])
@pytest.mark.parametrize("k", [60, 150])
def test_new_skeletons_stay_clean_at_scale(semantic, shape, k):
    """層状・木の島も重なりゼロ / unresolved 0 を保つ (§8-3 を新骨格へ拡張)。"""
    if shape == "causal":
        nodes = [{"id": f"a{i:03d}", "label": f"原因と結果の概念{i}",
                  "community_id": f"c{i % 3}"} for i in range(k)]
        edges = [{"id": f"e{i:03d}_{d}", "from": f"a{i:03d}", "to": f"a{i + d:03d}",
                  "label": "促進すると考えられる", "glyph": "arrow",
                  "confidence": 0.4 + (i % 5) / 10}
                 for i in range(k) for d in (1, 3)
                 if i + d < k and i % 3 == (i + d) % 3]
        # 閉路を仕込んでも切断して層状に組めること
        edges.append({"id": "back", "from": f"a{k - 3:03d}", "to": "a000",
                      "label": "戻る", "glyph": "arrow", "confidence": 0.2})
        comms = 3
    else:
        nodes = [{"id": f"t{i:03d}", "label": f"分類の概念{i}",
                  "community_id": f"c{i % 2}"} for i in range(k)]
        edges = [{"id": f"h{i:03d}", "from": f"t{i:03d}", "to": f"t{(i - 1) // 2:03d}",
                  "label": "の一種である", "glyph": "isa"}
                 for i in range(1, k) if i % 2 == ((i - 1) // 2) % 2]
        comms = 2

    plan = compute_layout({
        "graph_version": f"kg_{shape}", "nodes": nodes, "edges": edges,
        "communities": [{"id": f"c{i}", "name": f"{shape}{i}"} for i in range(comms)]})
    clear_label_plan_cache()
    report = check_overlaps(plan)
    assert report.clean, (report.node_on_node[:3], report.label_on_node[:3],
                          report.label_on_label[:3], report.unresolved_labels[:3])
    assert all(i["layout_mode"] == "semantic" for i in plan["islands"])

    placed = {n["id"]: n for n in plan["nodes"]}
    for e in plan["edges"]:
        if e["glyph"] == "arrow" and e["id"] != "back":
            assert placed[e["from"]]["x"] < placed[e["to"]]["x"], e["id"]
        if e["glyph"] == "isa":
            assert placed[e["to"]]["y"] < placed[e["from"]]["y"], e["id"]
