"""レイアウト v3 バッチ L1 のテスト (docs/layout-v3-design.md §7)。

L1 の受け入れは 2 本立て:
  1. **既定 (grid) の生成物が 1 バイトも変わらない** — フラグを立てるまで
     本番挙動は完全不変、が憲法 (§8-1)
  2. flag=semantic のとき、決定的で・重なりゼロで・島の中に収まり・
     解けなければ黙らずに grid へ退避する
"""

from __future__ import annotations

import json
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
    """
    label = "非常に長い関係の説明テキストです"
    nodes = [{"id": f"d{i:03d}", "label": f"とても長い概念のラベル{i}",
              "community_id": "c0"} for i in range(k)]
    edges = [{"id": f"e{i:03d}_{j:03d}", "from": f"d{i:03d}", "to": f"d{j:03d}",
              "label": label, "glyph": "arrow"}
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
    # summary に必ず出る (黙らない)
    assert layout_v3.layout_summary(plan) == {
        "engine": "semantic", "islands": {"semantic": 0, "grid_fallback": 1}}


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
    ) == {"engine": "grid", "islands": {"semantic": 0, "grid_fallback": 0}}


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
