"""R2a M5+M6 — 3 検証器・rejection_log・論証・内部矛盾 (設計書 §11)。

主眼は 5 つ:
  - **合成は走れた検証器だけで再正規化**する (§7)。検証器が落ちた run で
    スコアが下がると、モデルの障害が「主張が弱い」に化ける
  - **閾値の 3 分岐**が設計どおり (validated / uncertain / rejected)。
    uncertain は捨てずに要レビューで登録を続ける
  - **rejected は rejection_log に残す** (§3.3)。何を落としたかが追える
  - epistemic_strength と矛盾の候補ペア絞り込みは **決定的コード** (§6)。
    LLM の自己申告を使わない
  - refutes が成立したエッジだけが ⚡ になる (投影規則③)。対応エッジが
    無ければサイドカーの記録だけで、**新しいエッジは作らない**

各テストは tmp_path を作業ディレクトリにするので production/ を汚さない。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cc_core import verifiers
from cc_core.detail import EDGE_CARRY, build_multilevel_plan, project
from cc_core.layer_assign import apply_validation, stamp_refutes
from cc_core.layers import apply_meta, project_glyph
from cc_core.validate import validate_layout_plan
from cc_core.verifiers import (
    ONTOLOGY_VERIFIER_ID,
    LLMClaimVerifier,
    LLMNLIVerifier,
    LocalNLIUnavailable,
    LocalNLIVerifier,
    OntologyChecker,
    VerifierError,
    VerifierResult,
    combine,
    judge,
    make_nli_verifier,
    nli_verifier_id,
)

from test_r2a_analysis import (                # M3/M4 のモック資産を流用する
    FakeAnalysisAgent,
    FakeVerificationAgent,
    mock_run,                                  # noqa: F401 — pytest fixture
)

PRODUCTION = Path(__file__).resolve().parents[1]
STATIC = PRODUCTION / "src" / "cc_web" / "static"
SCHEMA_PATH = PRODUCTION / "schemas" / "layout_plan.schema.json"


# --------------------------------------------------------------- 補助


def claim_of(nanopub: str, text: str, *, concepts: list[str] | None = None,
             spans: list[str] | None = None, status: str | None = None) -> dict:
    """サイドカーの claims 1 件 (§3.2)。"""
    claim: dict = {
        "nanopub_id": nanopub,
        "assertion": {"claim_id": nanopub[-5:], "claim_text": text,
                      "is_underspecified": False,
                      "related_concepts": concepts or []},
        "provenance": {"source_span": spans or [], "extractor_id": "cc-analysis",
                       "extraction_timestamp": "2026-08-07T00:00:00",
                       "extraction_method": "llm-fewshot"},
        "pub_info": {"document_id": "d1"},
    }
    if status:
        claim["validation"] = {"status": status, "combined": 0.9, "scores": {},
                               "requires_human_review": False}
    return claim


def kg_of(*edges: dict) -> dict:
    """2〜4 概念の小さな kg。ノードは edge の from/to から起こす。"""
    ids = {e[k] for e in edges for k in ("from", "to")}
    return {"nodes": [{"id": i, "label": f"概念{i[-1]}", "community_id": "comm_1"}
                      for i in sorted(ids)],
            "edges": [dict(e) for e in edges]}


class StubVerifier:
    """Protocol 実装の差し替え口を試すための最小の検証器。"""

    def __init__(self, score: float, verifier_id: str = "stub:1",
                 fail: bool = False) -> None:
        self.verifier_id = verifier_id
        self.score = score
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def check(self, premise: str, hypothesis: str) -> VerifierResult:
        self.calls.append((premise, hypothesis))
        if self.fail:
            raise VerifierError("差し替えた検証器が落ちた")
        return {"label": "entails", "score": self.score,
                "verifier_id": self.verifier_id, "detail": "stub"}


# ==================================================== M5: Protocol と検証器


def test_protocol_verifiers_are_swappable() -> None:
    """EntailmentVerifier は差し替えられる (裁定 A の抽象化が効いている)。"""
    stub = StubVerifier(0.9)
    kg = kg_of({"id": "r001", "from": "c001", "to": "c002", "glyph": "arrow",
                "evidence_span": [{"document_id": "d1", "surface": "機序が示された"}]})
    results, report = verifiers.run_validation(
        kg, [], nli=stub, llm=StubVerifier(0.9, "stub:2"), session="s1")

    assert stub.calls, "差し替えた検証器が呼ばれていない"
    assert results["r001"]["status"] == "validated"
    # 実際に走った検証器の ID だけが並ぶ (provenance.validator_ids の材料)
    assert set(report.verifier_ids) == {"stub:1", "stub:2", ONTOLOGY_VERIFIER_ID}


def test_verifier_ids_follow_the_designed_shape() -> None:
    """"llm-nli:<model>" / "llm-verifier:<model>" / "ontology-rules" (§7)。"""
    from cc_core.layers import verifier_id as llm_verifier_id

    assert nli_verifier_id("gpt-5.6-terra") == "llm-nli:terra"
    assert llm_verifier_id("gpt-5.6-terra") == "llm-verifier:terra"
    assert OntologyChecker().verifier_id == "ontology-rules"
    assert LLMNLIVerifier(lambda p: "", model="gpt-5.6-terra").verifier_id \
        == "llm-nli:terra"
    assert LLMClaimVerifier(lambda p: "", model="gpt-5.6-terra").verifier_id \
        == "llm-verifier:terra"


def test_nli_verifier_repairs_the_llm_answer() -> None:
    """未知ラベルは neutral へ、score は 0〜1 へ (analysis.py と同じ思想)。"""
    v = LLMNLIVerifier(lambda p: "", model="terra")
    assert v.repair({"label": "entails", "score": 1.0})["score"] == 1.0
    assert v.repair({"label": "contradicts", "score": 1.0})["score"] == 0.0
    # confidence は「基準値」と「情報なし (0.5)」の補間 — 自信が無ければ 0.5 寄り
    assert v.repair({"label": "entails", "score": 0.0})["score"] == 0.5
    broken = v.repair({"label": "たぶん含意", "score": 5})
    assert broken["label"] == "neutral" and "neutral" in broken["detail"]
    with pytest.raises(VerifierError):
        v.repair(["リストで返ってきた"])


def test_verifiers_reject_an_answer_to_a_different_question() -> None:
    """契約違いの応答は「走らなかった」扱い (実測: cc-verification が
    描画検証の {"verdict": "PASS"} を返した【2026-08-07】)。

    neutral / 不支持として合成に入れると、エージェントの結線ミスが「主張が
    弱い」に化けて全件を静かに rejected へ押し下げる。
    """
    scene_verdict = json.dumps({"verdict": "PASS", "summary": "一致"})
    with pytest.raises(VerifierError, match="label"):
        LLMNLIVerifier(lambda p: scene_verdict, model="terra").check("前提", "仮説")
    with pytest.raises(VerifierError, match="supported"):
        LLMClaimVerifier(lambda p: scene_verdict, model="terra").check("前提", "仮説")

    # 落ちた検証器は再正規化で外れる — 静かな全件棄却にならない
    kg = kg_of({"id": "r001", "from": "c001", "to": "c002", "glyph": "arrow",
                "evidence_span": [{"document_id": "d1", "surface": "機序の記述"}]})
    results, report = verifiers.run_validation(
        kg, [], nli=LLMNLIVerifier(lambda p: scene_verdict, model="terra"),
        llm=LLMClaimVerifier(lambda p: scene_verdict, model="terra"), session="s1")
    assert results["r001"]["status"] != "rejected"
    assert results["r001"]["scores"] == {"ontology": 1.0}
    assert len(report.errors) == 2 and report.rejections == 0


def test_verifier_prompts_forbid_tool_use() -> None:
    """cc-verification は描画検証の tools を持つ (裁定 E の流用)。先に釘を刺す。"""
    nli = LLMNLIVerifier(lambda p: "", model="terra").prompt("前提", "仮説")
    llm = LLMClaimVerifier(lambda p: "", model="terra").prompt("前提", "仮説")
    assert "ツールは呼ばず" in nli and "ツールは呼ばず" in llm


def test_nli_verifier_failure_is_not_a_score() -> None:
    """検証器の事故は「走らなかった」— 0.0 として合成に効かせない。"""
    def broken(prompt: str) -> str:
        raise TimeoutError("foundry timeout")

    with pytest.raises(VerifierError):
        LLMNLIVerifier(broken, model="terra").check("前提", "仮説")

    kg = kg_of({"id": "r001", "from": "c001", "to": "c002", "glyph": "arrow",
                "evidence_span": [{"document_id": "d1", "surface": "機序が示された"}]})
    results, report = verifiers.run_validation(
        kg, [], nli=StubVerifier(0.9, "stub:1", fail=True),
        llm=StubVerifier(1.0, "stub:2"), session="s1")
    # 走ったのは llm (1.0) と ontology (1.0) だけ -> 再正規化して 1.0
    assert results["r001"]["combined"] == 1.0
    assert results["r001"]["scores"] == {"llm": 1.0, "ontology": 1.0}
    assert report.errors and "stub:1" not in report.verifier_ids


def test_local_nli_is_a_stub_with_a_helpful_message() -> None:
    """M5 ではスタブ。何を入れれば動くかを伝えて落ちる (裁定 A)。"""
    with pytest.raises(LocalNLIUnavailable) as caught:
        LocalNLIVerifier()
    message = str(caught.value)
    assert "nli" in message and ("pip install" in message or "スタブ" in message)


def test_nli_backend_env_selects_and_falls_back(monkeypatch) -> None:
    """CC_NLI_BACKEND=local を選び、使えなければ LLM へ落として**記録する**。"""
    monkeypatch.setenv("CC_NLI_BACKEND", "local")
    notes: list[str] = []
    chosen = make_nli_verifier(lambda p: "", model="gpt-5.6-terra", notes=notes)
    assert isinstance(chosen, LLMNLIVerifier)
    assert notes and "local" in notes[0]

    monkeypatch.delenv("CC_NLI_BACKEND")
    assert isinstance(make_nli_verifier(lambda p: "", model="terra"), LLMNLIVerifier)


# ==================================================== M5: OntologyChecker


def test_ontology_rule_is_a_cycle_is_zero() -> None:
    """規則1: is_a に循環があれば 0.0 (裁定 B の決定的規則)。"""
    checker = OntologyChecker([
        {"from": "犬", "to": "動物", "relation": "is_a"},
        {"from": "動物", "to": "犬", "relation": "is_a"},
    ])
    assert checker.cycles                     # 循環に居るノードが拾えている
    result = checker.check_relation("犬", "動物", "causes")
    assert result["score"] == 0.0 and result["label"] == "inconsistent"
    assert result["verifier_id"] == ONTOLOGY_VERIFIER_ID


def test_ontology_rule_is_a_and_part_of_on_the_same_pair_is_zero() -> None:
    """規則2: 「一種」と「一部」は同じ対に両立しない。"""
    checker = OntologyChecker([
        {"from": "車輪", "to": "自動車", "relation": "is_a"},
        {"from": "車輪", "to": "自動車", "relation": "part_of"},
    ])
    assert checker.check_relation("車輪", "自動車")["score"] == 0.0
    # 向きを入れ替えて問い合わせても同じ対として見る
    assert checker.check_relation("自動車", "車輪")["score"] == 0.0


def test_ontology_rule_causes_between_is_a_siblings_is_a_warning() -> None:
    """規則3: 兄弟間の causes は 0.5 (禁止ではなく警告)。"""
    checker = OntologyChecker([
        {"from": "犬", "to": "動物", "relation": "is_a"},
        {"from": "猫", "to": "動物", "relation": "is_a"},
    ])
    warned = checker.check_relation("犬", "猫", "causes")
    assert warned["score"] == 0.5 and warned["label"] == "warning"
    # causes 以外の関係では兄弟であることを咎めない
    assert checker.check_relation("犬", "猫", "correlates_with")["score"] == 1.0


def test_ontology_rule_default_is_one() -> None:
    """規則4: どれにも当たらなければ 1.0。関係候補が空でも落ちない。"""
    clean = OntologyChecker([{"from": "犬", "to": "動物", "relation": "is_a"}])
    assert clean.check_relation("犬", "猫", "causes")["score"] == 1.0
    assert OntologyChecker().check_relation("A", "B")["score"] == 1.0
    # Protocol 適合の入口 (premise/hypothesis = 関係の両端)
    assert OntologyChecker().check("A", "B")["score"] == 1.0


def test_ontology_checker_ignores_unusable_relations() -> None:
    """語彙外・壊れた関係候補は黙って捨てる (地図の生成を止めない)。"""
    checker = OntologyChecker([
        {"from": "犬", "to": "動物", "relation": "causes"},   # 層 A ではない
        {"from": "", "to": "動物", "relation": "is_a"},
        "文字列で返ってきた",
    ])
    assert checker.pairs == {} and not checker.cycles


# ==================================================== M5: 合成と閾値


def test_combine_renormalises_over_the_verifiers_that_ran() -> None:
    """走れた検証器だけで再正規化する (§7)。欠けた分で薄まらない。"""
    assert combine({"nli": 1.0, "llm": 1.0, "ontology": 1.0}) == 1.0
    # nli 0.4 / llm 0.35 -> 0.75 を分母にする
    assert combine({"nli": 1.0, "llm": 0.0}) == round(0.4 / 0.75, 4)
    assert combine({"ontology": 0.5}) == 0.5
    assert combine({}) is None                # 「検証していない」は 0.0 ではない
    assert combine({"unknown": 1.0}) is None  # 語彙外の重みは無視する


def test_judge_has_three_branches() -> None:
    """>=0.75 validated / >=0.5 uncertain / <0.5 rejected (§7)。"""
    assert judge({"nli": 1.0, "llm": 1.0})["status"] == "validated"
    assert judge({"nli": 1.0, "llm": 1.0})["requires_human_review"] is False

    borderline = judge({"nli": 0.8, "llm": 0.6, "ontology": 1.0})
    assert borderline["combined"] == 0.78 and borderline["status"] == "validated"

    middle = judge({"nli": 0.6, "llm": 0.6})
    assert middle["status"] == "uncertain"
    assert middle["requires_human_review"] is True      # 登録は継続、要レビュー

    low = judge({"nli": 0.2, "llm": 0.2})
    assert low["status"] == "rejected" and low["combined"] == 0.2


def test_judge_never_validates_on_the_deterministic_check_alone() -> None:
    """ontology だけでは validated にしない (整合性は正しさの裏付けではない)。"""
    only_rules = judge({"ontology": 1.0})
    assert only_rules["status"] == "uncertain"
    assert only_rules["requires_human_review"] is True
    # 検証器が 1 つも走らなければ rejected ではなく uncertain
    nothing = judge({})
    assert nothing["status"] == "uncertain" and nothing["combined"] is None
    assert nothing["requires_human_review"] is True


def test_missing_evidence_forces_human_review() -> None:
    """根拠スパンが空の causes 候補は検証前に要レビュー (§7 の②段相当)。"""
    kg = kg_of({"id": "r001", "from": "c001", "to": "c002", "glyph": "arrow"})
    zones = [{"sentence_id": "d1#0000#aa", "text": "概念1 が 概念2 を引き起こす機序"}]
    results, _ = verifiers.run_validation(
        kg, [], zones=zones, nli=StubVerifier(1.0), llm=StubVerifier(1.0, "s2"),
        session="s1")
    assert results["r001"]["status"] == "validated"
    assert results["r001"]["requires_human_review"] is True


def test_missing_evidence_uses_neighbour_sentences_as_premise() -> None:
    """premise には近傍文を使う (§7)。ラベルを含む文だけを拾う。"""
    stub = StubVerifier(1.0)
    kg = kg_of({"id": "r001", "from": "c001", "to": "c002", "glyph": "arrow"})
    zones = [{"sentence_id": "d1#0000#aa", "text": "無関係な前置きの文である"},
             {"sentence_id": "d1#0001#bb", "text": "概念1 は 概念2 を強く押し上げる"}]
    verifiers.run_validation(kg, [], zones=zones, nli=stub, session="s1")
    premise, hypothesis = stub.calls[0]
    assert premise == "概念1 は 概念2 を強く押し上げる"
    assert "概念1" in hypothesis and "概念2" in hypothesis


# ==================================================== M5: 対象と rejection_log


def test_only_causal_candidates_are_validated() -> None:
    """検証対象は claims 全件 + causes 候補のみ (§7「全エッジではない」)。"""
    kg = kg_of(
        {"id": "r001", "from": "c001", "to": "c002", "glyph": "arrow"},
        {"id": "r002", "from": "c002", "to": "c003", "glyph": "wave",
         "causal_check": {"demoted_from": "arrow"}},        # 降格した候補も対象
        {"id": "r003", "from": "c003", "to": "c004", "glyph": "wave"},
        {"id": "r004", "from": "c001", "to": "c004", "glyph": "isa"},
        {"id": "r005", "from": "c002", "to": "c004", "glyph": "hole"},
    )
    assert [e["id"] for e in verifiers.causal_candidates(kg)] == ["r001", "r002"]
    results, report = verifiers.run_validation(kg, [], nli=StubVerifier(1.0),
                                               session="s1")
    assert set(results) == {"r001", "r002"} and report.targets == 2


def test_rejection_log_row_matches_the_designed_schema(tmp_path) -> None:
    """§3.3 の 1 行 (追記専用 jsonl)。"""
    claims = [claim_of("np:aaa", "根拠のない大胆な主張", spans=["d1#0000#aa"])]
    zones = [{"sentence_id": "d1#0000#aa", "text": "実際には確認できなかった"}]
    kg = kg_of({"id": "r001", "from": "c001", "to": "c002", "glyph": "arrow",
                "evidence_span": [{"document_id": "d1", "surface": "根拠らしきもの"}]})

    results, report = verifiers.run_validation(
        kg, claims, zones=zones, nli=StubVerifier(0.1, "stub:nli"),
        llm=StubVerifier(0.0, "stub:llm"), session="s1",
        logs_dir=tmp_path, timestamp="2026-08-07T12:00:00")

    path = tmp_path / "rejections" / "rejections_s1.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2 and report.rejections == 2
    assert {r["kind"] for r in rows} == {"claim", "causal_edge"}

    row = next(r for r in rows if r["kind"] == "claim")
    assert set(row) == {"ts", "session", "kind", "target_id", "text", "scores",
                        "combined", "verdicts", "evidence_span", "reason"}
    assert row["session"] == "s1" and row["target_id"] == "np:aaa"
    assert row["ts"] == "2026-08-07T12:00:00"
    assert row["scores"] == {"nli": 0.1, "llm": 0.0} and row["combined"] < 0.5
    assert [v["verifier_id"] for v in row["verdicts"]] == ["stub:nli", "stub:llm"]
    assert row["evidence_span"] == ["d1#0000#aa"] and row["reason"]

    # サイドカーには status=rejected として残る (何を落としたかが追える)
    assert claims[0]["validation"]["status"] == "rejected"
    assert report.claims == {"rejected": 1} and report.edges == {"rejected": 1}


def test_rejection_log_appends(tmp_path) -> None:
    """追記専用 — 2 回目の検証で前の記録を消さない。"""
    for _ in range(2):
        verifiers.log_rejection("s1", kind="claim", target_id="np:a", text="t",
                                validation={"combined": 0.1, "scores": {}},
                                logs_dir=tmp_path, reason="低スコア")
    path = verifiers.rejection_path("s1", logs_dir=tmp_path)
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_rejection_log_failure_does_not_break_the_run(tmp_path) -> None:
    """ログが書けなくても地図の生成は続く (サイドカーの save と同じ方針)。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("ディレクトリではなくファイル", encoding="utf-8")
    assert verifiers.log_rejection("s1", kind="claim", target_id="np:a", text="t",
                                   validation={"combined": 0.1, "scores": {}},
                                   logs_dir=blocked, reason="低スコア") is None


def test_validate_max_limits_the_targets(monkeypatch) -> None:
    """CC_VALIDATE_MAX が効く。主張を優先し、エッジから削る。"""
    monkeypatch.setenv("CC_VALIDATE_MAX", "2")
    kg = kg_of(*[{"id": f"r{i:03d}", "from": "c001", "to": "c002",
                  "glyph": "arrow"} for i in range(1, 4)])
    claims = [claim_of("np:a", "主張A"), claim_of("np:b", "主張B")]
    results, report = verifiers.run_validation(kg, claims, nli=StubVerifier(1.0),
                                               session="s1")
    assert report.targets == 2 and results == {}
    assert all("validation" in c for c in claims)
    assert any("CC_VALIDATE_MAX" in n for n in report.notes)


def test_llm_call_budget_stops_at_the_knob(monkeypatch) -> None:
    """呼び出し上限を超えた対象は決定的検証だけになり、validated にならない。"""
    monkeypatch.setenv("CC_VALIDATE_MAX_CALLS", "2")
    kg = kg_of(*[{"id": f"r{i:03d}", "from": "c001", "to": "c002", "glyph": "arrow",
                  "evidence_span": [{"document_id": "d1", "surface": "機序の記述"}]}
                 for i in range(1, 4)])
    results, report = verifiers.run_validation(
        kg, [], nli=StubVerifier(1.0), llm=StubVerifier(1.0, "s2"), session="s1")
    assert report.llm_calls == 2
    assert results["r001"]["status"] == "validated"
    assert results["r002"]["status"] == "uncertain"     # ontology だけで判定
    assert results["r002"]["scores"] == {"ontology": 1.0}
    assert any("CC_VALIDATE_MAX_CALLS" in n for n in report.notes)


# ==================================================== M5: kg への反映と投影


def test_validation_drives_the_arrow_projection() -> None:
    """0.75 以上なら →、足りなければ 〜 のまま (投影規則④/⑩)。"""
    kg = kg_of(
        {"id": "r001", "from": "c001", "to": "c002", "glyph": "arrow",
         "layer_tags": {"layer_A": [], "layer_B": [], "layer_C": ["causes"],
                        "layer_D": []}},
        {"id": "r002", "from": "c002", "to": "c003", "glyph": "arrow",
         "layer_tags": {"layer_A": [], "layer_B": [], "layer_C": ["causes"],
                        "layer_D": []}})
    apply_validation(kg, {"r001": {"status": "validated", "combined": 0.81,
                                   "scores": {}, "requires_human_review": False},
                          "r002": {"status": "rejected", "combined": 0.31,
                                   "scores": {}, "requires_human_review": False}})
    apply_meta(kg, extractor_model="gpt-5.6-sol")

    by_id = {e["id"]: e for e in kg["edges"]}
    assert by_id["r001"]["glyph"] == "arrow"
    # rejected な causes 候補は矢印にせず相関のまま + 降格の記録 (KPI 連続性)
    assert by_id["r002"]["glyph"] == "wave"
    assert by_id["r002"]["causal_check"]["demoted_from"] == "arrow"
    assert by_id["r002"]["validation"]["status"] == "rejected"


def test_rejected_claims_lose_their_refs_but_stay_in_the_sidecar() -> None:
    """rejected な主張は地図から参照を外す。記録はサイドカーに残す。"""
    kg = kg_of({"id": "r001", "from": "c001", "to": "c002", "glyph": "wave",
                "claim_refs": ["np:ok", "np:ng"]})
    kg["nodes"][0]["claim_refs"] = ["np:ng"]
    claims = [claim_of("np:ok", "通った主張", status="validated"),
              claim_of("np:ng", "落ちた主張", status="rejected")]

    stats = apply_validation(kg, {}, claims=claims)
    assert stats["refs_dropped"] == 2
    assert kg["edges"][0]["claim_refs"] == ["np:ok"]
    assert "claim_refs" not in kg["nodes"][0]           # 空配列は残さない
    assert len(claims) == 2 and claims[1]["validation"]["status"] == "rejected"


def test_validation_is_carried_to_the_plan_and_the_web_view() -> None:
    """3 点セット: EDGE_CARRY / schema / sessions.EDGE_FIELDS に登録済み。"""
    from cc_web.sessions import EDGE_FIELDS

    assert "validation" in EDGE_CARRY and "validation" in EDGE_FIELDS
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    properties = schema["properties"]["edges"]["items"]["properties"]
    assert "validation" in properties
    assert set(properties["validation"]["properties"]["status"]["enum"]) == {
        "validated", "uncertain", "rejected"}

    kg = kg_of({"id": "r001", "from": "c001", "to": "c002", "glyph": "wave",
                "validation": {"status": "uncertain", "combined": 0.62,
                               "scores": {"nli": 0.6, "ontology": 1.0},
                               "requires_human_review": True}})
    view = project(build_multilevel_plan(kg, default_level="detailed"), "detailed")
    edge = next(e for e in view["edges"] if e["id"] == "r001")
    assert edge["validation"]["combined"] == 0.62
    assert validate_layout_plan(view).valid                 # additionalProperties


def test_stages_include_validate_and_rhetoric() -> None:
    """進捗 2 段の追加は pipeline と app.js を同一コミットで (§9)。"""
    from cc_orchestrator.pipeline import STAGES

    assert ("validate", "主張の検証") in STAGES
    assert ("rhetoric", "論証と矛盾の検出") in STAGES
    keys = [k for k, _ in STAGES]
    assert keys.index("relate") < keys.index("validate") < keys.index("rhetoric")
    assert keys.index("rhetoric") < keys.index("detail")

    source = (STATIC / "app.js").read_text(encoding="utf-8")
    block = re.search(r"var STAGES = \[(.*?)\];", source, re.S)
    pairs = re.findall(r'\["(\w+)",\s*"([^"]+)"\]', block.group(1))
    assert [tuple(p) for p in pairs] == [tuple(s) for s in STAGES]


# ==================================================== M6: 論証 (cgw)


def test_epistemic_strength_is_deterministic_code() -> None:
    """0.4·根拠の数 + 0.4·確信の平均 + 0.2·warrant (§6)。"""
    from cc_orchestrator.analysis import epistemic_strength

    strong = epistemic_strength(
        [{"confidence": 0.9}, {"confidence": 0.9}, {"confidence": 0.9}], "だから")
    assert strong == {"score": 0.96, "level": "strong"}

    # 根拠 3 本で頭打ち — 数を増やしても score は上がらない
    assert epistemic_strength([{"confidence": 0.9}] * 5, "だから")["score"] == 0.96

    moderate = epistemic_strength([{"confidence": 0.5}], "だから")
    assert moderate["score"] == round(0.4 / 3 + 0.2 + 0.2, 4)
    assert moderate["level"] == "moderate"

    weak = epistemic_strength([{"confidence": 0.5}, {"confidence": 0.5}], "")
    assert weak["level"] == "weak"
    assert epistemic_strength([], "") == {"score": 0.0, "level": "speculative"}
    assert epistemic_strength([], "warrant だけ")["level"] == "speculative"


def test_cgw_targets_validated_claims_only() -> None:
    """論証は validated な主張だけに組む (§6)。"""
    from cc_orchestrator.analysis import AnalysisReport, run_cgw, validated_claims

    claims = [claim_of("np:a", "通った主張", spans=["d1#0000#aa"], status="validated"),
              claim_of("np:b", "怪しい主張", spans=["d1#0001#bb"], status="uncertain"),
              claim_of("np:c", "落ちた主張", spans=["d1#0002#cc"], status="rejected"),
              claim_of("np:d", "未検証の主張", spans=["d1#0003#dd"])]
    assert [c["nanopub_id"] for c in validated_claims(claims)] == ["np:a"]

    zones = [{"sentence_id": f"d1#000{i}#{c * 2}", "text": f"文{i}"}
             for i, c in enumerate("abcd")]
    seen: list[dict] = []

    def run(prompt: str) -> str:
        payload = json.loads(prompt[prompt.index("{"):])
        seen.append(payload)
        return json.dumps({"arguments": [
            {"claim_id": c["claim_id"],
             "grounds": [{"span_ref": s["sentence_id"], "text": s["text"],
                          "confidence": 0.8} for s in payload["sentences"][:3]],
             "warrant": "根拠が主張の条件を満たす"} for c in payload["claims"]]})

    report = AnalysisReport()
    arguments = run_cgw(run, claims, zones, report=report)
    assert len(seen) == 1 and [c["claim_id"] for c in seen[0]["claims"]] == ["np:a"[-5:]]
    assert len(arguments) == 1
    assert arguments[0]["claim_ref"] == "np:a" and arguments[0]["argument_id"] == "arg-001"
    # 根拠 2 本 (0.8) + warrant あり -> 0.4·(2/3) + 0.4·0.8 + 0.2
    assert arguments[0]["epistemic_strength"] == {"score": 0.7867, "level": "strong"}
    assert report.llm_calls == 1


def test_cgw_repairs_phantom_references() -> None:
    """入力に無い claim_id / span_ref は捨てる。text は入力から引き直す。"""
    from cc_orchestrator.analysis import AnalysisReport, repair_arguments

    claims = [claim_of("np:a", "主張A", status="validated")]
    sentences = [{"sentence_id": "d1#0000#aa", "text": "原文のままの文"}]
    report = AnalysisReport()
    out = repair_arguments({"arguments": [
        {"claim_id": "np:a"[-5:], "warrant": "  ",
         "grounds": [{"span_ref": "d1#0000#aa", "text": "LLM が書き換えた文",
                      "confidence": 5},
                     {"span_ref": "d1#0000#aa", "confidence": 0.5},   # 重複
                     {"span_ref": "存在しない文", "confidence": 0.9}]},
        {"claim_id": "np:a"[-5:], "grounds": []},                     # 重複した論証
        {"claim_id": "cl-999", "grounds": []},                        # 幻の主張
        "文字列で返ってきた",
    ]}, claims, sentences, report)

    assert len(out) == 1
    assert out[0]["grounds"] == [{"span_ref": "d1#0000#aa", "text": "原文のままの文",
                                  "confidence": 1.0}]
    assert out[0]["warrant"] == ""
    # 根拠 1 本・warrant 無し -> 0.4·(1/3) + 0.4·1.0 + 0 = 0.5333 (strong にはならない)
    assert out[0]["epistemic_strength"] == {"score": 0.5333, "level": "moderate"}
    assert set(report.repairs) == {"cgw: 入力に無い span_ref を破棄",
                                   "cgw: 同じ主張への重複した論証を破棄",
                                   "cgw: 入力に無い claim_id を破棄",
                                   "cgw: 未知の要素を破棄"}


def test_cgw_without_validated_claims_makes_no_call() -> None:
    """validated が 0 件なら LLM を呼ばない (無駄な call を出さない)。"""
    from cc_orchestrator.analysis import AnalysisReport, run_cgw

    def boom(prompt: str) -> str:
        raise AssertionError("呼ばれてはいけない")

    report = AnalysisReport()
    assert run_cgw(boom, [claim_of("np:a", "未検証")], [], report=report) == []
    assert report.llm_calls == 0 and report.notes


# ==================================================== M6: 内部矛盾 (refutes)


def test_refutes_candidate_pairs_are_filtered_deterministically() -> None:
    """概念の共有 かつ (極性の反転 or 対になる語) — ここは LLM を使わない (§6)。"""
    from cc_orchestrator.analysis import refutes_candidates

    a = claim_of("np:a", "処理は精度を改善する", concepts=["処理", "精度"])
    b = claim_of("np:b", "処理は精度を改善しない", concepts=["処理", "精度"])
    c = claim_of("np:c", "別系統は速度を改善する", concepts=["速度"])
    d = claim_of("np:d", "精度が増加する", concepts=["精度"])
    e = claim_of("np:e", "精度が減少する", concepts=["精度"])

    pairs = refutes_candidates([a, b, c, d, e])
    ids = [(x["nanopub_id"], y["nanopub_id"]) for x, y in pairs]
    assert ("np:a", "np:b") in ids            # 極性の反転
    assert ("np:d", "np:e") in ids            # 増加 <-> 減少 の対
    assert ("np:a", "np:d") not in ids        # 概念は共有するが対立の手がかりなし
    assert all("np:c" not in pair for pair in ids)   # 共有する概念が無い

    # 決定的 — 同じ入力なら同じ順序で同じペア
    assert refutes_candidates([a, b, c, d, e]) == pairs


def test_refutes_skips_rejected_claims_and_honours_the_cap(monkeypatch) -> None:
    """rejected は対象外。CC_REFUTES_MAX_PAIRS で頭打ちにする。"""
    from cc_orchestrator.analysis import AnalysisReport, refutes_candidates

    rejected = claim_of("np:x", "精度は増加しない", concepts=["精度"],
                        status="rejected")
    positive = claim_of("np:y", "精度は増加する", concepts=["精度"])
    assert refutes_candidates([rejected, positive]) == []

    monkeypatch.setenv("CC_REFUTES_MAX_PAIRS", "1")
    many = [claim_of(f"np:{i}", "精度は増加する" if i % 2 else "精度は増加しない",
                     concepts=["精度"]) for i in range(4)]
    report = AnalysisReport()
    assert len(refutes_candidates(many, report=report)) == 1
    assert any("CC_REFUTES_MAX_PAIRS" in n for n in report.notes)


def test_refutes_response_must_line_up_with_the_pairs() -> None:
    """個数がずれた応答は丸ごと捨てる (1 つずれると無関係な対に矛盾が付く)。"""
    from cc_orchestrator.analysis import AnalysisReport, repair_refutes

    a = claim_of("np:a", "改善する", concepts=["x"])
    b = claim_of("np:b", "改善しない", concepts=["x"])
    report = AnalysisReport()
    assert repair_refutes({"results": []}, [(a, b)], report) == []
    assert report.repairs

    out = repair_refutes({"results": [{"verdict": "REFUTES", "confidence": 1.5,
                                       "rationale": "逆の結論"}]}, [(a, b)], report)
    assert out == [{"pair": ["np:a", "np:b"], "verdict": "refutes",
                    "confidence": 1.0, "rationale": "逆の結論"}]
    unknown = repair_refutes({"results": [{"verdict": "たぶん矛盾"}]}, [(a, b)], report)
    assert unknown[0]["verdict"] == "none"      # 未知の verdict は none へ倒す


# ==================================================== M6: ⚡ の点灯


def _refutes_kg() -> dict:
    return {"nodes": [{"id": "c001", "label": "議論", "community_id": "comm_1"},
                      {"id": "c002", "label": "矛盾点", "community_id": "comm_1"},
                      {"id": "c003", "label": "無関係", "community_id": "comm_1"}],
            "edges": [{"id": "r001", "from": "c001", "to": "c002", "glyph": "tension",
                       "label": "対立候補",
                       "causal_check": {"verifier_verdict": "skipped",
                                        "demoted_from": "zigzag",
                                        "reason": "矛盾判定は L8 (R2) で行う。"
                                                  "R1 では候補として非断定表示"}},
                      {"id": "r002", "from": "c002", "to": "c003", "glyph": "wave"}]}


def test_refutes_lights_the_zigzag_on_the_matching_edge() -> None:
    """成立した矛盾は層 D へ刻まれ、投影規則③ で ⚡ になる。"""
    kg = _refutes_kg()
    claims = [claim_of("np:a", "議論から矛盾点が増える", concepts=["議論", "矛盾点"]),
              claim_of("np:b", "議論から矛盾点は増えない", concepts=["議論", "矛盾点"])]
    stats = stamp_refutes(kg, [{"pair": ["np:a", "np:b"], "verdict": "refutes",
                                "confidence": 0.9, "rationale": "逆の結論"}], claims)

    assert stats == {"pairs": 1, "edges_stamped": 1, "unmatched": 0,
                     "kept_tension": 0, "restored_from_tension": 1}
    edge = kg["edges"][0]
    assert edge["layer_tags"]["layer_D"] == ["refutes"]
    assert project_glyph(edge) == "zigzag"
    # R1 の「非断定へ降格」の記録は畳む (⚡ が点いた説明と食い違わせない)
    assert "demoted_from" not in edge["causal_check"]
    assert "rhetoric" in edge["causal_check"]["reason"]
    # 無関係なエッジには触らない
    assert "layer_tags" not in kg["edges"][1]

    apply_meta(kg, extractor_model="gpt-5.6-sol")
    assert kg["edges"][0]["glyph"] == "zigzag" and kg["edges"][1]["glyph"] == "wave"


def test_refutes_without_a_matching_edge_creates_nothing() -> None:
    """対応するエッジが無ければサイドカーの記録だけ (エッジは作らない)。"""
    kg = _refutes_kg()
    claims = [claim_of("np:a", "別件が増える", concepts=["無関係"]),
              claim_of("np:b", "別件は増えない", concepts=["無関係"])]
    stats = stamp_refutes(kg, [{"pair": ["np:a", "np:b"], "verdict": "refutes"}],
                          claims)
    assert stats["unmatched"] == 1 and stats["edges_stamped"] == 0
    assert len(kg["edges"]) == 2 and all("layer_tags" not in e for e in kg["edges"])


def test_refutes_matches_through_claim_refs() -> None:
    """両方の主張が既に紐づいているエッジも対応とみなす (規則1)。"""
    kg = _refutes_kg()
    kg["edges"][1]["claim_refs"] = ["np:a", "np:b"]
    claims = [claim_of("np:a", "増える", concepts=[]), claim_of("np:b", "増えない")]
    stats = stamp_refutes(kg, [{"pair": ["np:a", "np:b"], "verdict": "refutes"}],
                          claims)
    assert stats["edges_stamped"] == 1
    assert kg["edges"][1]["layer_tags"]["layer_D"] == ["refutes"]


def test_disagrees_is_recorded_but_does_not_weaken_the_display() -> None:
    """disagrees は層 D に残すが ⚡ にはしない。tension の灰破線も弱めない。"""
    kg = _refutes_kg()
    claims = [claim_of("np:a", "議論から矛盾点が増える", concepts=["議論", "矛盾点"]),
              claim_of("np:b", "議論から矛盾点は増えない", concepts=["議論", "矛盾点"])]
    stats = stamp_refutes(kg, [{"pair": ["np:a", "np:b"], "verdict": "disagrees"}],
                          claims)
    assert stats["kept_tension"] == 1 and stats["edges_stamped"] == 0
    assert "layer_tags" not in kg["edges"][0]          # 非断定の表示のまま
    assert project_glyph(kg["edges"][0]) == "tension"

    # tension でないエッジには disagrees_with を刻む (内部 30 種を失わない)
    kg["edges"][1]["claim_refs"] = ["np:a", "np:b"]
    stamp_refutes(kg, [{"pair": ["np:a", "np:b"], "verdict": "disagrees"}], claims)
    assert kg["edges"][1]["layer_tags"]["layer_D"] == ["disagrees_with"]
    assert project_glyph(kg["edges"][1]) == "wave"     # UI の記号は増やさない


def test_verdict_none_stamps_nothing() -> None:
    """「調べたが矛盾しなかった」は層タグにしない。"""
    kg = _refutes_kg()
    claims = [claim_of("np:a", "増える", concepts=["議論", "矛盾点"]),
              claim_of("np:b", "増えない", concepts=["議論", "矛盾点"])]
    stats = stamp_refutes(kg, [{"pair": ["np:a", "np:b"], "verdict": "none"}], claims)
    assert stats["pairs"] == 0 and all("layer_tags" not in e for e in kg["edges"])


# ==================================================== M5+M6: モック E2E


CONTRADICTORY_CLAIMS = [
    {"claim_text": "Teams議論から矛盾点が増加する", "is_underspecified": False,
     "related_concepts": ["Teams議論", "矛盾点"]},
    {"claim_text": "Teams議論から矛盾点は増加しない", "is_underspecified": False,
     "related_concepts": ["Teams議論", "矛盾点"]},
]


def test_mock_e2e_validates_claims_logs_rejections_and_lights_zigzag(
        mock_run) -> None:                # noqa: F811 — pytest fixture
    """layers=True の 1 周で M5+M6 の成果が全部出る (§12 の受け入れ基準 3)。"""
    agent = FakeAnalysisAgent(claims=CONTRADICTORY_CLAIMS)
    summary, kg, stages, _ = mock_run(agent, FakeVerificationAgent())

    # --- 進捗の 2 段が実際に発火する ---
    assert stages.index("relate") < stages.index("validate") < stages.index("rhetoric")

    # --- ⑤validate: 主張に validation が付く ---
    assert summary["validation"]["status"] == "done"
    assert set(summary["validation"]["verifier_ids"]) == {
        "llm-nli:terra", "llm-verifier:terra", "ontology-rules"}
    doc = json.loads(Path(summary["layers"]["saved"]).read_text(encoding="utf-8"))
    assert len(doc["claims"]) == 2
    for claim in doc["claims"]:
        validation = claim["validation"]
        assert set(validation) == {"status", "combined", "scores",
                                   "requires_human_review"}
        assert validation["status"] == "validated" and validation["combined"] == 0.95
        assert validation["scores"] == {"nli": 0.95, "llm": 0.95}
    assert doc["stats"]["validated"] == 2 and doc["stats"]["claims"] == 2

    # --- ⑥rhetoric: 論証と矛盾がサイドカーに入る ---
    assert summary["rhetoric"]["status"] == "done"
    assert "cgw" in agent.tasks and "refutes" in agent.tasks
    argument = doc["arguments"][0]
    assert set(argument) == {"argument_id", "claim_ref", "grounds", "warrant",
                             "epistemic_strength"}
    assert argument["epistemic_strength"]["level"] in ("strong", "moderate")
    assert doc["refutes"][0]["verdict"] == "refutes"
    assert doc["refutes"][0]["pair"] == [c["nanopub_id"] for c in doc["claims"]]
    assert doc["stats"]["refutes"] == 1 and doc["stats"]["arguments"] == 2

    # --- ⚡ が点灯する (投影規則③) ---
    r013 = next(e for e in kg["edges"] if e["id"] == "r013")
    assert r013["glyph"] == "zigzag" and r013["layer_tags"]["layer_D"] == ["refutes"]
    assert summary["rhetoric"]["stamped"]["edges_stamped"] == 1

    # --- 検証済みの記録が provenance と KG に残る ---
    assert set(r013["provenance"]["validator_ids"]) >= {"llm-nli:terra",
                                                        "ontology-rules"}
    validated_edges = [e for e in kg["edges"] if "validation" in e]
    assert validated_edges and all(e["validation"]["status"] == "validated"
                                   for e in validated_edges)
    assert doc["stats"]["llm_calls"] <= 30          # 受け入れ基準 5


def test_mock_e2e_writes_the_rejection_log(mock_run) -> None:      # noqa: F811
    """検証器が否定すれば rejection_log が出て、因果は矢印にならない。"""
    agent = FakeAnalysisAgent(claims=CONTRADICTORY_CLAIMS)
    summary, kg, _, _ = mock_run(
        agent, FakeVerificationAgent(label="contradicts", supported=False))

    assert summary["validation"]["rejections"] > 0
    log = Path(summary["validation"]["rejection_log"])
    assert log.name == f"rejections_{summary['session']}.jsonl"
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert {r["kind"] for r in rows} == {"claim", "causal_edge"}
    assert all(r["combined"] < 0.5 and r["session"] == summary["session"]
               for r in rows)

    # rejected な主張はサイドカーに残るが、地図からの参照は外れる
    doc = json.loads(Path(summary["layers"]["saved"]).read_text(encoding="utf-8"))
    assert all(c["validation"]["status"] == "rejected" for c in doc["claims"])
    assert doc["stats"]["rejected"] == 2 and doc["stats"]["validated"] == 0
    assert all(not e.get("claim_refs") for e in kg["edges"])
    # 検証を通らなかった causes 候補は相関のまま (エッジは消さない)
    assert all(e["glyph"] != "arrow" for e in kg["edges"] if "validation" in e)
    # validated が 0 件なので論証は組まれない
    assert doc["arguments"] == []


def test_offline_skips_validation_but_finishes(tmp_path, monkeypatch) -> None:
    """offline は検証器 (別モデル) を呼べない。それでも全段完走する (§9)。"""
    monkeypatch.chdir(tmp_path)
    from cc_orchestrator.pipeline import run_pipeline

    stages: list[str] = []
    summary = run_pipeline(
        "今週の研究を概念地図として整理して", target="file",
        kg_file=str(PRODUCTION / "tests" / "fixtures" / "kg_sample.json"),
        offline=True, verify_causal=False, export_svg=False,
        progress=lambda key, label: stages.append(key))

    assert summary["validation"] == {"status": "disabled"}
    assert summary["rhetoric"] == {"status": "disabled"}
    assert "validate" in stages and "rhetoric" in stages      # 進捗は必ず出す
    assert not list(tmp_path.glob("logs/rejections/*.jsonl"))
