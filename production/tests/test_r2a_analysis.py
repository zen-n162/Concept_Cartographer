"""R2a M3+M4 — 文分割・ゾーニング・主張抽出・層タグの刻印 (設計書 §11)。

主眼は 4 つ:
  - **文分割が決定的**であること (§5)。ここがぶれると sentence_id が run ごとに
    変わり、layers サイドカーと kg の突合が壊れる
  - **LLM 出力を必ず修復してから使う** (§6)。未知ラベル・幻の sentence_id・
    範囲外の confidence を通さない
  - **層タグを刻んでも記号は動かない** (§8(1))。LLM が何も足さなければ
    layers=True でも R1.5 と同じ地図になる
  - claims / L5 が ◇◧ として実際に投影されること (§4 の規則⑦⑧)

各テストは tmp_path を作業ディレクトリにするので production/ を汚さない。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cc_core import layers_store
from cc_core.detail import build_multilevel_plan, project
from cc_core.layer_assign import (
    UNTAGGED_GLYPHS,
    ZONE_TO_LAYER_B,
    assign_layer_tags,
)
from cc_core.layers import apply_meta
from cc_core.sentences import (
    MAX_SENTENCE_CHARS,
    SPLITTER_VERSION,
    SentenceSpan,
    split_sentences,
)
from cc_core.validate import validate_layout_plan
from cc_orchestrator import analysis

PRODUCTION = Path(__file__).resolve().parents[1]
STATIC = PRODUCTION / "src" / "cc_web" / "static"
KG_FIXTURE = PRODUCTION / "tests" / "fixtures" / "kg_sample.json"


# --------------------------------------------------------------- 補助


def spans(*texts: str) -> list[SentenceSpan]:
    """突合用の SentenceSpan を素朴に作る (分割器を経由しない)。"""
    out = []
    cursor = 0
    for i, text in enumerate(texts):
        out.append(SentenceSpan(sentence_id=f"d1#{i:04d}#deadbeef", text=text,
                                char_start=cursor, char_end=cursor + len(text),
                                document_id="d1"))
        cursor += len(text)
    return out


def kg_two_nodes(glyph: str = "wave", **edge: object) -> dict:
    return {
        "graph_version": "kg_test",
        "nodes": [{"id": "c001", "label": "概念A", "community_id": "comm_001"},
                  {"id": "c002", "label": "概念B", "community_id": "comm_001"}],
        "edges": [{"id": "r001", "from": "c001", "to": "c002", "label": "関係",
                   "glyph": glyph, **edge}],
        "communities": [{"id": "comm_001", "name": "テーマ", "is_gap": False}],
    }


def zone_of(sentence_id: str, text: str, label: str) -> dict:
    return {"sentence_id": sentence_id, "text": text, "zone_label": label,
            "zone_system": "CoreSC", "confidence": 0.9,
            "document_id": "d1", "char_start": 0, "char_end": len(text)}


# ======================================================= M3: 文分割 (§5)


def test_splits_on_japanese_period() -> None:
    result = split_sentences("第一文です。第二文です。", "d1")
    assert [s.text for s in result] == ["第一文です。", "第二文です。"]


def test_splits_on_exclamation_and_question_marks() -> None:
    """終端記号が連続する場合はまとめて 1 文の末尾にする。"""
    result = split_sentences("本当ですか!?はい！そうです？", "d1")
    assert [s.text for s in result] == ["本当ですか!?", "はい！", "そうです？"]


def test_blank_line_splits_but_single_newline_does_not() -> None:
    """段落境界は**連続**改行。箇条書きの折り返しで切らないため。"""
    result = split_sentences("一行目\n二行目\n\n次の段落", "d1")
    assert [s.text for s in result] == ["一行目\n二行目", "次の段落"]


@pytest.mark.parametrize("text,expected", [
    ("彼は「これは本当か。」と述べた。", 1),
    ("引用は『第一。第二。』である。", 1),
    ("補足（詳細は後述。）を参照。", 1),
    ("note (see below. ) here.", 1),
])
def test_terminators_inside_brackets_do_not_split(text: str, expected: int) -> None:
    """「」『』（）() の内側では切らない (深さカウンタ / §5)。"""
    assert len(split_sentences(text, "d1")) == expected


def test_unclosed_bracket_is_rescued_by_the_length_guard() -> None:
    """閉じ忘れで 1 文が資料 1 本ぶんに膨らんでも 500 字で強制的に切る。"""
    result = split_sentences("「" + "あ" * 1200, "d1")
    assert len(result) == 3
    assert [len(s.text) for s in result] == [MAX_SENTENCE_CHARS, MAX_SENTENCE_CHARS, 201]


def test_forced_split_at_500_chars() -> None:
    result = split_sentences("あ" * 1200, "d1")
    assert all(len(s.text) <= MAX_SENTENCE_CHARS for s in result)
    assert "".join(s.text for s in result) == "あ" * 1200


def test_whitespace_only_segments_are_dropped() -> None:
    result = split_sentences("最初の文。\n\n   \n\n   \n\n最後の文。", "d1")
    assert [s.text for s in result] == ["最初の文。", "最後の文。"]


def test_offsets_point_back_into_the_source() -> None:
    """`text == source[char_start:char_end]` — 後段が offset だけで原文へ戻れる。"""
    source = "  最初の文。 \n\n  次の文です！  "
    for span in split_sentences(source, "d1"):
        assert source[span.char_start:span.char_end] == span.text


def test_sentence_id_is_stable_and_position_aware() -> None:
    """同じ内容なら同じハッシュ / 位置は idx で表す (§5)。"""
    a = split_sentences("第一文です。第二文です。", "d1")
    b = split_sentences("第一文です。第二文です。", "d1")
    assert [s.sentence_id for s in a] == [s.sentence_id for s in b]
    assert re.fullmatch(r"d1#0000#[0-9a-f]{8}", a[0].sentence_id)
    # 文書 ID が違えば ID も違う / 内容が違えばハッシュが違う
    assert split_sentences("第一文です。", "d2")[0].sentence_id != a[0].sentence_id
    assert a[0].sentence_id.split("#")[2] != a[1].sentence_id.split("#")[2]
    # 同じ文が別の位置にあってもハッシュ部分は変わらない (移動を追える)
    moved = split_sentences("先頭を足した。第一文です。", "d1")
    assert moved[1].sentence_id.split("#")[2] == a[0].sentence_id.split("#")[2]


def test_empty_input_is_not_an_error() -> None:
    assert split_sentences("", "d1") == []
    assert split_sentences("   \n\n  ", "d1") == []
    assert split_sentences(None, "d1") == []


# ============================================== M3: zone 出力の修復 (§6)


def test_zone_repair_keeps_only_known_labels() -> None:
    report = analysis.AnalysisReport()
    batch = spans("結果は改善した。", "手法は次のとおり。")
    zones = analysis.repair_zone_labels({"labels": [
        {"sentence_id": batch[0].sentence_id, "zone_label": "Result", "confidence": 0.8},
        {"sentence_id": batch[1].sentence_id, "zone_label": "Teleportation"},
    ]}, batch, report)
    assert [z["zone_label"] for z in zones] == ["Result"]
    assert any("Teleportation" in key for key in report.repairs)


def test_zone_repair_drops_hallucinated_sentence_ids() -> None:
    """入力に無い文には**絶対にラベルを付けない** (幻の文を作らせない)。"""
    report = analysis.AnalysisReport()
    batch = spans("結果は改善した。")
    zones = analysis.repair_zone_labels({"labels": [
        {"sentence_id": "d9#0099#ffffffff", "zone_label": "Result"},
        {"sentence_id": batch[0].sentence_id, "zone_label": "Result"},
    ]}, batch, report)
    assert len(zones) == 1 and zones[0]["sentence_id"] == batch[0].sentence_id
    assert any("sentence_id" in key for key in report.repairs)


def test_zone_repair_clamps_confidence_and_uses_input_text() -> None:
    """confidence は 0〜1 へ / text と offset は**入力から引き直す**。"""
    report = analysis.AnalysisReport()
    batch = spans("結果は改善した。")
    zones = analysis.repair_zone_labels({"labels": [
        {"sentence_id": batch[0].sentence_id, "zone_label": "result",
         "confidence": 4.2, "text": "LLM が書き換えた文"},
    ]}, batch, report)
    assert zones[0]["confidence"] == 1.0
    assert zones[0]["text"] == "結果は改善した。"
    assert zones[0]["zone_label"] == "Result"          # 大小文字は吸収する
    assert zones[0]["char_start"] == 0 and zones[0]["document_id"] == "d1"

    zones = analysis.repair_zone_labels({"labels": [
        {"sentence_id": batch[0].sentence_id, "zone_label": "Result",
         "confidence": "とても高い"}]}, batch, report)
    assert zones[0]["confidence"] == analysis.DEFAULT_CONFIDENCE


def test_zone_repair_accepts_az_vocabulary() -> None:
    report = analysis.AnalysisReport()
    batch = spans("先行研究では…")
    zones = analysis.repair_zone_labels(
        {"labels": [{"sentence_id": batch[0].sentence_id, "zone_label": "AIM"}]},
        batch, report)
    assert zones[0]["zone_label"] == "AIM" and zones[0]["zone_system"] == "AZ"


def test_zone_batches_by_the_knob(monkeypatch) -> None:
    """50 文/call でバッチ処理し、上限で頭打ちにする (§6 / 受け入れ基準 5)。"""
    monkeypatch.setenv("CC_ZONE_BATCH", "2")
    sizes: list[int] = []

    def fake_run(prompt: str) -> str:
        payload = json.loads(prompt[prompt.index("{"):])
        sizes.append(len(payload["sentences"]))
        return json.dumps({"labels": [
            {"sentence_id": s["sentence_id"], "zone_label": "Result"}
            for s in payload["sentences"]]})

    report = analysis.AnalysisReport()
    zones = analysis.run_zone(fake_run, spans(*[f"文{i}。" for i in range(5)]),
                              report=report)
    assert sizes == [2, 2, 1] and report.llm_calls == 3 and len(zones) == 5


def test_zone_batch_failure_does_not_sink_the_run() -> None:
    """1 バッチが失敗しても他は続ける (資料の一部が無ラベルでも地図は作れる)。"""
    calls = {"n": 0}

    def flaky(prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("responses -> 429")
        payload = json.loads(prompt[prompt.index("{"):])
        return json.dumps({"labels": [{"sentence_id": s["sentence_id"],
                                       "zone_label": "Result"}
                                      for s in payload["sentences"]]})

    report = analysis.AnalysisReport()
    zones = analysis.run_zone(flaky, spans("文1。", "文2。"), report=report,
                              batch_size=1)
    assert len(zones) == 1
    assert report.errors == ["zone: RuntimeError"]


def test_sentence_cap_is_enforced(monkeypatch) -> None:
    monkeypatch.setenv("CC_ZONE_MAX_SENTENCES", "3")
    report = analysis.AnalysisReport()
    docs = [{"name": "d1.txt", "text": "".join(f"文{i}です。" for i in range(10))}]
    sentences = analysis.collect_sentences(docs, {}, report=report)
    assert len(sentences) == 3
    assert any("CC_ZONE_MAX_SENTENCES" in note for note in report.notes)


def test_evidence_spans_become_pseudo_sentences_with_a_note() -> None:
    """Work IQ 経由で本文が手元に無い資料は根拠スパンを疑似文にする (§9)。

    制約 (char offset が原文基準でない) は必ず report に残す。
    """
    kg = json.loads(KG_FIXTURE.read_text(encoding="utf-8"))
    report = analysis.AnalysisReport()
    sentences = analysis.collect_sentences([], kg, report=report)
    assert sentences and report.sentence_source == "evidence_span"
    assert any("疑似文" in note for note in report.notes)
    assert all(s.document_id.endswith(".docx") for s in sentences)

    report = analysis.AnalysisReport()
    analysis.collect_sentences([{"name": "local.txt", "text": "手元の文。"}], kg,
                               report=report)
    assert report.sentence_source == "mixed"


# ================================================ M3: layers_store (§3.2)


def test_layers_store_round_trip(tmp_path) -> None:
    doc = layers_store.new_document("20260807_101112")
    doc["zones"] = [zone_of("d1#0000#aaaaaaaa", "結果は改善した。", "Result")]
    doc["stats"] = layers_store.compute_stats(doc, sentences=1, llm_calls=1)
    path = layers_store.save("20260807_101112", doc, graphs_dir=tmp_path)

    assert path.name == "layers_session_20260807_101112.json"
    loaded = layers_store.load("20260807_101112", graphs_dir=tmp_path)
    assert loaded == json.loads(path.read_text(encoding="utf-8"))
    assert loaded["version"] == 1 and loaded["splitter"] == SPLITTER_VERSION
    assert loaded["zones"][0]["zone_label"] == "Result"
    assert loaded["stats"] == {"sentences": 1, "zoned": 1, "claims": 0,
                               "validated": 0, "llm_calls": 1}
    # 空でも §3.2 のキーは全部ある (読む側が .get で場合分けしなくて済む)
    assert set(layers_store.DOCUMENT_KEYS) <= set(loaded)


def test_layers_store_rejects_path_traversal(tmp_path) -> None:
    with pytest.raises(layers_store.InvalidSession):
        layers_store.path("../../etc/passwd", graphs_dir=tmp_path)
    assert layers_store.exists("../secrets", graphs_dir=tmp_path) is False


def test_layers_store_survives_a_broken_sidecar(tmp_path) -> None:
    """層が読めなくても地図の再描画は続けられるべきなので例外にしない。"""
    (tmp_path / "layers_session_s1.json").write_text("{壊れた", encoding="utf-8")
    doc = layers_store.load("s1", graphs_dir=tmp_path)
    assert doc["zones"] == [] and doc["claims"] == []


def test_session_of_kg_file_follows_the_naming_rule() -> None:
    assert layers_store.session_of_kg_file(
        "graphs/kg_session_20260807_101112.json") == "20260807_101112"
    assert layers_store.session_of_kg_file("tests/fixtures/kg_sample.json") is None
    assert layers_store.session_of_kg_file(None) is None


def test_nanopub_id_is_server_side_and_order_independent() -> None:
    """同じ主張・同じ根拠なら run をまたいで同じ ID (§6)。"""
    a = layers_store.nanopub_id("結果は改善した", ["d1#0001#aa", "d1#0002#bb"])
    b = layers_store.nanopub_id("結果は改善した", ["d1#0001#aa", "d1#0002#bb"])
    assert a == b and a.startswith("np:") and len(a) == 3 + 16
    assert a != layers_store.nanopub_id("結果は悪化した", ["d1#0001#aa", "d1#0002#bb"])


# ============================================ M3: STAGES と app.js の同期


def test_pipeline_stages_match_app_js() -> None:
    """進捗の並びは pipeline と app.js で 1:1 (§9 / 同一コミットで同期)。"""
    from cc_orchestrator.pipeline import STAGES

    source = (STATIC / "app.js").read_text(encoding="utf-8")
    block = re.search(r"var STAGES = \[(.*?)\];", source, re.S)
    assert block, "app.js の STAGES 配列が見つからない"
    pairs = re.findall(r'\["(\w+)",\s*"([^"]+)"\]', block.group(1))
    assert [tuple(p) for p in pairs] == [tuple(s) for s in STAGES]
    assert ("zone", "文脈ラベル付け") in STAGES
    assert ("claims", "主張の抽出") in STAGES


def test_analysis_agent_is_registered_for_ensure_agents() -> None:
    """ensure_agents が回す辞書に居ないと Foundry に作られない。"""
    from cc_orchestrator.agents_def import AGENT_SPECS, MODELS

    spec = AGENT_SPECS["cc-analysis"]
    assert spec["model"] == MODELS["analysis"] == "gpt-5.6-sol"
    assert spec["tools"] == [] and spec["effort"] == "medium"
    for task in ("zone", "claims", "cgw", "refutes"):
        assert f'"{task}"' in spec["instructions"]


# ==================================================== M4: claims の修復 (§6)


def test_claims_target_only_result_conclusion_hypothesis_observation() -> None:
    """手順や背景の文から主張を作らせない (§6)。"""
    seen: list[list[str]] = []

    def fake_run(prompt: str) -> str:
        payload = json.loads(prompt[prompt.index("{"):])
        seen.append([s["text"] for s in payload["sentences"]])
        return json.dumps({"claims": []})

    zones = [zone_of("d1#0000#aa", "手順は次のとおり。", "Method"),
             zone_of("d1#0001#bb", "結果は改善した。", "Result"),
             zone_of("d1#0002#cc", "背景はこうだ。", "Background"),
             zone_of("d1#0003#dd", "以上より有効である。", "Conclusion")]
    report = analysis.AnalysisReport()
    analysis.run_claims(fake_run, zones, kg_two_nodes(), report=report)
    assert seen == [["結果は改善した。", "以上より有効である。"]]


def test_claims_repair_matches_concepts_and_drops_unsourced() -> None:
    report = analysis.AnalysisReport()
    batch = [zone_of("d1#0001#bb", "概念Aは概念Bを改善した。", "Result")]
    concepts = {"概念a": "概念A", "概念b": "概念B"}
    claims = analysis.repair_claims({"claims": [
        {"claim_text": "概念Aは概念Bを改善する",
         "source_sentence_ids": ["d1#0001#bb"],
         "related_concepts": ["概念A", "存在しない概念"]},
        {"claim_text": "根拠の無い主張", "source_sentence_ids": ["d9#9999#zz"]},
        {"claim_text": "   ", "source_sentence_ids": ["d1#0001#bb"]},
    ]}, batch, concepts, report, timestamp="2026-08-07T00:00:00")

    assert len(claims) == 1
    assertion = claims[0]["assertion"]
    assert assertion["related_concepts"] == ["概念A"]      # 実在ノードだけ残す
    assert assertion["is_underspecified"] is False
    assert claims[0]["provenance"] == {
        "source_span": ["d1#0001#bb"], "extractor_id": "cc-analysis",
        "extraction_timestamp": "2026-08-07T00:00:00",
        "extraction_method": "llm-fewshot"}
    assert claims[0]["pub_info"]["document_id"] == "d1"
    # ⑤validate が走っていない run では検証結果の枠を作らない
    assert "validation" not in claims[0]
    assert any("根拠文" in key for key in report.repairs)


def test_claims_are_capped_and_numbered(monkeypatch) -> None:
    monkeypatch.setenv("CC_CLAIMS_MAX", "2")

    def fake_run(prompt: str) -> str:
        return json.dumps({"claims": [
            {"claim_text": f"主張{i}", "source_sentence_ids": ["d1#0001#bb"]}
            for i in range(5)]})

    zones = [zone_of("d1#0001#bb", "結果は改善した。", "Result")]
    report = analysis.AnalysisReport()
    claims, _ = analysis.run_claims(fake_run, zones, kg_two_nodes(), report=report)
    assert [c["assertion"]["claim_id"] for c in claims] == ["cl-001", "cl-002"]
    assert any("上限" in note for note in report.notes)


def test_ontology_repair_keeps_only_known_classes_and_nodes() -> None:
    report = analysis.AnalysisReport()
    concepts = {"概念a": "概念A", "概念b": "概念B"}
    found = analysis.repair_ontology({
        "concepts": [{"label": "概念A", "onto_class": "Process"},
                     {"label": "概念B", "onto_class": "bfo:MaterialEntity"},
                     {"label": "概念B", "onto_class": "Wizardry"},
                     {"label": "幻の概念", "onto_class": "Process"}],
        "relations": [{"from": "概念A", "to": "概念B", "relation": "is-a"},
                      {"from": "概念A", "to": "概念A", "relation": "is_a"},
                      {"from": "概念A", "to": "幻の概念", "relation": "part_of"},
                      {"from": "概念A", "to": "概念B", "relation": "causes"}],
    }, concepts, report)

    assert found["concepts"] == [{"label": "概念A", "onto_class": "bfo:Process"},
                                 {"label": "概念B", "onto_class": "bfo:MaterialEntity"}]
    assert found["relations"] == [{"from": "概念A", "to": "概念B", "relation": "is_a"}]
    assert any("onto_class" in key for key in report.repairs)
    assert any("relation" in key for key in report.repairs)


# ================================================ M4: 層タグの刻印 (§8)


def test_glyphs_do_not_move_when_the_llm_adds_nothing() -> None:
    """**挙動不変の要**: 層タグを刻んでも記号は動かない (§8(1) が要る理由)。

    刻印は ④relate の**後**に行う契約なので、ここでも同じ順で回す。
    因果の 3 点セットを通ったエッジは corroborated として arrow のまま、
    降格されたエッジは wave のまま — どちらも投影が同じ判断を再現する。
    """
    from cc_core.causal import apply_relation_policy

    kg = json.loads(KG_FIXTURE.read_text(encoding="utf-8"))
    # 語彙証拠のある因果を 1 本足す (fixture はサニタイズ済みで 3 点セットを
    # 通る根拠を持たないため、通過した arrow が 1 本も無いと検査にならない)
    kg["edges"].append({
        "id": "r900", "from": "c001", "to": "c005", "glyph": "arrow",
        "label": "機序で引き起こす",
        "evidence_span": [{"document_id": "sample_proposal.docx",
                           "surface": "その機序により変化を引き起こす。"}]})
    kg, _ = apply_relation_policy(kg, verifier=lambda edge, text: True)
    before = [e["glyph"] for e in kg["edges"]]
    assert "arrow" in before, "3 点セットを通った因果が無いと検査にならない"

    assign_layer_tags(kg)
    apply_meta(kg, extractor_model="gpt-5.6-sol")
    assert [e["glyph"] for e in kg["edges"]] == before


def test_untagged_glyphs_stay_untagged() -> None:
    """hole / tension は層の語彙で表せない — タグを刻まず非断定表示を守る。"""
    kg = json.loads(KG_FIXTURE.read_text(encoding="utf-8"))
    zones = [zone_of("d1#0000#aa", "サンプル文書中の記述 10-1", "Result")]
    assign_layer_tags(kg, zones=zones)
    for edge in kg["edges"]:
        if edge["glyph"] in UNTAGGED_GLYPHS:
            assert "layer_tags" not in edge
        else:
            assert edge["layer_tags"]


def test_demoted_causal_edge_keeps_causes_in_layer_c() -> None:
    """④relate で降格したエッジは「裏付け不足の causes 候補」として残す。"""
    kg = kg_two_nodes("wave", causal_check={"demoted_from": "arrow",
                                            "verifier_verdict": "fail"})
    assign_layer_tags(kg)
    assert kg["edges"][0]["layer_tags"]["layer_C"] == ["causes"]
    apply_meta(kg, extractor_model="gpt-5.6-sol")
    assert kg["edges"][0]["glyph"] == "wave"          # 裏付けが無いので昇格しない


@pytest.mark.parametrize("zone_label,expected", sorted(ZONE_TO_LAYER_B.items()))
def test_zone_labels_map_to_layer_b(zone_label: str, expected: str) -> None:
    """zone → layer_B の転用は LLM を呼ばない (v4実§4.7 経路B)。"""
    kg = kg_two_nodes("wave", evidence_span=[{"document_id": "d1",
                                              "surface": "結果は改善した。"}])
    assign_layer_tags(kg, zones=[zone_of("d1#0000#aa", "結果は改善した。", zone_label)])
    assert kg["edges"][0]["layer_tags"]["layer_B"] == [expected]


def test_short_sentences_do_not_match_by_containment() -> None:
    """「はい。」のような短文はどの根拠スパンにも含まれてしまう。

    部分一致で文を同定してよいのは十分に長い側だけ — でないと無関係な
    zone ラベルが層 B に流れ込む。
    """
    kg = kg_two_nodes("wave", evidence_span=[
        {"surface": "実験の結果、はい。という応答が増えた。"}])
    assign_layer_tags(kg, zones=[zone_of("d1#0000#aa", "はい。", "Conclusion")])
    assert kg["edges"][0]["layer_tags"]["layer_B"] == []

    # 十分な長さがあれば部分一致で拾う (LLM は前後を欠いた引用を返しうる)
    kg = kg_two_nodes("wave", evidence_span=[{"surface": "以上より本手法は有効である"}])
    assign_layer_tags(kg, zones=[
        zone_of("d1#0000#aa", "以上より本手法は有効であると結論づけられる。", "Conclusion")])
    assert kg["edges"][0]["layer_tags"]["layer_B"] == ["conclusion_of"]


def test_zone_without_a_mapping_adds_no_layer_b() -> None:
    """写像表に無いラベル (Object / Model 等) は無理に当てない。"""
    kg = kg_two_nodes("wave", evidence_span=[{"surface": "対象はこれである。"}])
    assign_layer_tags(kg, zones=[zone_of("d1#0000#aa", "対象はこれである。", "Object")])
    assert kg["edges"][0]["layer_tags"]["layer_B"] == []


def test_layer_a_stamping_projects_to_isa_and_partof() -> None:
    """L5 の関係候補 → layer_A → ⑦meta の投影で ◇ / ◧ が実際に出る (§11 M4)。"""
    kg = {
        "graph_version": "kg_test",
        "nodes": [{"id": "c001", "label": "動的グラフ", "community_id": "comm_001"},
                  {"id": "c002", "label": "グラフ", "community_id": "comm_001"},
                  {"id": "c003", "label": "ノード", "community_id": "comm_001"}],
        "edges": [{"id": "r001", "from": "c001", "to": "c002", "glyph": "wave",
                   "label": "の一種"},
                  {"id": "r002", "from": "c003", "to": "c002", "glyph": "wave",
                   "label": "の構成要素"}],
        "communities": [{"id": "comm_001", "name": "テーマ", "is_gap": False}],
    }
    stats = assign_layer_tags(kg, ontology={
        "concepts": [{"label": "動的グラフ", "onto_class": "bfo:InformationEntity"}],
        "relations": [{"from": "動的グラフ", "to": "グラフ", "relation": "is_a"},
                      {"from": "ノード", "to": "グラフ", "relation": "part_of"}]})

    assert stats["layer_a_from_llm"] == 2 and stats["onto_class_set"] == 1
    assert kg["nodes"][0]["onto_class"] == "bfo:InformationEntity"
    apply_meta(kg, extractor_model="gpt-5.6-sol")
    assert kg["edges"][0]["glyph"] == "isa"          # ◇ 分類
    assert kg["edges"][1]["glyph"] == "partof"       # ◧ 構成


def test_layer_a_is_not_stamped_on_the_reverse_edge() -> None:
    """向きが違うエッジに is_a を刻むと ◇ が反対を向いて地図が嘘をつく。"""
    kg = {
        "graph_version": "kg_test",
        "nodes": [{"id": "c001", "label": "動的グラフ", "community_id": "comm_001"},
                  {"id": "c002", "label": "グラフ", "community_id": "comm_001"}],
        "edges": [{"id": "r001", "from": "c002", "to": "c001", "glyph": "wave"}],
        "communities": [{"id": "comm_001", "name": "テーマ", "is_gap": False}],
    }
    stats = assign_layer_tags(kg, ontology={"relations": [
        {"from": "動的グラフ", "to": "グラフ", "relation": "is_a"}]})
    assert kg["edges"][0]["layer_tags"]["layer_A"] == []
    assert stats["relations_unmatched"] == 1


def test_claim_refs_are_filled_on_nodes_and_edges() -> None:
    kg = kg_two_nodes("wave", evidence_span=[{"document_id": "d1",
                                              "surface": "概念Aは概念Bを改善した。"}])
    zones = [zone_of("d1#0001#bb", "概念Aは概念Bを改善した。", "Result")]
    claims = [{"nanopub_id": "np:abcdef0123456789",
               "assertion": {"claim_id": "cl-001", "claim_text": "改善する",
                             "is_underspecified": False,
                             "related_concepts": ["概念A"]},
               "provenance": {"source_span": ["d1#0001#bb"]}}]
    stats = assign_layer_tags(kg, zones=zones, claims=claims)

    assert kg["nodes"][0]["claim_refs"] == ["np:abcdef0123456789"]
    assert "claim_refs" not in kg["nodes"][1]
    assert kg["edges"][0]["claim_refs"] == ["np:abcdef0123456789"]
    assert stats["node_claim_refs"] == 1 and stats["edge_claim_refs"] == 1


def test_claim_refs_and_layer_tags_reach_the_plan() -> None:
    """運搬 (kg → plan → view)。carry リストから漏れると UI に届かない。"""
    kg = kg_two_nodes("wave", evidence_span=[{"surface": "概念Aは概念Bを改善した。"}])
    assign_layer_tags(
        kg,
        zones=[zone_of("d1#0001#bb", "概念Aは概念Bを改善した。", "Result")],
        claims=[{"nanopub_id": "np:abcdef0123456789",
                 "assertion": {"related_concepts": ["概念A"]},
                 "provenance": {"source_span": ["d1#0001#bb"]}}],
        ontology={"concepts": [{"label": "概念A", "onto_class": "bfo:Process"}]})
    apply_meta(kg, extractor_model="gpt-5.6-sol")

    view = project(build_multilevel_plan(kg, default_level="detailed"), "detailed")
    edge = next(e for e in view["edges"] if e["id"] == "r001")
    assert edge["layer_tags"]["layer_B"] == ["result_of"]
    assert edge["claim_refs"] == ["np:abcdef0123456789"]
    node = next(n for n in view["nodes"] if n["id"] == "c001")
    assert node["onto_class"] == "bfo:Process" and node["claim_refs"]
    assert validate_layout_plan(view).valid


def test_assignment_is_idempotent() -> None:
    """2 回刻んでも同じ結果 (再実行で層タグが増殖しない)。"""
    kg = json.loads(KG_FIXTURE.read_text(encoding="utf-8"))
    zones = [zone_of("d1#0000#aa", "サンプル文書中の記述 5-1", "Result")]
    assign_layer_tags(kg, zones=zones)
    once = json.dumps(kg, sort_keys=True, ensure_ascii=False)
    assign_layer_tags(kg, zones=zones)
    assert json.dumps(kg, sort_keys=True, ensure_ascii=False) == once


# ==================================================== M3/M4: パイプライン


class FakeAnalysisAgent:
    """cc-analysis の代わり。task ごとに決め打ちの JSON を返す。"""

    def __init__(self, relations: list[dict] | None = None) -> None:
        self.relations = relations or []
        self.tasks: list[str] = []

    def __call__(self, prompt: str) -> str:
        payload = json.loads(prompt[prompt.index("{"):])
        self.tasks.append(payload["task"])
        if payload["task"] == "zone":
            return json.dumps({"labels": [
                {"sentence_id": s["sentence_id"], "zone_label": "Result",
                 "zone_system": "CoreSC", "confidence": 0.86}
                for s in payload["sentences"]]}, ensure_ascii=False)
        first = payload["sentences"][0]
        return json.dumps({
            "claims": [{"claim_text": "概念地図は研究の見通しを改善する",
                        "is_underspecified": False,
                        "source_sentence_ids": [first["sentence_id"]],
                        "related_concepts": payload["concepts"][:2]}],
            "concepts": [{"label": payload["concepts"][0],
                          "onto_class": "InformationEntity"}],
            "relations": self.relations,
        }, ensure_ascii=False)


class FakeFoundry:
    """FoundryAgentsV2 の代わり (ネットワークに出ない)。"""

    def __init__(self, analysis_agent: FakeAnalysisAgent) -> None:
        self.analysis_agent = analysis_agent
        self.ensured: list[str] = []

    def ensure_agent(self, name: str, *args: object, **kwargs: object) -> str:
        self.ensured.append(name)
        return name

    def run(self, agent: str, prompt: str, tool_executor: object = None,
            **kwargs: object) -> str:
        if agent == "cc-analysis":
            return self.analysis_agent(prompt)
        if agent == "cc-projection":
            return json.dumps({"status": "RENDER_OK", "created": 42})
        if agent == "cc-verification":
            return json.dumps({"verdict": "PASS", "summary": "一致"})
        raise AssertionError(f"予期しないエージェント呼び出し: {agent}")


class FakeExecutor:
    """描画・書き出しの実行系 (ローカル canvas を叩かない)。"""

    def __init__(self, target: str = "local") -> None:
        self.target = target

    def __call__(self, name: str, args: dict) -> dict:
        return {"success": True, "created": [], "mode": self.target}

    def export_excalidraw(self, out_path: str) -> str:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text("{}", encoding="utf-8")
        return out_path


@pytest.fixture
def mock_run(tmp_path, monkeypatch):
    """layers=True の E2E をモックで 1 回まわす (Foundry を呼ばない)。"""
    monkeypatch.chdir(tmp_path)
    from cc_orchestrator import pipeline

    def _run(agent: FakeAnalysisAgent, **extra):
        client = FakeFoundry(agent)
        monkeypatch.setattr(pipeline, "FoundryAgentsV2", lambda *a, **k: client)
        monkeypatch.setattr(pipeline, "ToolExecutor", FakeExecutor)
        stages: list[str] = []
        summary = pipeline.run_pipeline(
            "今週の研究を概念地図として整理して", target="file",
            kg_file=str(KG_FIXTURE), verify_causal=False, export_svg=False,
            layers=True, progress=lambda key, label: stages.append(key), **extra)
        session = summary["session"]
        kg = json.loads((tmp_path / f"graphs/kg_session_{session}.json")
                        .read_text(encoding="utf-8"))
        return summary, kg, stages, client
    return _run


def test_mock_e2e_writes_a_sidecar_in_the_designed_shape(mock_run) -> None:
    """layers=True で layers_session が設計 §3.2 のスキーマどおりに出る。"""
    agent = FakeAnalysisAgent()
    summary, _, _, client = mock_run(agent)

    assert summary["layers"]["status"] == "generated"
    assert "cc-analysis" in client.ensured          # ensure_agents で登録される
    assert agent.tasks[0] == "zone" and "claims" in agent.tasks

    doc = json.loads(Path(summary["layers"]["saved"]).read_text(encoding="utf-8"))
    assert list(doc)[:3] == ["version", "session", "splitter"]
    assert doc["version"] == 1 and doc["splitter"] == SPLITTER_VERSION
    assert doc["arguments"] == [] and doc["refutes"] == []      # M6 が埋める

    zone = doc["zones"][0]
    assert set(zone) == {"sentence_id", "text", "zone_label", "zone_system",
                         "confidence", "document_id", "char_start", "char_end"}
    assert zone["zone_label"] == "Result" and 0.0 <= zone["confidence"] <= 1.0

    claim = doc["claims"][0]
    assert set(claim) == {"nanopub_id", "assertion", "provenance", "pub_info"}
    assert claim["nanopub_id"].startswith("np:")
    assert set(claim["assertion"]) == {"claim_id", "claim_text",
                                       "is_underspecified", "related_concepts"}
    assert claim["provenance"]["extractor_id"] == "cc-analysis"
    assert doc["stats"]["zoned"] == len(doc["zones"])
    assert doc["stats"]["llm_calls"] <= 30          # 受け入れ基準 5


def test_mock_e2e_stamps_layers_and_lights_new_glyphs(mock_run) -> None:
    """L5 の関係候補が地図の ◇ / ◧ になるところまで通す。"""
    agent = FakeAnalysisAgent(relations=[
        {"from": "時系列情報フロー", "to": "動的グラフ", "relation": "is_a"},
        {"from": "概念マップ", "to": "出典スニペット", "relation": "part_of"}])
    summary, kg, _, _ = mock_run(agent)

    assert kg["layer_model"] == "r2a"
    glyphs = {e["id"]: e["glyph"] for e in kg["edges"]}
    assert glyphs["r005"] == "isa" and glyphs["r008"] == "partof"
    # 層 B は zone からの転用 (LLM の追加呼び出しなし)
    r005 = next(e for e in kg["edges"] if e["id"] == "r005")
    assert r005["layer_tags"]["layer_B"] == ["result_of"]
    assert r005["layer_tags"]["layer_A"] == ["is_a"]
    assert summary["layers"]["assigned"]["layer_a_from_llm"] == 2
    # ギャップ候補は層の語彙外なので触らない
    assert glyphs["r010"] == "hole" and glyphs["r013"] == "tension"


def test_progress_fires_for_zone_and_claims_even_when_skipped(tmp_path,
                                                              monkeypatch) -> None:
    """層を作らない run でも進捗は出す (止まって見えると「固まった」と読まれる)。"""
    monkeypatch.chdir(tmp_path)
    from cc_orchestrator.pipeline import run_pipeline

    stages: list[str] = []
    summary = run_pipeline("今週の研究を概念地図として整理して", target="file",
                           kg_file=str(KG_FIXTURE), offline=True,
                           verify_causal=False, export_svg=False,
                           progress=lambda key, label: stages.append(key))
    assert summary["layers"]["status"] == "disabled"
    assert stages.index("zone") < stages.index("claims") < stages.index("relate")


def test_offline_reuses_an_existing_sidecar(tmp_path, monkeypatch) -> None:
    """offline は LLM を呼べないが、元セッションの層があれば再利用する (§9)。"""
    monkeypatch.chdir(tmp_path)
    graphs = tmp_path / "graphs"
    graphs.mkdir()
    source_kg = json.loads(KG_FIXTURE.read_text(encoding="utf-8"))
    (graphs / "kg_session_20260101_000000.json").write_text(
        json.dumps(source_kg, ensure_ascii=False), encoding="utf-8")
    doc = layers_store.new_document("20260101_000000")
    doc["zones"] = [zone_of("d1#0000#aa", "サンプル文書中の記述 5-1", "Result")]
    doc["ontology"] = {"concepts": [], "relations": [
        {"from": "時系列情報フロー", "to": "動的グラフ", "relation": "is_a"}]}
    layers_store.save("20260101_000000", doc, graphs_dir=graphs)

    from cc_orchestrator.pipeline import run_pipeline

    summary = run_pipeline(
        "今週の研究を概念地図として整理して", target="file",
        kg_file=str(graphs / "kg_session_20260101_000000.json"),
        offline=True, verify_causal=False, export_svg=False, layers=True)

    assert summary["layers"]["status"] == "reused"
    assert summary["layers"]["source_session"] == "20260101_000000"
    # 再利用でも層タグは決定的に刻み直される (LLM 呼び出しゼロ)
    kg = json.loads((graphs / f"kg_session_{summary['session']}.json")
                    .read_text(encoding="utf-8"))
    assert next(e for e in kg["edges"] if e["id"] == "r005")["glyph"] == "isa"
    # 新セッションでも自己完結する (サイドカーが複製されている)
    assert layers_store.exists(summary["session"], graphs_dir=graphs)


def test_layers_off_is_byte_identical_to_r15(tmp_path, monkeypatch) -> None:
    """既定 (layers=False) では層のファイルすら作らない — 挙動完全不変。"""
    monkeypatch.chdir(tmp_path)
    from cc_orchestrator.pipeline import run_pipeline

    summary = run_pipeline("今週の研究を概念地図として整理して", target="file",
                           kg_file=str(KG_FIXTURE), offline=True,
                           verify_causal=False, export_svg=False)
    kg = json.loads((tmp_path / f"graphs/kg_session_{summary['session']}.json")
                    .read_text(encoding="utf-8"))
    assert summary["layers"] == {"status": "disabled"}
    assert not list((tmp_path / "graphs").glob("layers_session_*.json"))
    assert all("layer_tags" not in e for e in kg["edges"])
    assert all("claim_refs" not in n for n in kg["nodes"])
