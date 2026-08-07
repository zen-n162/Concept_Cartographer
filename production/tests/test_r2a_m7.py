"""R2a M7 — 統合フリップ (設計書 §9 / §10 / §11 / §12)。

M7 は新しい層を足す段ではなく、**M1〜M6 で作ったものを既定にする**段なので、
テストの主眼も「足したもの」ではなく「壊していないこと」に置いてある:

  - layers=True が既定になっても、**切れば R1.5 に戻る** (逃げ道の確保)
  - **3 世代 (pre-R1.5 / R1.5 / R2a) の地図が読めて編集できて再構成できる**。
    R2a が読めるだけでは足りない。古い地図を持っている人が困らないこと
  - ギャップ 3 型を足しても `detect_gaps(kg)` の呼び出し規約は変えない
    (editing.rebuild_session は凍結されている)
  - LLM 呼び出しの上限が**既定値だけで** 30 call/run 以下に収まる (裁定 G)
  - 検証器が壊れたときに「否定した」と区別が付く (R1.5 の潜在不具合)

各テストは tmp_path を作業ディレクトリにするので production/ を汚さない。
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import pytest

from cc_core import editing, layers_store, verifiers
from cc_core.causal import apply_relation_policy
from cc_core.gaps import GAP_KINDS, detect_gaps
from cc_core.layers import apply_meta
from cc_core.normalize import VALID_GLYPHS
from cc_core.validate import validate_layout_plan
from cc_orchestrator import analysis, pipeline
from cc_orchestrator.agents_def import VERIFICATION_INSTRUCTIONS

from test_r2a_analysis import (            # M3〜M6 のモック資産を流用する
    FakeAnalysisAgent,
    FakeVerificationAgent,
    mock_run,                              # noqa: F401 — pytest fixture
)

PRODUCTION = Path(__file__).resolve().parents[1]
STATIC = PRODUCTION / "src" / "cc_web" / "static"
SCHEMA_PATH = PRODUCTION / "schemas" / "layout_plan.schema.json"
KG_FIXTURE = PRODUCTION / "tests" / "fixtures" / "kg_sample.json"


# ============================================ 裁定 G: LLM 呼び出しの総枠


def test_default_knobs_alone_cap_a_run_at_30_calls(monkeypatch) -> None:
    """既定値**だけ**で最悪 30 call/run 以下になる (裁定 G / 受け入れ基準 5)。

    環境変数で絞れることは M3〜M5 で確認済み。ここで固定したいのは
    「何も設定しない利用者が上限を超えない」こと — knob は保険であって、
    既定が安全でなければ意味がない。
    """
    for name in ("CC_ZONE_BATCH", "CC_ZONE_MAX_SENTENCES", "CC_CLAIMS_MAX_CALLS",
                 "CC_CGW_MAX_CALLS", "CC_VALIDATE_MAX_CALLS"):
        monkeypatch.delenv(name, raising=False)

    zone = math.ceil(analysis.zone_max_sentences() / analysis.zone_batch())
    worst = (zone + analysis.claims_max_calls() + verifiers.validate_max_calls()
             + analysis.cgw_max_calls() + 1)          # refutes は 1 回だけ
    assert (zone, analysis.claims_max_calls(), verifiers.validate_max_calls(),
            analysis.cgw_max_calls()) == (8, 2, 16, 2)
    assert worst == 29 and worst <= 30


def test_measured_llm_calls_are_reported_in_the_summary(mock_run) -> None:
    """実測値が summary に出る (裁定 G「機械検査できるようにする」)。"""
    summary, _, _, _ = mock_run(FakeAnalysisAgent(), FakeVerificationAgent())
    stats = summary["layers"]["stats"]
    assert stats["llm_calls"] > 0 and stats["llm_calls"] <= 30
    # サイドカー側の実測と summary の実測が食い違わない
    doc = json.loads(Path(summary["layers"]["saved"]).read_text(encoding="utf-8"))
    assert doc["stats"]["llm_calls"] == stats["llm_calls"]


# ============================================ 裁定 H: note_cues_kept の位置


def _cues_of_arrow_edges(kg: dict) -> list[str]:
    return sorted(hit for e in kg["edges"] if e.get("glyph") == "arrow"
                  for hit in (e.get("causal_check") or {}).get("lexicon_hit", []))


def _causal_kg_file(root: Path) -> str:
    """語彙証拠のある因果を 1 本持つ KG (offline でも矢印が残る)。"""
    kg = {"graph_version": "kg_cue",
          "nodes": [{"id": "c001", "label": "投与量", "community_id": "comm_001"},
                    {"id": "c002", "label": "反応速度", "community_id": "comm_001"},
                    {"id": "c003", "label": "観測ノイズ", "community_id": "comm_001"}],
          "edges": [{"id": "r001", "from": "c001", "to": "c002", "glyph": "arrow",
                     "label": "反応を引き起こす",
                     "evidence_span": [{"document_id": "d1",
                                        "surface": "投与により機序を介して反応が生じる"}]},
                    {"id": "r002", "from": "c002", "to": "c003", "glyph": "arrow",
                     "label": "関連する",
                     "evidence_span": [{"document_id": "d1",
                                        "surface": "両者に相関が見られた"}]}],
          "communities": [{"id": "comm_001", "name": "テーマ", "is_gap": False}]}
    path = root / "kg_cue.json"
    path.write_text(json.dumps(kg, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _spy_cue_order(monkeypatch) -> tuple[list[str], dict]:
    """apply_meta と note_cues_kept の**呼ばれた順**を記録する。

    裁定 H は「⑦meta の後で数える」という**順序**の裁定なので、順序そのものを
    見張る。結果 (集計値) だけを見ると、たまたま一致しただけの実装を通す。
    """
    order: list[str] = []
    captured: dict[str, list[str]] = {}
    real_meta, real_cues = pipeline.apply_meta, pipeline.note_cues_kept

    def spy_meta(kg, **kwargs):
        order.append("apply_meta")
        return real_meta(kg, **kwargs)

    def spy_cues(cues, **kwargs):
        order.append("note_cues_kept")
        captured["cues"] = list(cues)
        return real_cues(captured["cues"], **kwargs)

    monkeypatch.setattr(pipeline, "apply_meta", spy_meta)
    monkeypatch.setattr(pipeline, "note_cues_kept", spy_cues)
    return order, captured


def test_cue_stats_are_counted_after_apply_meta(tmp_path, monkeypatch) -> None:
    """語彙統計は ⑦meta の**後**、投影後の glyph で数える (裁定 H)。

    投影で相関へ降ろされた causes 候補の語彙が「因果として維持された」に
    混ざると、cue_warnings が誤った語を「因果へ昇格しがち」と警告し、
    抽出プロンプトへの注意書きが的外れになる。
    """
    monkeypatch.chdir(tmp_path)
    order, captured = _spy_cue_order(monkeypatch)
    summary = pipeline.run_pipeline(
        "今週の研究を概念地図として整理して", target="file",
        kg_file=_causal_kg_file(tmp_path), offline=True,
        verify_causal=False, export_svg=False)

    assert order == ["apply_meta", "note_cues_kept"]
    kg = json.loads(Path(summary["knowledge_graph"]["saved"]).read_text(encoding="utf-8"))
    assert sorted(captured["cues"]) == _cues_of_arrow_edges(kg)
    assert _cues_of_arrow_edges(kg)                 # 数える対象が実在する run
    # 相関へ降格したエッジ (相関表現のみ) の語彙は 1 つも入らない
    assert not any(hit in captured["cues"] for e in kg["edges"]
                   if e["glyph"] == "wave"
                   for hit in (e.get("causal_check") or {}).get("lexicon_hit", []))


def test_cue_stats_are_unchanged_when_layers_are_off(tmp_path, monkeypatch) -> None:
    """layers=False では投影が素通しなので集計は R1.5 と同じ (裁定 H)。

    「移動しても layers=False の集計は変わらない」を固定する — フリップの
    巻き戻し先 (R1.5 相当) の挙動が動かないことの担保。
    """
    monkeypatch.chdir(tmp_path)
    kg_file = _causal_kg_file(tmp_path)
    results = []
    for layers in (True, False):
        order, captured = _spy_cue_order(monkeypatch)
        pipeline.run_pipeline("今週の研究を概念地図として整理して", target="file",
                              kg_file=kg_file, offline=True, layers=layers,
                              verify_causal=False, export_svg=False)
        assert order == ["apply_meta", "note_cues_kept"]
        results.append(sorted(captured["cues"]))
    assert results[0] == results[1] and results[0]


# ============================================ 裁定 I: cc-verification の契約


def test_verification_agent_declares_the_task_branches() -> None:
    """instructions に task 分岐がある (cc-analysis と同じ形 / 裁定 I)。"""
    text = VERIFICATION_INSTRUCTIONS
    assert '"task"' in text
    for task in ('# task: "nli"', '# task: "claim_check"', '# task: "causal_check"'):
        assert task in text
    # task があるときはツールを呼ばない、と明示されている
    assert "ツールは一切呼ばず" in text
    # 描画検証の経路は残っている (R1 からの契約を壊さない)
    assert "verify_scene" in text and '"verdict"' in text


def test_verifier_prompts_carry_the_task_contract() -> None:
    """検証器のプロンプトが instructions と同じ task 名で問う (裁定 I)。"""
    nli = verifiers.LLMNLIVerifier(lambda p: "", model="terra").prompt("前提", "仮説")
    claim = verifiers.LLMClaimVerifier(lambda p: "", model="terra").prompt("前提", "仮説")

    nli_body = json.loads(nli[nli.index("{"):])
    claim_body = json.loads(claim[claim.index("{"):])
    assert nli_body == {"task": "nli", "premise": "前提", "hypothesis": "仮説"}
    assert claim_body == {"task": "claim_check", "claim": "仮説", "evidence": "前提"}
    # 旧バージョンのエージェントに当たったときの保険は残す
    assert "ツールは呼ばず" in nli and "ツールは呼ばず" in claim


def test_claim_verifier_reads_both_the_new_and_old_response_shapes() -> None:
    """裁定 I の {supported, score, rationale} も M6 の {confidence, why} も読む。"""
    new = verifiers.LLMClaimVerifier(
        lambda p: json.dumps({"supported": True, "score": 1.0, "rationale": "明示"}),
        model="terra").check("前提", "仮説")
    old = verifiers.LLMClaimVerifier(
        lambda p: json.dumps({"supported": True, "confidence": 1.0, "why": "明示"}),
        model="terra").check("前提", "仮説")
    assert new["score"] == old["score"] == 1.0
    assert new["detail"] == old["detail"] == "明示"


# ==================================== 独立検証器の契約違反 (R1.5 の潜在不具合)


def _causal_kg() -> dict:
    return {"nodes": [{"id": "c001", "label": "A"}, {"id": "c002", "label": "B"}],
            "edges": [{"id": "r001", "from": "c001", "to": "c002", "glyph": "arrow",
                       "label": "機序により生じる",
                       "evidence_span": [{"document_id": "d1",
                                          "surface": "機序を介してBが生じる"}]}]}


class _Client:
    """cc-verification の代わり。返す JSON をテストが決める。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    def run(self, agent: str, prompt: str, **kwargs: object) -> str:
        self.prompts.append(prompt)
        return json.dumps(self.payload, ensure_ascii=False)


def test_verifier_contract_violation_demotes_but_is_labelled() -> None:
    """"causal" キーが無い応答は**降格しつつ**本物の否定と区別できる。

    結論 (相関へ降格) は変えない — 答えられなかった因果を通すほうが危険。
    区別が要るのは KPI で「検証器が否定した」と「検証器が壊れていた」を
    混ぜないため。
    """
    client = _Client({"why": "機序の記述あり"})       # causal キーが無い = 契約違反
    kg, _ = apply_relation_policy(_causal_kg(),
                                  verifier=pipeline._causal_verifier(client))
    assert pipeline._mark_verifier_errors(kg) == 1

    edge = kg["edges"][0]
    assert edge["glyph"] == "wave"                    # 安全側 (fail-closed) は維持
    assert edge["causal_check"]["reason_code"] == "verifier_error"
    assert "契約どおりに応答しなかった" in edge["causal_check"]["reason"]
    assert pipeline.VERIFIER_ERROR_FLAG not in edge   # 一時印は保存形に残さない
    # プロンプトは裁定 I の task 契約で問うている
    assert json.loads(client.prompts[0][client.prompts[0].index("{"):])["task"] \
        == "causal_check"


def test_a_real_denial_is_not_labelled_as_a_verifier_error() -> None:
    """本物の否定には印を付けない (区別できることが目的なので両側を固定する)。"""
    client = _Client({"causal": False, "rationale": "相関のみ"})
    kg, _ = apply_relation_policy(_causal_kg(),
                                  verifier=pipeline._causal_verifier(client))
    assert pipeline._mark_verifier_errors(kg) == 0
    check = kg["edges"][0]["causal_check"]
    assert check["glyph" if False else "reason"] == "独立検証器が因果を否定"
    assert "reason_code" not in check


# ================================================== ギャップ 3 型 (設計書 §9)


def _layered_kg() -> dict:
    """3 型がすべて出る最小の R2a 世代 KG。"""
    return {
        "nodes": [
            {"id": "c001", "label": "手法あり", "community_id": "comm_001",
             "claim_refs": ["np:aaa"]},
            {"id": "c002", "label": "手法なし", "community_id": "comm_001",
             "claim_refs": ["np:bbb"]},
            {"id": "c003", "label": "主張なし", "community_id": "comm_001"},
            # 構造ギャップ (孤立) を 1 つ混ぜて 3 型が同居する形にする
            {"id": "c004", "label": "孤立した概念", "community_id": "comm_002"},
        ],
        "edges": [
            {"id": "r001", "from": "c001", "to": "c003", "glyph": "wave",
             "layer_tags": {"layer_A": [], "layer_B": ["method_of"],
                            "layer_C": ["correlates_with"], "layer_D": []}},
            {"id": "r002", "from": "c002", "to": "c003", "glyph": "wave",
             "layer_tags": {"layer_A": [], "layer_B": ["result_of"],
                            "layer_C": ["causes"], "layer_D": []},
             "validation": {"status": "uncertain", "combined": 0.62,
                            "scores": {"nli": 0.62}, "requires_human_review": True}},
            {"id": "r003", "from": "c001", "to": "c002", "glyph": "wave",
             "layer_tags": {"layer_A": [], "layer_B": [],
                            "layer_C": ["causes"], "layer_D": []},
             "validation": {"status": "rejected", "combined": 0.2,
                            "scores": {"nli": 0.2}, "requires_human_review": True}},
        ],
        "communities": [{"id": "comm_001", "name": "テーマ", "is_gap": False}],
    }


def test_structural_gaps_keep_their_ids_and_gain_the_type() -> None:
    """R1 からの検出は gap_id を変えずに structural へ写像する (§9)。

    gap_id が変わると、既に confirm/dismiss された候補の引き継ぎ
    (_merge_gap_decisions) が切れて有用率 KPI の分母が飛ぶ。
    """
    kg = json.loads(KG_FIXTURE.read_text(encoding="utf-8"))
    gaps = [g.to_dict() for g in detect_gaps(kg)]
    assert gaps and all(g["gap_type"] == "structural" for g in gaps)
    assert all(g["detected_from_layer"] == "L4-L6" for g in gaps)
    assert all(g["toulmin"]["grounds"] and g["toulmin"]["warrant"] for g in gaps)
    assert all(g["detection_signal"] for g in gaps)
    assert {g["gap_id"].rsplit("-", 1)[0] for g in gaps} <= {
        "gap-isolated", "gap-weak", "gap-bridge", "gap-declared",
        "gap-bridge-comm_001", "gap-bridge-comm_002", "gap-bridge-comm_003"}


def test_discourse_gap_when_a_claim_has_no_method_sentence() -> None:
    """主張はあるのに手法の文に基づく関係が無い概念 = 言説ギャップ (§9)。"""
    gaps = {g.gap_id: g for g in detect_gaps(_layered_kg())}
    assert "gap-discourse-c002" in gaps
    assert "gap-discourse-c001" not in gaps      # method_of が付いた関係を持つ
    assert "gap-discourse-c003" not in gaps      # そもそも主張が無い

    gap = gaps["gap-discourse-c002"].to_dict()
    assert gap["gap_type"] == "discourse" and gap["detected_from_layer"] == "layer_B"
    assert gap["presumed_type"] == "data"
    assert "claim_refs=1" in gap["detection_signal"]
    assert "method_of" in gap["toulmin"]["grounds"]
    assert gap["toulmin"]["warrant"]


def test_causal_gap_from_uncertain_and_rejected_candidates() -> None:
    """裏付けを得られず相関止まりになった causes 候補 = 因果ギャップ (§9)。"""
    gaps = {g.gap_id: g for g in
            detect_gaps(_layered_kg(), rejection_log="logs/rejections/x.jsonl")}
    assert "gap-causal-r002" in gaps and "gap-causal-r003" in gaps
    assert "gap-causal-r001" not in gaps         # causes タグを持たない

    uncertain = gaps["gap-causal-r002"].to_dict()
    rejected = gaps["gap-causal-r003"].to_dict()
    assert uncertain["gap_type"] == rejected["gap_type"] == "causal"
    assert uncertain["detected_from_layer"] == "layer_C"
    assert uncertain["presumed_type"] == "unknown"      # 判断保留
    assert rejected["presumed_type"] == "true"          # 裏付けなしと判定された
    assert rejected["confidence"] > uncertain["confidence"]
    assert "validation=uncertain" in uncertain["detection_signal"]
    # rejected のものだけ rejection_log を出典に添える
    assert any("rejection_log" in link for link in rejected["evidence_links"])
    assert not any("rejection_log" in link for link in uncertain["evidence_links"])


def test_corroborated_causes_are_not_a_gap() -> None:
    """矢印として点灯した因果はギャップにしない (点いているものを疑わない)。"""
    kg = _layered_kg()
    kg["edges"][1]["glyph"] = "arrow"
    kg["edges"][1]["validation"] = {"status": "validated", "combined": 0.9,
                                    "scores": {"nli": 0.9},
                                    "requires_human_review": False}
    assert "gap-causal-r002" not in {g.gap_id for g in detect_gaps(kg)}


def test_old_generation_kg_yields_only_structural_gaps() -> None:
    """層タグが無い世代では新型はゼロ件 = 旧セッションの挙動は変わらない。"""
    kg = _layered_kg()
    for edge in kg["edges"]:
        edge.pop("layer_tags", None)
        edge.pop("validation", None)
    for node in kg["nodes"]:
        node.pop("claim_refs", None)
    kinds = {g.gap_type for g in detect_gaps(kg)}
    assert kinds <= {"structural"}


def test_gap_records_validate_against_the_schema(tmp_path) -> None:
    """3 型のレコードが layout_plan スキーマを通る (§9「スキーマ登録」)。"""
    from cc_core.detail import build_multilevel_plan

    kg = _layered_kg()
    plan = build_multilevel_plan(kg, default_level="standard")
    plan["gaps"] = [g.to_dict() for g in detect_gaps(kg)]
    result = validate_layout_plan(plan)
    assert result.valid, result.errors

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    props = schema["properties"]["gaps"]["items"]["properties"]
    assert set(props["gap_type"]["enum"]) == set(GAP_KINDS)
    assert set(props["toulmin"]["properties"]) == {"grounds", "warrant"}


# ================================== ギャップ確定の引き継ぎ (_merge_gap_decisions)


def _write_session(root: Path, session: str, kg: dict) -> None:
    from cc_core.detail import build_multilevel_plan

    graphs = root / "graphs"
    graphs.mkdir(parents=True, exist_ok=True)
    editing.kg_file(session, graphs_dir=graphs).write_text(
        json.dumps(kg, ensure_ascii=False), encoding="utf-8")
    plan = build_multilevel_plan(kg, default_level="standard")
    plan["gaps"] = [g.to_dict() for g in detect_gaps(kg)]
    editing.plan_file(session, graphs_dir=graphs).write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8")


def test_gap_decisions_survive_rebuild_for_all_three_types(tmp_path) -> None:
    """confirm/dismiss は 3 型とも rebuild をまたいで残る (§9「現行流用」)。

    editing.py は凍結されているので、`_merge_gap_decisions` は gap_id だけを
    見る。3 型が同じ ID 規約に乗っていることの機械検査でもある。
    """
    from cc_core.gaps import apply_decision

    graphs = tmp_path / "graphs"
    _write_session(tmp_path, "s1", _layered_kg())
    plan = editing.load_plan("s1", graphs_dir=graphs)
    picked = {}
    for kind in GAP_KINDS:
        gap = next((g for g in plan["gaps"] if g["gap_type"] == kind), None)
        if gap is None:
            continue
        apply_decision(plan, gap["gap_id"], "confirm", user_id="tester")
        picked[kind] = gap["gap_id"]
    editing.plan_file("s1", graphs_dir=graphs).write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    assert set(picked) == set(GAP_KINDS)

    rebuilt = editing.rebuild_session("s1", graphs_dir=graphs)
    by_id = {g["gap_id"]: g for g in rebuilt["gaps"]}
    for kind, gap_id in picked.items():
        assert by_id[gap_id]["status"] == "confirmed", kind
        assert by_id[gap_id]["confirmed_by"] == "tester"
        assert by_id[gap_id]["gap_type"] == kind


# ============================================== 3 世代の互換 (受け入れ基準 4)


def _pre_r15_kg() -> dict:
    """R1.5 より前の原本: 関係ポリシー適用**前**。polarity も層タグも無い。"""
    return {
        "graph_version": "kg_pre",
        "nodes": [{"id": "c001", "label": "概念A", "community_id": "comm_001"},
                  {"id": "c002", "label": "概念B", "community_id": "comm_001"},
                  {"id": "c003", "label": "概念C", "community_id": "comm_002"},
                  {"id": "c004", "label": "概念D", "community_id": "comm_002"}],
        "edges": [{"id": "r001", "from": "c001", "to": "c002", "glyph": "arrow",
                   "label": "影響する",
                   "evidence_span": [{"document_id": "d1", "surface": "相関が見られた"}]},
                  {"id": "r002", "from": "c003", "to": "c004", "glyph": "zigzag",
                   "label": "対立する"}],
        "communities": [{"id": "comm_001", "name": "テーマ1", "is_gap": False},
                        {"id": "comm_002", "name": "テーマ2", "is_gap": False}],
    }


def _r15_kg() -> dict:
    """R1.5 の原本: ポリシー適用済み (causal_check あり)。層タグはまだ無い。"""
    kg = _pre_r15_kg()
    kg["graph_version"] = "kg_r15"
    kg["edges"][0].update({
        "glyph": "wave", "label": "関連",
        "causal_check": {"lexicon_hit": [], "verifier_verdict": "skipped",
                         "demoted_from": "arrow",
                         "reason": "相関表現のみで因果の語彙証拠が無い"}})
    kg["edges"][1].update({
        "glyph": "tension",
        "causal_check": {"verifier_verdict": "skipped", "demoted_from": "zigzag",
                         "reason": "矛盾判定は L8 (R2) で行う"}})
    return kg


def _r2a_kg() -> dict:
    """R2a の原本: 層タグ・polarity・provenance・validation まで刻まれている。"""
    kg = _r15_kg()
    kg["graph_version"] = "kg_r2a"
    kg["nodes"][0]["onto_class"] = "bfo:Process"
    kg["nodes"][0]["claim_refs"] = ["np:aaa"]
    kg["edges"][0]["layer_tags"] = {"layer_A": [], "layer_B": ["result_of"],
                                    "layer_C": ["causes"], "layer_D": []}
    kg["edges"][0]["validation"] = {"status": "uncertain", "combined": 0.6,
                                    "scores": {"nli": 0.6},
                                    "requires_human_review": True}
    apply_meta(kg, extractor_model="gpt-5.6-sol",
               validator_ids=["llm-nli:terra"])
    return kg


GENERATIONS = {"pre-r1.5": _pre_r15_kg, "r1.5": _r15_kg, "r2a": _r2a_kg}


@pytest.mark.parametrize("generation", sorted(GENERATIONS))
def test_three_generations_load_edit_and_rebuild(generation, tmp_path) -> None:
    """3 世代の地図が読めて・編集できて・再構成できる (受け入れ基準 4)。

    R2a を出したあとで古い地図が開けなくなるのが一番あってはならない事故
    なので、世代ごとに読込 → rename → retype → rebuild を通す。
    """
    graphs = tmp_path / "graphs"
    kg = GENERATIONS[generation]()
    _write_session(tmp_path, generation.replace(".", ""), kg)
    session = generation.replace(".", "")

    before = editing.load_plan(session, graphs_dir=graphs)
    assert before["levels"]["standard"]["nodes"] == 4

    editing.append_edit(session, {"op": "rename_node", "target": "c001",
                                  "payload": {"label": "改名した概念"}},
                        graphs_dir=graphs, eval_log=None)
    editing.append_edit(session, {"op": "retype_edge", "target": "r001",
                                  "payload": {"glyph": "double"}},
                        graphs_dir=graphs, eval_log=None)
    plan = editing.rebuild_session(session, graphs_dir=graphs)

    labels = {n["id"]: n["label"] for n in plan["_level_plans"]["detailed"]["nodes"]}
    glyphs = {e["id"]: e["glyph"] for e in plan["_level_plans"]["detailed"]["edges"]}
    assert labels["c001"] == "改名した概念"
    assert glyphs["r001"] == "double"          # ユーザーの指定が最終権威
    assert plan["provenance"]["edit_count"] == 2
    # 原本は不変 (編集は追記ログの側に積む)
    assert editing.load_kg(session, graphs_dir=graphs) == kg
    assert validate_layout_plan(plan).valid


@pytest.mark.parametrize("generation", sorted(GENERATIONS))
def test_rebuild_does_not_move_untouched_glyphs(generation, tmp_path) -> None:
    """触っていない関係の記号は再構成で動かない (投影は生成時だけ / 裁定 C)。"""
    graphs = tmp_path / "graphs"
    session = "g" + generation.replace(".", "").replace("-", "")
    kg = GENERATIONS[generation]()
    _write_session(tmp_path, session, kg)
    before = {e["id"]: e["glyph"]
              for e in editing.load_plan(session, graphs_dir=graphs)
              ["_level_plans"]["detailed"]["edges"]}

    editing.append_edit(session, {"op": "rename_node", "target": "c003",
                                  "payload": {"label": "別名"}},
                        graphs_dir=graphs, eval_log=None)
    plan = editing.rebuild_session(session, graphs_dir=graphs)
    after = {e["id"]: e["glyph"] for e in plan["_level_plans"]["detailed"]["edges"]}
    assert after == before
    # reconcile は R2a セッションでは何もしない (kg と plan が一致しているため)
    if generation == "r2a":
        assert not plan["provenance"].get("policy_reconciled")


# ================================================ offline E2E (受け入れ基準 2)


def test_offline_rerun_reuses_layers_and_completes(mock_run, tmp_path) -> None:
    """layers_session があるセッションの再実行は "reused" で全段完走する (§9)。

    再実行では LLM を 1 回も呼ばないのに、投影 (⚡ / ◇◧) もギャップ 3 型も
    元の run と同じ結果になる — 層の情報が不変なサイドカーに載っている、
    という設計の実証。
    """
    summary, kg, _, _ = mock_run(FakeAnalysisAgent(claims=[
        {"claim_text": "Teams議論から矛盾点が増加する", "is_underspecified": False,
         "related_concepts": ["Teams議論", "矛盾点"]},
        {"claim_text": "Teams議論から矛盾点は増加しない", "is_underspecified": False,
         "related_concepts": ["Teams議論", "矛盾点"]},
    ]), FakeVerificationAgent())
    session = summary["session"]
    assert summary["layers"]["status"] == "generated"

    again = pipeline.run_pipeline(
        "同じ地図をもう一度", target="file", offline=True,
        kg_file=f"graphs/kg_session_{session}.json",
        verify_causal=False, export_svg=False)

    assert again["layers"]["status"] == "reused"
    assert again["layers"]["source_session"] == session
    assert again["layers"]["stats"]["claims"] == summary["layers"]["stats"]["claims"]
    # 再利用でも新セッション名でサイドカーが自己完結する
    assert layers_store.exists(again["session"], graphs_dir="graphs")
    # offline は検証器を呼べないので、検証段は正直に skipped と言う
    assert again["validation"]["status"] == "skipped_offline"

    kg2 = json.loads(Path(again["knowledge_graph"]["saved"]).read_text(encoding="utf-8"))
    glyphs = {e["id"]: e["glyph"] for e in kg2["edges"]}
    assert glyphs == {e["id"]: e["glyph"] for e in kg["edges"]}
    assert "zigzag" in glyphs.values()                  # ⚡ が点いたまま
    # 全段完走: 描画・出力・KPI まで到達している (検証の verdict はモック依存)
    assert again["projection"]["status"] == "RENDER_OK"
    assert again["export"]["excalidraw"] and again["kpi"]

    kinds = again["gaps"]["by_gap_type"]
    assert set(kinds) == set(GAP_KINDS) and sum(kinds.values()) == again["gaps"]["candidates"]


def test_rerun_does_not_leave_a_stale_explanation_on_the_zigzag() -> None:
    """⚡ が点いたエッジの説明が表示と食い違わない (受け入れ基準 3)。

    ④relate は矛盾候補を毎回 tension へ落として「矛盾判定は L8 (R2) で行う」と
    書く (裁定 7)。R2a の地図を再実行するとその記録が層 D の refutes より後に
    書かれ、⚡ を表示しながら「候補として非断定表示」と説明する状態になる
    【実測: layers 再利用の offline 再実行】。⑦meta で畳む。
    """
    kg = {"nodes": [{"id": "c001", "label": "A"}, {"id": "c002", "label": "B"}],
          "edges": [{"id": "r001", "from": "c001", "to": "c002", "glyph": "zigzag",
                     "layer_tags": {"layer_A": [], "layer_B": [],
                                    "layer_C": [], "layer_D": ["refutes"]}}]}
    kg, _ = apply_relation_policy(kg)                 # ⚡ -> tension + R1 の記録
    assert kg["edges"][0]["causal_check"]["demoted_from"] == "zigzag"

    apply_meta(kg, extractor_model="kg_file")
    edge = kg["edges"][0]
    assert edge["glyph"] == "zigzag"                  # 投影規則③ で戻る
    assert "demoted_from" not in edge["causal_check"]
    assert "投影規則③" in edge["causal_check"]["reason"]


def test_mock_e2e_lights_the_new_glyphs_and_reports_gap_kinds(mock_run) -> None:
    """1 周で ⚡ (矛盾) と ◇◧ (分類・構成) の点灯条件が揃うことを固定する。"""
    agent = FakeAnalysisAgent(
        claims=[{"claim_text": "Teams議論から矛盾点が増加する",
                 "is_underspecified": False,
                 "related_concepts": ["Teams議論", "矛盾点"]},
                {"claim_text": "Teams議論から矛盾点は増加しない",
                 "is_underspecified": False,
                 "related_concepts": ["Teams議論", "矛盾点"]}],
        relations=[{"from": "マルチモーダル入力", "to": "OneNote・Teams・論文",
                    "relation": "is_a"}])
    summary, kg, _, _ = mock_run(agent, FakeVerificationAgent())

    glyphs = [e["glyph"] for e in kg["edges"]]
    assert "zigzag" in glyphs                      # ⚡ = 層 D の refutes
    assert set(summary["gaps"]["by_gap_type"]) == set(GAP_KINDS)
    assert summary["gaps"]["candidates"] == sum(summary["gaps"]["by_gap_type"].values())
    # 層タグは残っている (UI は畳むが内部 30 種は失わない)
    tagged = [e for e in kg["edges"] if e.get("layer_tags")]
    assert tagged and any(e["layer_tags"]["layer_B"] for e in tagged)


# ==================================================== CLI (設計書 §10)


def test_chat_summary_prints_the_layers_line(mock_run, capsys) -> None:
    """chat の結果表示に多層分析の 1 行が出る (§10)。

    `_print_summary` は summary の形に強く依存しているので、フリップ直後に
    KeyError で落ちないことを実物の summary で確かめる。
    """
    from cc_orchestrator.chat import _print_summary

    summary, _, _, _ = mock_run(FakeAnalysisAgent(), FakeVerificationAgent())
    _print_summary(summary)
    out = capsys.readouterr().out

    assert "🧩 多層分析 [generated]" in out
    assert "LLM " in out and "call" in out
    assert "型: 構造" in out and "言説" in out and "因果" in out


def test_chat_summary_omits_the_layers_line_when_disabled(tmp_path,
                                                          monkeypatch, capsys) -> None:
    """`--no-layers` では多層分析の行を出さない (無いものを 0 件と見せない)。"""
    from cc_orchestrator.chat import _print_summary

    monkeypatch.chdir(tmp_path)
    summary = pipeline.run_pipeline(
        "今週の研究を概念地図として整理して", target="file", kg_file=str(KG_FIXTURE),
        offline=True, layers=False, verify_causal=False, export_svg=False)
    _print_summary(summary)
    out = capsys.readouterr().out
    assert "多層分析" not in out
    assert "❓ ギャップ候補" in out and "型: 構造" in out    # 型の内訳は常に出る


def test_layers_summary_accepts_plan_kg_or_session_id(tmp_path) -> None:
    """`--layers-summary` は plan / kg / サイドカー / 素の ID を同じに扱う。"""
    from cc_orchestrator.chat import _layers_target

    assert _layers_target("20260807_120000") == ("20260807_120000",
                                                 Path(layers_store.GRAPHS_DIR))
    for name in ("layout_plan_session_s1", "kg_session_s1", "layers_session_s1"):
        session, graphs = _layers_target(f"graphs/{name}.json")
        assert (session, graphs) == ("s1", Path("graphs"))


# ==================================================== Web (設計書 §10)


@pytest.fixture
def web(tmp_path, monkeypatch):
    """offline ジョブが回せる Web クライアント (Foundry / MCP を使わない)。"""
    import time

    from fastapi.testclient import TestClient

    from cc_web import account
    from cc_web.app import create_app

    (tmp_path / "graphs").mkdir()
    (tmp_path / "graphs" / "kg_web_test.json").write_text(
        KG_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(account, "_az_upn", lambda: "tester@example.ac.jp")
    account.clear_cache()

    with TestClient(create_app()) as client:
        def run_job(**extra) -> dict:
            body = {"message": "今週の研究を概念地図として整理して",
                    "kg_file": "kg_web_test.json", "offline": True,
                    "causal_verify": False, "target": "file"}
            body.update(extra)
            res = client.post("/api/jobs", json=body)
            assert res.status_code == 202, res.text
            job_id = res.json()["job_id"]
            deadline = time.time() + 60
            job: dict = {}
            while time.time() < deadline:
                job = client.get(f"/api/jobs/{job_id}").json()
                if job["status"] in ("done", "error"):
                    break
                time.sleep(0.05)
            assert job.get("status") == "done", job.get("error")
            return job["summary"]
        client.run_job = run_job          # type: ignore[attr-defined]
        yield client
    account.clear_cache()


def test_jobs_body_carries_the_layers_flag(web) -> None:
    """Web からも多層分析を切れる (§10 の jobs ボディ `layers`)。既定は ON。"""
    assert web.run_job()["layers"]["status"] == "skipped_offline"   # 既定 = 走らせた
    assert web.run_job(layers=False)["layers"] == {"status": "disabled"}


def test_layers_api_returns_the_sidecar(web, tmp_path) -> None:
    """GET /api/sessions/{s}/layers が 200 で主張と統計を返す (§10)。"""
    session = web.run_job()["session"]
    # offline 実行はサイドカーを作らないので、生成済みの層を置いて再現する
    doc = layers_store.new_document(session)
    doc["claims"] = [{"nanopub_id": "np:aaa",
                      "assertion": {"claim_id": "cl-001", "claim_text": "主張の本文",
                                    "is_underspecified": False,
                                    "related_concepts": ["概念A"]},
                      "provenance": {"extractor_id": "cc-analysis"},
                      "pub_info": {"created_at": "2026-08-07T00:00:00"},
                      "validation": {"status": "validated", "combined": 0.9,
                                     "scores": {"nli": 0.9},
                                     "requires_human_review": False}}]
    doc["zones"] = [{"sentence_id": "d1#0000#aa", "text": "文", "zone_label": "Result",
                     "zone_system": "CoreSC", "confidence": 0.8,
                     "document_id": "d1", "char_start": 0, "char_end": 1}]
    doc["stats"] = layers_store.compute_stats(doc, sentences=1, llm_calls=3)
    layers_store.save(session, doc, graphs_dir=tmp_path / "graphs")

    body = web.get(f"/api/sessions/{session}/layers").json()
    assert body["session"] == session and body["version"] == 1
    assert body["stats"]["claims"] == 1 and body["stats"]["validated"] == 1
    assert body["claims"][0]["assertion"]["claim_text"] == "主張の本文"
    # 文の全文 (zones) は UI へ運ばない — クリック展開に要るのは主張だけ
    assert "zones" not in body


def test_layers_api_404_says_the_map_predates_r2a(web) -> None:
    """R2a 以前の地図では 404 + 理由 (バグに見せない / §10)。"""
    session = web.run_job()["session"]
    res = web.get(f"/api/sessions/{session}/layers")
    assert res.status_code == 404
    assert "R2a 以前" in res.json()["error"]["message"]
    # 未知のセッションも 404 (層の有無より前にセッションを確かめる)
    assert web.get("/api/sessions/nosuch/layers").status_code == 404


def test_edit_glyphs_are_the_eight_ui_symbols() -> None:
    """EDIT_GLYPHS は 8 種。hole / tension は選択肢に出さない (§10)。"""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    block = re.search(r"var EDIT_GLYPHS = \[(.*?)\];", source, re.S)
    assert block, "app.js の EDIT_GLYPHS が見つからない"
    pairs = re.findall(r'\["(\w+)", "([^"]+)"\]', block.group(1))
    keys = [k for k, _ in pairs]

    assert len(keys) == 8 and len(set(keys)) == 8
    assert set(keys) == set(VALID_GLYPHS) - {"hole", "tension"}
    assert keys[:2] == ["arrow", "wave"]           # 既定 (wave) は選択肢にある

    # 表示名は GLYPH_INFO と一致させる (同じ記号が 2 つの名前で出ない)
    info = re.search(r"var GLYPH_INFO = \{(.*?)\n  \};", source, re.S).group(1)
    names = dict(re.findall(r"(\w+): \{ label: \"([^\"]+)\"", info))
    assert {k: v for k, v in pairs} == {k: names[k] for k in keys}


def test_gap_kind_labels_cover_every_type() -> None:
    """型バッジの語彙が gaps.GAP_KINDS と 1:1 (未知の型が生ラベルで出ない)。"""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    block = re.search(r"var GAP_KIND_LABEL = \{(.*?)\};", source, re.S)
    assert block
    keys = set(re.findall(r"(\w+):", block.group(1)))
    assert keys == set(GAP_KINDS)
