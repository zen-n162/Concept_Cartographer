"""フィードバック学習 (cc_core.learning) の回帰テスト — 編集/学習設計書 §10。

「学習」の実体は 3 機構だけ (§1):
  (a) 決定的な自動適用 (用語辞書・除外リスト・因果上書き)
  (b) 抽出プロンプトへの少数事例注入
  (c) 因果検証語彙の統計調整
モデルの重みは変わらない。テストもこの 3 つに対応させる。

とくに検証したい安全側の性質:
  - 曖昧な改名 (同じ語に複数の訳語) は**機械適用しない**
  - 1 回だけの削除は除外リストに**しない** (ヒント止まり)
  - add_edge / add_node は**機械適用しない** (資料に無い関係を足さない)
  - 取り消した編集は学習からも消える
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cc_core import editing, learning
from cc_core.causal import apply_relation_policy
from cc_core.detail import build_multilevel_plan
from cc_core.editing import append_edit
from cc_core.learning import (
    apply_learned,
    build_prompt_hints,
    derive_learned,
    load_learned,
    note_cues_kept,
    relearn,
    summarize,
    update_from_edit,
)

SESSION = "20260807_120000"
OTHER = "20260808_090000"


def kg_with(nodes: list[tuple[str, str]], edges: list[dict]) -> dict:
    return {
        "graph_version": "kg_test",
        "nodes": [{"id": nid, "label": label, "community_id": "comm_000"}
                  for nid, label in nodes],
        "edges": edges,
        "communities": [{"id": "comm_000", "name": "テーマ", "is_gap": False}],
    }


def edge(eid: str, src: str, dst: str, glyph: str = "arrow",
         surface: str = "機序により生じる") -> dict:
    return {"id": eid, "from": src, "to": dst, "label": "影響", "glyph": glyph,
            "evidence_span": [{"document_id": "d1", "surface": surface}]}


BASE_KG = kg_with(
    [("c001", "ML モデル"), ("c002", "研究"), ("c003", "被ばく線量"),
     ("c004", "細胞損傷"), ("c005", "照射条件")],
    [edge("r001", "c003", "c004"), edge("r002", "c001", "c005", glyph="wave"),
     edge("r003", "c005", "c002")],
)


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    (tmp_path / "graphs").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def setup_session(session: str = SESSION, kg: dict | None = None) -> dict:
    kg = json.loads(json.dumps(kg or BASE_KG, ensure_ascii=False))
    editing.kg_file(session).write_text(json.dumps(kg, ensure_ascii=False),
                                        encoding="utf-8")
    plan = build_multilevel_plan(kg, default_level="standard")
    editing.plan_file(session).write_text(json.dumps(plan, ensure_ascii=False),
                                          encoding="utf-8")
    return kg


# ------------------------------------------------------------ 導出 (§5.2)

def test_unique_rename_becomes_auto_lexicon(workdir) -> None:
    setup_session()
    append_edit(SESSION, {"op": "rename_node", "target": "c001",
                          "payload": {"label": "機械学習モデル"}})
    store = relearn()
    entry = store["lexicon"][0]
    assert (entry["from"], entry["to"]) == ("ML モデル", "機械学習モデル")
    assert entry["auto"] is True


def test_ambiguous_rename_is_demoted_to_a_hint(workdir) -> None:
    """同じ語が文脈により別の語へ直されるなら機械適用しない (§5.2)。"""
    setup_session()
    setup_session(OTHER)
    append_edit(SESSION, {"op": "rename_node", "target": "c001",
                          "payload": {"label": "機械学習モデル"}})
    append_edit(OTHER, {"op": "rename_node", "target": "c001",
                        "payload": {"label": "統計モデル"}})
    store = relearn()
    assert len(store["lexicon"]) == 2
    assert all(e["auto"] is False for e in store["lexicon"])
    assert any(f["kind"] == "ambiguous_rename" for f in store["few_shot"])


def test_stoplist_needs_two_deletions(workdir) -> None:
    setup_session()
    append_edit(SESSION, {"op": "delete_node", "target": "c002"})
    store = relearn()
    assert store["stoplist"][0]["auto"] is False       # 1 回では機械適用しない
    assert any(f["kind"] == "noisy_node" for f in store["few_shot"])

    setup_session(OTHER)
    append_edit(OTHER, {"op": "delete_node", "target": "c002"})
    store = relearn()
    entry = next(e for e in store["stoplist"] if e["label"] == "研究")
    assert entry["n"] == 2 and entry["auto"] is True


def test_retype_downgrade_becomes_deny_and_counts_cue(workdir) -> None:
    setup_session()
    append_edit(SESSION, {"op": "retype_edge", "target": "r001",
                          "payload": {"glyph": "wave"}})
    store = relearn()
    override = store["causal_overrides"][0]
    assert override["decision"] == "deny"
    assert (override["from_label"], override["to_label"]) == ("被ばく線量", "細胞損傷")
    assert override["source"] == "user_retype"
    # 語彙統計にユーザー降格が入る (§5.1 cue_stats)
    assert store["cue_stats"]["機序"]["downgraded_by_user"] == 1


def test_retype_promotion_becomes_allow(workdir) -> None:
    setup_session()
    append_edit(SESSION, {"op": "retype_edge", "target": "r002",
                          "payload": {"glyph": "arrow"}})
    store = relearn()
    assert store["causal_overrides"][0]["decision"] == "allow"


def test_reverse_edge_becomes_reverse_override(workdir) -> None:
    setup_session()
    append_edit(SESSION, {"op": "reverse_edge", "target": "r001"})
    store = relearn()
    o = store["causal_overrides"][0]
    assert o["decision"] == "reverse"
    assert (o["from_label"], o["to_label"]) == ("被ばく線量", "細胞損傷")


def test_delete_causal_edge_denies_and_records_example(workdir) -> None:
    setup_session()
    append_edit(SESSION, {"op": "delete_edge", "target": "r001"})
    store = relearn()
    assert store["causal_overrides"][0]["decision"] == "deny"
    assert any(f["kind"] == "wrong_edge" for f in store["few_shot"])


def test_additions_stay_as_hints_only(workdir) -> None:
    """add_edge / add_node は事例ヒント止まり (機械追加はしない = 捏造しない)。"""
    setup_session()
    added = append_edit(SESSION, {"op": "add_node", "target": None,
                                  "payload": {"label": "線量評価", "new_island": True}})
    append_edit(SESSION, {"op": "add_edge", "target": None,
                          "payload": {"from": "c005",
                                      "to": f"un-{added['edit_id'][2:]}",
                                      "label": "必要", "glyph": "wave"}})
    store = relearn()
    kinds = {f["kind"] for f in store["few_shot"]}
    assert {"missed_node", "missed_edge"} <= kinds
    assert store["lexicon"] == [] and store["stoplist"] == []
    assert store["causal_overrides"] == []


def test_revert_removes_the_learned_entry(workdir) -> None:
    setup_session()
    e = append_edit(SESSION, {"op": "rename_node", "target": "c001",
                              "payload": {"label": "機械学習モデル"}})
    assert relearn()["lexicon"]
    editing.append_revert(SESSION, e["edit_id"])
    assert relearn()["lexicon"] == []


def test_relearn_is_idempotent(workdir) -> None:
    setup_session()
    append_edit(SESSION, {"op": "rename_node", "target": "c001",
                          "payload": {"label": "機械学習モデル"}})
    append_edit(SESSION, {"op": "retype_edge", "target": "r001",
                          "payload": {"glyph": "wave"}})
    first = relearn()
    second = relearn()
    for key in ("lexicon", "stoplist", "causal_overrides", "few_shot", "cue_stats"):
        assert first[key] == second[key]


def test_relearn_preserves_generation_side_cue_counts(workdir) -> None:
    """kept は生成側の統計。編集ログから復元できないので引き継ぐ。"""
    setup_session()
    note_cues_kept(["mechanism:機序", "mechanism:機序", "intervention:投与"])
    append_edit(SESSION, {"op": "retype_edge", "target": "r001",
                          "payload": {"glyph": "wave"}})
    store = relearn()
    assert store["cue_stats"]["機序"]["kept"] == 2
    assert store["cue_stats"]["機序"]["downgraded_by_user"] == 1
    # 維持 2 : 降格 1 は健全なので既定では警告しない。閾値を下げれば出る
    assert learning.cue_warnings(store) == []
    assert learning.cue_warnings(store, ratio=0.3, min_n=1)


# ------------------------------------------------------------ 適用 (§5.3)

def test_apply_learned_renames_and_reports(workdir) -> None:
    learned = derive_learned({SESSION: [
        {"edit_id": "e-1", "op": "rename_node", "ts": "2026-08-07T10:00:00",
         "before": {"label": "ML モデル"}, "payload": {"label": "機械学習モデル"}},
    ]})
    kg, report = apply_learned(BASE_KG, learned)
    assert report["renames"] == 1
    assert any(n["label"] == "機械学習モデル" for n in kg["nodes"])
    assert report["details"][0]["kind"] == "rename"
    assert BASE_KG["nodes"][0]["label"] == "ML モデル"   # 元は変更しない


def test_apply_learned_matches_normalized_labels(workdir) -> None:
    """照合は NFKC + casefold (全角/半角・大小文字の揺れを吸収)。"""
    learned = derive_learned({SESSION: [
        {"edit_id": "e-1", "op": "rename_node", "ts": "2026-08-07T10:00:00",
         "before": {"label": "ｍｌ　モデル"}, "payload": {"label": "機械学習モデル"}},
    ]})
    kg, report = apply_learned(BASE_KG, learned)
    assert report["renames"] == 1


def test_apply_learned_stoplists_node_with_its_edges(workdir) -> None:
    learned = derive_learned({
        SESSION: [{"edit_id": "e-1", "op": "delete_node", "ts": "2026-08-07T10:00:00",
                   "before": {"node": {"label": "研究"}}, "payload": {}}],
        OTHER: [{"edit_id": "e-1", "op": "delete_node", "ts": "2026-08-08T10:00:00",
                 "before": {"node": {"label": "研究"}}, "payload": {}}],
    })
    kg, report = apply_learned(BASE_KG, learned)
    assert report["stoplisted"] == 1
    assert not any(n["label"] == "研究" for n in kg["nodes"])
    assert not any(e["id"] == "r003" for e in kg["edges"])   # 接続エッジも消える


def test_apply_learned_can_be_disabled(workdir) -> None:
    learned = derive_learned({SESSION: [
        {"edit_id": "e-1", "op": "rename_node", "ts": "2026-08-07T10:00:00",
         "before": {"label": "ML モデル"}, "payload": {"label": "機械学習モデル"}},
    ]})
    kg, report = apply_learned(BASE_KG, learned, enabled=False)
    assert report["enabled"] is False and report["renames"] == 0
    assert kg is BASE_KG


def test_override_reverses_direction_and_marks_allow(workdir) -> None:
    learned = derive_learned({SESSION: [
        {"edit_id": "e-1", "op": "reverse_edge", "ts": "2026-08-07T10:00:00",
         "before": {"glyph": "arrow", "from_label": "被ばく線量",
                    "to_label": "細胞損傷"}, "payload": {}},
    ]})
    kg, report = apply_learned(BASE_KG, learned)
    r001 = next(e for e in kg["edges"] if e["id"] == "r001")
    assert (r001["from"], r001["to"]) == ("c004", "c003")
    assert r001["causal_override"] == "allow"
    assert report["reversed"] == 1 and report["causal_allow"] == 1


def test_override_skips_the_llm_verifier(workdir) -> None:
    """causal_overrides が付いた対には独立検証器を呼ばない (§5.3 の 3)。"""
    calls: list[str] = []

    def verifier(edge_dict, text):
        calls.append(edge_dict["id"])
        return True

    learned = derive_learned({SESSION: [
        {"edit_id": "e-1", "op": "retype_edge", "ts": "2026-08-07T10:00:00",
         "before": {"glyph": "arrow", "from_label": "被ばく線量",
                    "to_label": "細胞損傷"}, "payload": {"glyph": "wave"}},
    ]})
    kg, _ = apply_learned(BASE_KG, learned)
    out, stats = apply_relation_policy(kg, verifier=verifier)
    # 上書きのある対には検証器を呼ばない。上書きの無い因果候補には従来どおり走る
    assert "r001" not in calls and "r003" in calls
    assert stats["override_deny"] == 1
    r001 = next(e for e in out["edges"] if e["id"] == "r001")
    assert r001["glyph"] == "wave"
    assert "過去の修正" in r001["causal_check"]["reason"]


def test_override_allow_keeps_causal_without_verifier(workdir) -> None:
    calls: list[str] = []
    learned = {"causal_overrides": [
        {"from_label": "被ばく線量", "to_label": "細胞損傷", "decision": "allow"}]}
    kg, _ = apply_learned(BASE_KG, learned)
    out, stats = apply_relation_policy(
        kg, verifier=lambda e, t: calls.append(e["id"]) or False)
    assert "r001" not in calls            # 確定済みの対は検証しない
    assert stats["override_allow"] == 1
    # r003 は検証器に否定されて降格、r001 は上書きで因果のまま
    assert next(e for e in out["edges"] if e["id"] == "r001")["glyph"] == "arrow"
    assert next(e for e in out["edges"] if e["id"] == "r003")["glyph"] == "wave"


# ------------------------------------------------------------ ヒント / 要約

def test_prompt_hints_respect_the_size_limit(workdir) -> None:
    edits = []
    for i in range(30):
        edits.append({"edit_id": f"e-{i}", "op": "rename_node",
                      "ts": f"2026-08-07T10:{i:02d}:00",
                      "before": {"label": f"用語{i}" * 6},
                      "payload": {"label": f"正式名称{i}" * 6}})
    learned = derive_learned({SESSION: edits})
    hints = build_prompt_hints(learned)
    assert len(hints) <= learning.HINTS_MAX_CHARS
    assert "過去の修正からの注意" in hints
    assert build_prompt_hints(learning.empty_store()) == ""
    assert build_prompt_hints(None) == ""


def test_prompt_hints_mention_each_mechanism(workdir) -> None:
    learned = derive_learned({SESSION: [
        {"edit_id": "e-1", "op": "rename_node", "ts": "2026-08-07T10:00:00",
         "before": {"label": "ML モデル"}, "payload": {"label": "機械学習モデル"}},
        {"edit_id": "e-2", "op": "retype_edge", "ts": "2026-08-07T10:01:00",
         "before": {"glyph": "arrow", "from_label": "A", "to_label": "B"},
         "payload": {"glyph": "wave"}},
    ]})
    hints = build_prompt_hints(learned)
    assert "「ML モデル」は「機械学習モデル」と表記してください" in hints
    assert "因果ではなく相関" in hints


def test_summary_counts_and_update_delta(workdir) -> None:
    setup_session()
    empty = summarize(load_learned())
    assert empty["lexicon"] == 0 and empty["causal_overrides"] == 0

    e = append_edit(SESSION, {"op": "rename_node", "target": "c001",
                              "payload": {"label": "機械学習モデル"}})
    delta = update_from_edit(e, SESSION)
    assert delta["changed"]["lexicon"] == 1
    assert delta["after"]["lexicon_auto"] == 1
    assert Path("logs/feedback/learned.json").exists()


def test_broken_store_falls_back_to_empty(workdir) -> None:
    path = Path("logs/feedback/learned.json")
    path.parent.mkdir(parents=True)
    path.write_text("{ broken", encoding="utf-8")
    store = load_learned()
    assert store["lexicon"] == [] and store["version"] == learning.STORE_VERSION
