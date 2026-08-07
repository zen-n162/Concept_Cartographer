"""概念図の編集 (cc_core.editing) の回帰テスト — 編集/学習設計書 §10。

原則の検証が主眼:
  - 原本不変 + 追記のみ / fold は例外で止まらない (§1)
  - 編集で島がシャッフルされない (コミュニティ凍結 §4.3)
  - 編集したノードが Top-K 選抜から落ちない (ピン留め §4.1)
  - 同じ base_kg + 同じ編集ログ → 同じ plan (§4.4)

各テストは tmp_path を作業ディレクトリにするので production/ を汚さない。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from cc_core import editing
from cc_core.community import LEVEL_BANDS, ImportanceBreakdown, select_nodes
from cc_core.detail import build_multilevel_plan, check_level_bands
from cc_core.editing import (
    EditConflict,
    EditError,
    EditTargetNotFound,
    append_edit,
    apply_edits,
    load_edits,
    rebuild_session,
    validate_edit,
)
from cc_core.evaluation import EvaluationStore, correction_rate

PRODUCTION = Path(__file__).resolve().parents[1]
KG_FIXTURE = PRODUCTION / "graphs" / "kg_session_20260807_010128.json"
SESSION = "20260807_120000"


def sample_kg(islands: int = 6, per_island: int = 6) -> dict:
    """島 n 個 × ノード m 個を鎖で繋ぎ、島同士を 1 本の橋で結んだ KG。"""
    nodes: list[dict] = []
    edges: list[dict] = []
    for i in range(islands):
        cid = f"comm_{i:03d}"
        for j in range(per_island):
            nodes.append({"id": f"c{i}{j:02d}", "label": f"概念{i}-{j}",
                          "community_id": cid})
        for j in range(per_island - 1):
            edges.append({
                "id": f"r{i}{j:02d}", "from": f"c{i}{j:02d}", "to": f"c{i}{j + 1:02d}",
                "label": "関連", "glyph": "wave",
                "evidence_span": [{"document_id": "d1", "surface": "関連が見られた"}],
            })
        if i:  # 前の島と橋を 1 本
            edges.append({
                "id": f"rb{i:02d}", "from": f"c{i - 1}00", "to": f"c{i}00",
                "label": "橋渡し", "glyph": "wave",
                "evidence_span": [{"document_id": "d1", "surface": "両者は関連する"}],
            })
    return {
        "graph_version": "kg_test",
        "nodes": nodes,
        "edges": edges,
        "communities": [{"id": f"comm_{i:03d}", "name": f"テーマ{i}", "is_gap": False}
                        for i in range(islands)],
    }


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    (tmp_path / "graphs").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def setup_session(kg: dict | None = None, session: str = SESSION) -> dict:
    """原本 KG と初回 plan を置く (通常の生成直後と同じ状態)。"""
    kg = kg or sample_kg()
    editing.kg_file(session).write_text(json.dumps(kg, ensure_ascii=False),
                                        encoding="utf-8")
    plan = build_multilevel_plan(kg, default_level="standard")
    editing.plan_file(session).write_text(json.dumps(plan, ensure_ascii=False),
                                          encoding="utf-8")
    return kg


def detailed_nodes(plan: dict) -> dict[str, dict]:
    return {n["id"]: n for n in plan["_level_plans"]["detailed"]["nodes"]}


# ------------------------------------------------------------ validate (§2)

def test_rename_to_empty_label_is_rejected(workdir) -> None:
    kg = setup_session()
    with pytest.raises(EditError, match="ラベルが空"):
        validate_edit({"op": "rename_node", "target": "c000",
                       "payload": {"label": "  "}}, kg, None)


def test_rename_to_existing_label_is_rejected(workdir) -> None:
    """ラベル衝突は拒否する (マージは R2 の merge_nodes)。"""
    kg = setup_session()
    with pytest.raises(EditError, match="同じ名前の概念"):
        validate_edit({"op": "rename_node", "target": "c000",
                       "payload": {"label": "概念0-1"}}, kg, None)


def test_rename_collision_uses_normalized_labels(workdir) -> None:
    """全角/半角・大小文字の違いは同じラベルとみなす (NFKC + casefold)。"""
    kg = setup_session()
    kg["nodes"][1]["label"] = "ML Model"
    with pytest.raises(EditError):
        validate_edit({"op": "rename_node", "target": "c000",
                       "payload": {"label": "ｍｌ　model"}}, kg, None)


def test_unknown_target_raises_target_not_found(workdir) -> None:
    kg = setup_session()
    with pytest.raises(EditTargetNotFound):
        validate_edit({"op": "rename_node", "target": "nope",
                       "payload": {"label": "x"}}, kg, None)
    with pytest.raises(EditTargetNotFound):
        validate_edit({"op": "delete_edge", "target": "nope", "payload": {}}, kg, None)


def test_add_edge_rejects_self_loop_and_duplicates(workdir) -> None:
    kg = setup_session()
    with pytest.raises(EditError, match="自己ループ"):
        validate_edit({"op": "add_edge", "target": None,
                       "payload": {"from": "c000", "to": "c000"}}, kg, None)
    with pytest.raises(EditError, match="既にあります"):
        validate_edit({"op": "add_edge", "target": None,
                       "payload": {"from": "c000", "to": "c001",
                                   "glyph": "wave"}}, kg, None)


def test_retype_rejects_unknown_glyph(workdir) -> None:
    kg = setup_session()
    with pytest.raises(EditError, match="関係の種類"):
        validate_edit({"op": "retype_edge", "target": "r000",
                       "payload": {"glyph": "squiggle"}}, kg, None)


def test_add_node_requires_known_island(workdir) -> None:
    kg = setup_session()
    with pytest.raises(EditTargetNotFound, match="島が見つかりません"):
        validate_edit({"op": "add_node", "target": None,
                       "payload": {"label": "新概念", "community_id": "comm_zzz"}},
                      kg, None)
    # new_island: true なら島の指定は不要
    validate_edit({"op": "add_node", "target": None,
                   "payload": {"label": "新概念", "new_island": True}}, kg, None)


# ------------------------------------------------------------ fold (§4)

def test_edit_then_revert_restores_original(workdir) -> None:
    setup_session()
    edit = append_edit(SESSION, {"op": "rename_node", "target": "c000",
                                 "payload": {"label": "改名後"}})
    kg, _ = apply_edits(editing.load_kg(SESSION), load_edits(SESSION))
    assert {n["label"] for n in kg["nodes"]} >= {"改名後"}

    editing.append_revert(SESSION, edit["edit_id"])
    kg2, _ = apply_edits(editing.load_kg(SESSION), load_edits(SESSION))
    labels = {n["label"] for n in kg2["nodes"]}
    assert "改名後" not in labels and "概念0-0" in labels
    # 原本は書き換わっていない (追記のみ)
    assert "改名後" not in editing.kg_file(SESSION).read_text(encoding="utf-8")


def test_double_revert_is_conflict(workdir) -> None:
    setup_session()
    edit = append_edit(SESSION, {"op": "rename_node", "target": "c000",
                                 "payload": {"label": "改名後"}})
    editing.append_revert(SESSION, edit["edit_id"])
    with pytest.raises(EditConflict):
        editing.append_revert(SESSION, edit["edit_id"])
    with pytest.raises(EditTargetNotFound):
        editing.append_revert(SESSION, "e-19990101-999")


def test_revert_makes_dependent_edit_a_warned_noop(workdir) -> None:
    """取り消された add_edge に依存する relabel は例外でなく警告つき no-op。"""
    setup_session()
    added = append_edit(SESSION, {"op": "add_edge", "target": None,
                                  "payload": {"from": "c001", "to": "c101",
                                              "label": "手動", "glyph": "wave"}})
    new_edge_id = f"ue-{added['edit_id'][2:]}"
    append_edit(SESSION, {"op": "relabel_edge", "target": new_edge_id,
                          "payload": {"label": "書き換え"}})
    editing.append_revert(SESSION, added["edit_id"])

    kg, warnings = apply_edits(editing.load_kg(SESSION), load_edits(SESSION))
    assert not any(e["id"] == new_edge_id for e in kg["edges"])
    assert len(warnings) == 1 and "関係が見つかりません" in warnings[0]


def test_delete_node_cascades_edges_and_keeps_them_in_before(workdir) -> None:
    kg0 = setup_session()
    touching = [e["id"] for e in kg0["edges"]
                if "c001" in (e["from"], e["to"])]
    assert touching
    edit = append_edit(SESSION, {"op": "delete_node", "target": "c001"})
    kg, _ = apply_edits(editing.load_kg(SESSION), load_edits(SESSION))
    assert not any(n["id"] == "c001" for n in kg["nodes"])
    assert not any(e["id"] in touching for e in kg["edges"])
    # undo できるよう before に退避されている
    assert {e["id"] for e in edit["before"]["edges"]} == set(touching)


def test_add_node_into_island_and_new_island(workdir) -> None:
    setup_session()
    e1 = append_edit(SESSION, {"op": "add_node", "target": None,
                               "payload": {"label": "既存島の概念",
                                           "community_id": "comm_000"}})
    e2 = append_edit(SESSION, {"op": "add_node", "target": None,
                               "payload": {"label": "新島の概念", "new_island": True}})
    kg, _ = apply_edits(editing.load_kg(SESSION), load_edits(SESSION))
    added = {n["label"]: n for n in kg["nodes"] if n.get("origin") == "user_added"}
    assert added["既存島の概念"]["community_id"] == "comm_000"
    assert added["新島の概念"]["community_id"] == "comm_user_1"
    assert any(c["id"] == "comm_user_1" and c["name"] == "新島の概念"
               for c in kg["communities"])
    # id は edit_id から決定的に作られる
    assert added["既存島の概念"]["id"] == f"un-{e1['edit_id'][2:]}"
    assert added["新島の概念"]["id"] == f"un-{e2['edit_id'][2:]}"


def test_retype_to_arrow_records_user_authority(workdir) -> None:
    """因果への昇格はユーザー権限。3 点セットの記録を人の判断で置き換える。"""
    setup_session()
    append_edit(SESSION, {"op": "retype_edge", "target": "r000",
                          "payload": {"glyph": "arrow"}})
    kg, _ = apply_edits(editing.load_kg(SESSION), load_edits(SESSION))
    edge = next(e for e in kg["edges"] if e["id"] == "r000")
    assert edge["glyph"] == "arrow"
    assert edge["origin"] == "user_edited"
    assert "ユーザー" in edge["causal_check"]["reason"]


# -------------------------------------------------- ピン留め (§4.1)

def test_pinned_node_survives_every_level(workdir) -> None:
    """Overview で落ちるはずの周辺ノードでも、編集したらピン留めで残る。"""
    setup_session(sample_kg(islands=6, per_island=6))
    plan0 = json.loads(editing.plan_file(SESSION).read_text(encoding="utf-8"))
    overview0 = {n["id"] for n in plan0["_level_plans"]["overview"]["nodes"]}
    dropped = next(n["id"] for n in plan0["_level_plans"]["detailed"]["nodes"]
                   if n["id"] not in overview0)

    append_edit(SESSION, {"op": "rename_node", "target": dropped,
                          "payload": {"label": "ここを直した"}})
    plan = rebuild_session(SESSION)
    for level in ("overview", "standard", "detailed"):
        ids = {n["id"] for n in plan["_level_plans"][level]["nodes"]}
        assert dropped in ids, f"{level} からピン留めノードが消えた"


def test_select_nodes_keeps_all_pins_even_over_the_band(workdir) -> None:
    """pinned だけで帯上限を超えても全部残す (エラーにしない)。"""
    importance = {f"c{i:03d}": ImportanceBreakdown(0.1, 0.1, 0.1, 0.1 + i / 1000)
                  for i in range(40)}
    communities = {n: "comm_000" for n in importance}
    pins = set(list(importance)[:25])
    selected = select_nodes(importance, communities, "overview",
                            total_nodes=len(importance), pinned=pins)
    assert pins <= set(selected)
    assert len(selected) > LEVEL_BANDS["overview"][1]


def test_check_level_bands_marks_pin_overflow_separately(workdir) -> None:
    plan = {"levels": {
        "overview": {"nodes": 25, "edges": 4, "aggregates": 0, "pinned": 25},
        "standard": {"nodes": 25, "edges": 4, "aggregates": 0, "pinned": 25},
        "detailed": {"nodes": 25, "edges": 4, "aggregates": 0, "pinned": 25},
    }}
    problems = check_level_bands(plan)
    assert problems and all(p.startswith("user_pinned_overflow") for p in problems)
    # ピン留めが無ければ従来どおりの超過報告
    plan["levels"]["overview"].pop("pinned")
    assert any(p.startswith("overview: 25") for p in check_level_bands(plan))


# -------------------------------------------------- 凍結 (§4.3)

def test_unrelated_islands_do_not_reshuffle_on_edit(workdir) -> None:
    """1 本消しただけで全コミュニティが組み替わらないこと。"""
    setup_session(sample_kg(islands=6, per_island=6))
    before = {n["id"]: n["community_id"] for n in
              detailed_nodes(json.loads(
                  editing.plan_file(SESSION).read_text(encoding="utf-8"))).values()}

    append_edit(SESSION, {"op": "delete_edge", "target": "rb03"})  # 島 2-3 の橋
    plan = rebuild_session(SESSION)
    after = {n["id"]: n["community_id"] for n in detailed_nodes(plan).values()}
    changed = [k for k in before if k in after and before[k] != after[k]]
    assert changed == [], f"無関係なノードの所属が変わった: {changed}"


def test_new_node_joins_neighbour_community_by_majority(workdir) -> None:
    """凍結マップに無いノードは隣接の多数決で所属を決める。"""
    setup_session()
    added = append_edit(SESSION, {"op": "add_node", "target": None,
                                  "payload": {"label": "橋渡し概念",
                                              "community_id": "comm_002"}})
    nid = f"un-{added['edit_id'][2:]}"
    append_edit(SESSION, {"op": "add_edge", "target": None,
                          "payload": {"from": "c200", "to": nid, "glyph": "wave"}})
    plan = rebuild_session(SESSION)
    assert detailed_nodes(plan)[nid]["community_id"] == "comm_002"


# -------------------------------------------------- 決定性 (§4.4)

def test_rebuild_is_deterministic(workdir) -> None:
    setup_session()
    append_edit(SESSION, {"op": "rename_node", "target": "c000",
                          "payload": {"label": "決定性テスト"}})
    append_edit(SESSION, {"op": "add_node", "target": None,
                          "payload": {"label": "追加概念", "new_island": True}})
    a = rebuild_session(SESSION, save=False)
    b = rebuild_session(SESSION, save=False)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_rename_recomputes_layout_size(workdir) -> None:
    """改名したらラベル幅に合わせてノードの大きさが変わる (再レイアウト)。"""
    setup_session()
    plan0 = json.loads(editing.plan_file(SESSION).read_text(encoding="utf-8"))
    before = detailed_nodes(plan0)["c000"]["size"]
    append_edit(SESSION, {"op": "rename_node", "target": "c000",
                          "payload": {"label": "非常に長い概念名をつけた場合の"
                                               "レイアウト再計算の確認用ラベル"}})
    plan = rebuild_session(SESSION)
    assert detailed_nodes(plan)["c000"]["size"] > before


def test_rebuild_keeps_gap_decisions(workdir) -> None:
    """編集で rebuild してもギャップの確定 (有用/却下) は失われない。"""
    setup_session()
    plan = json.loads(editing.plan_file(SESSION).read_text(encoding="utf-8"))
    from cc_core.gaps import detect_gaps
    plan["gaps"] = [g.to_dict() for g in detect_gaps(editing.load_kg(SESSION))]
    assert plan["gaps"], "テスト前提: ギャップ候補があること"
    gap_id = plan["gaps"][0]["gap_id"]
    plan["gaps"][0].update({"status": "confirmed", "confirmed_by": "tester",
                            "confirmed_at": "2026-08-07T00:00:00"})
    editing.plan_file(SESSION).write_text(json.dumps(plan, ensure_ascii=False),
                                          encoding="utf-8")
    append_edit(SESSION, {"op": "rename_node", "target": "c000",
                          "payload": {"label": "改名"}})
    rebuilt = rebuild_session(SESSION)
    kept = next((g for g in rebuilt["gaps"] if g["gap_id"] == gap_id), None)
    assert kept and kept["status"] == "confirmed" and kept["confirmed_by"] == "tester"


def test_provenance_records_edit_count(workdir) -> None:
    setup_session()
    e1 = append_edit(SESSION, {"op": "rename_node", "target": "c000",
                               "payload": {"label": "A"}})
    append_edit(SESSION, {"op": "rename_node", "target": "c001",
                          "payload": {"label": "B"}})
    editing.append_revert(SESSION, e1["edit_id"])
    plan = rebuild_session(SESSION)
    assert plan["provenance"]["edit_count"] == 1     # 有効な編集のみ
    assert plan["provenance"]["edits_logged"] == 3   # ログ行数


# -------------------------------------------------- 評価への自動追記 (§6)

def test_edit_appends_to_evaluation_log(workdir) -> None:
    setup_session()
    append_edit(SESSION, {"op": "delete_edge", "target": "r000"}, user="tester")
    append_edit(SESSION, {"op": "rename_node", "target": "c000",
                          "payload": {"label": "改名"}}, user="tester")
    rows = EvaluationStore("logs/evaluation.jsonl").load()
    assert len(rows) == 2
    # delete_edge は「この関係は誤り」という判定でもある
    assert rows[0]["relation_verdicts"] == {"r000": "incorrect"}
    assert rows[0]["operations"][0]["op"] == "delete_element"
    assert rows[0]["operations"][0]["edit_op"] == "delete_edge"
    assert rows[1]["operations"][0]["op"] == "edit_node"
    assert rows[0]["user_id"] == "tester"
    # v3 §7.2.1 の修正率が実データを持つ
    assert correction_rate(rows)["corrections"] == 2


def test_revert_is_not_counted_as_a_correction(workdir) -> None:
    setup_session()
    edit = append_edit(SESSION, {"op": "rename_node", "target": "c000",
                                 "payload": {"label": "改名"}})
    editing.append_revert(SESSION, edit["edit_id"])
    rows = EvaluationStore("logs/evaluation.jsonl").load()
    assert rows[1]["operations"][0]["op"] == "edit_revert"
    assert correction_rate(rows)["corrections"] == 1


# -------------------------------------------------- 実データでの往復

def test_real_kg_round_trip(workdir) -> None:
    """本物の KG で 4 操作 → rebuild → revert が通ること。"""
    shutil.copy(KG_FIXTURE, editing.kg_file(SESSION))
    kg = editing.load_kg(SESSION)
    plan = build_multilevel_plan(kg, default_level="standard")
    editing.plan_file(SESSION).write_text(json.dumps(plan, ensure_ascii=False),
                                          encoding="utf-8")
    node_id = kg["nodes"][0]["id"]
    edge_id = kg["edges"][0]["id"]

    append_edit(SESSION, {"op": "rename_node", "target": node_id,
                          "payload": {"label": "手直しした概念"}})
    append_edit(SESSION, {"op": "retype_edge", "target": edge_id,
                          "payload": {"glyph": "wave"}})
    added = append_edit(SESSION, {"op": "add_node", "target": None,
                                  "payload": {"label": "追加した概念",
                                              "new_island": True}})
    append_edit(SESSION, {"op": "add_edge", "target": None,
                          "payload": {"from": node_id,
                                      "to": f"un-{added['edit_id'][2:]}",
                                      "label": "手動追加", "glyph": "arrow"}})
    plan = rebuild_session(SESSION)
    labels = {n["label"] for n in plan["_level_plans"]["detailed"]["nodes"]}
    assert {"手直しした概念", "追加した概念"} <= labels
    assert plan["provenance"]["edit_count"] == 4

    rows = editing.annotate_edits(load_edits(SESSION))
    assert [r["reverted"] for r in rows] == [False] * 4
    editing.append_revert(SESSION, rows[0]["edit_id"])
    plan = rebuild_session(SESSION)
    labels = {n["label"] for n in plan["_level_plans"]["detailed"]["nodes"]}
    assert "手直しした概念" not in labels


def test_edits_on_session_without_base_kg_are_refused(workdir) -> None:
    """原本が無いセッションは編集できない (fold の基準が無いため)。"""
    with pytest.raises(EditTargetNotFound, match="原本"):
        append_edit("99999999_999999", {"op": "rename_node", "target": "c000",
                                        "payload": {"label": "x"}})


# ------------------------------------------------------ 関係ポリシーの整合 (§4)


def _legacy_session(workdir: Path, session: str = "20260101_000000") -> Path:
    """R1.5 以前の形の保存物を作る。

    原本 = 関係ポリシー適用**前** (LLM が言ったままの arrow)、
    plan = 適用**後** (3 点セットに落ちて wave へ降格済み)。
    """
    graphs = workdir / "graphs"
    kg = sample_kg(islands=2, per_island=3)
    kg["edges"][0]["glyph"] = "arrow"          # 生の因果主張
    kg["edges"][0]["label"] = "原因となる"
    (graphs / f"kg_session_{session}.json").write_text(
        json.dumps(kg, ensure_ascii=False), encoding="utf-8")

    plan = build_multilevel_plan(kg, default_level="detailed")
    for edges in [plan["edges"]] + [p["edges"] for p in plan["_level_plans"].values()]:
        for edge in edges:
            if edge["id"] == kg["edges"][0]["id"]:
                edge["glyph"] = "wave"          # 降格後
                edge["label"] = "関連する (因果の根拠不足)"
                edge["causal_check"] = {"passed": False, "reason": "lexicon_miss"}
    (graphs / f"layout_plan_session_{session}.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return graphs


def _glyph_of(plan: dict, edge_id: str) -> str:
    for edge in plan["_level_plans"]["detailed"]["edges"]:
        if edge["id"] == edge_id:
            return edge["glyph"]
    raise AssertionError(f"edge {edge_id} not in plan")


def test_rebuild_keeps_demoted_relation_demoted(workdir):
    """旧セッションを編集しても、降格済みの相関が因果矢印へ戻らない。

    原本がポリシー適用前なので、素朴に fold すると arrow が復活する
    (裁定 7 の 3 点セットが 1 か所の編集で黙って無効化される)。
    """
    session = "20260101_000000"
    _legacy_session(workdir, session)
    target = "r000"  # arrow に細工したエッジ

    append_edit(session, {"op": "rename_node", "target": "c001",
                          "payload": {"label": "改名した概念"}})
    plan = rebuild_session(session)

    assert _glyph_of(plan, target) == "wave"
    assert plan["provenance"]["policy_reconciled"]  # 黙って直さない
    assert any(target in note for note in plan["provenance"]["policy_reconciled"])
    # 判定の根拠 (causal_check) も現在の KG へ戻っている
    assert any(e["id"] == target and e.get("causal_check")
               for e in editing.current_kg(session)["edges"])


def test_user_retype_wins_over_plan(workdir):
    """ユーザーが因果へ戻したものは整合で上書きされない (人が最終権威)。"""
    session = "20260101_000000"
    _legacy_session(workdir, session)
    target = "r000"

    append_edit(session, {"op": "retype_edge", "target": target,
                          "payload": {"glyph": "arrow"}})
    plan = rebuild_session(session)

    assert _glyph_of(plan, target) == "arrow"
    assert target not in " ".join(plan["provenance"].get("policy_reconciled", []))


def test_reconcile_is_noop_for_current_sessions(workdir):
    """R1.5 以降 (原本がポリシー適用後) は整合が何もしない。"""
    session = "20260202_000000"
    graphs = workdir / "graphs"
    kg = sample_kg(islands=2, per_island=3)
    (graphs / f"kg_session_{session}.json").write_text(
        json.dumps(kg, ensure_ascii=False), encoding="utf-8")
    (graphs / f"layout_plan_session_{session}.json").write_text(
        json.dumps(build_multilevel_plan(kg, default_level="detailed"),
                   ensure_ascii=False), encoding="utf-8")

    append_edit(session, {"op": "rename_node", "target": "c001",
                          "payload": {"label": "改名"}})
    plan = rebuild_session(session)
    assert "policy_reconciled" not in plan["provenance"]


def test_reconcile_survives_plan_without_level_plans(workdir):
    """`_level_plans` を持たない旧形式の plan でも整合できる。"""
    session = "20260303_000000"
    graphs = _legacy_session(workdir, session)
    path = graphs / f"layout_plan_session_{session}.json"
    plan = json.loads(path.read_text(encoding="utf-8"))
    plan.pop("_level_plans")
    path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    append_edit(session, {"op": "rename_node", "target": "c001",
                          "payload": {"label": "改名"}})
    assert _glyph_of(rebuild_session(session), "r000") == "wave"
