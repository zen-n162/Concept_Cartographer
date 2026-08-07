"""R2a 知識モデル多層化 M1+M2 の回帰テスト — R2a 設計書 §11。

主眼は 3 つ:
  - **glyph の定義箇所が increases したときに揃っているか** (§2 の同期リスト)。
    記号を 1 か所だけ足して他を忘れる事故は実際に起きる (portal_agent の
    tension 欠落がそれ) ので、Python / JSON Schema / JS / CSS を機械検査する
  - project_glyph の 11 規則と、その順序 (§4)。順序そのものが仕様
  - **挙動不変** — 層タグが無い R1.5 世代のエッジは glyph も座標も変わらない

各テストは tmp_path を作業ディレクトリにするので production/ を汚さない。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cc_core.adapter import GLYPH_STYLES
from cc_core.detail import EDGE_CARRY, NODE_CARRY, build_multilevel_plan, project
from cc_core.excalidraw_file import build_scene
from cc_core.layers import (
    CAUSAL_GLYPH,
    LAYER_A,
    LAYER_B,
    LAYER_C,
    LAYER_D,
    LAYER_MODEL,
    LAYER_VOCAB,
    apply_meta,
    corroborated,
    normalize_layer_tags,
    project_glyph,
    verifier_id,
)
from cc_core.layout import GLYPH_PREFIX_EM, compute_layout
from cc_core.normalize import VALID_GLYPHS, normalize_kg
from cc_core.svg_export import build_svg
from cc_core.validate import SCHEMA_PATH, validate_layout_plan

PRODUCTION = Path(__file__).resolve().parents[1]
STATIC = PRODUCTION / "src" / "cc_web" / "static"


# --------------------------------------------------------------- 補助


def kg_with(edges: list[dict], nodes: list[dict] | None = None) -> dict:
    nodes = nodes or [
        {"id": "c001", "label": "概念A", "community_id": "comm_001"},
        {"id": "c002", "label": "概念B", "community_id": "comm_001"},
    ]
    return {
        "graph_version": "kg_r2a_test",
        "nodes": nodes,
        "edges": edges,
        "communities": [{"id": "comm_001", "name": "テーマ", "is_gap": False}],
    }


def tagged(**layers: list[str]) -> dict[str, list[str]]:
    """layer_tags を 4 層そろった形で作る。"""
    return {key: list(layers.get(key, ())) for key in LAYER_VOCAB}


# ======================================================= M1: glyph 同期


def test_glyph_vocabulary_is_ten() -> None:
    """記号は 6 -> 10 種 (設計書 §2)。追加のみで、既存 6 種は消さない。"""
    assert VALID_GLYPHS == {"arrow", "wave", "zigzag", "double", "hole", "tension",
                            "isa", "partof", "precedes", "question"}


def test_layout_reuses_normalize_vocabulary() -> None:
    """layout.py の重複定義は廃止済み — normalize の集合を**そのまま**使う。"""
    from cc_core import layout, normalize

    assert not hasattr(layout, "_LOCAL_VALID_GLYPHS")
    assert layout.VALID_GLYPHS is normalize.VALID_GLYPHS


def test_every_glyph_has_a_draw_style() -> None:
    """描画スタイルが無い記号があると adapter/excalidraw が KeyError で落ちる。"""
    assert set(GLYPH_STYLES) == VALID_GLYPHS
    for name, style in GLYPH_STYLES.items():
        for key in ("strokeColor", "strokeStyle", "strokeWidth",
                    "endArrowhead", "opacity", "label_prefix"):
            assert key in style, f"{name}: {key} が無い"


def test_every_glyph_has_a_label_width() -> None:
    """接頭記号の幅が無いとエッジラベルの間隔計算が過小になり文字が重なる。"""
    assert set(GLYPH_PREFIX_EM) == VALID_GLYPHS


def test_schema_glyph_enum_matches_vocabulary() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    enum = schema["properties"]["edges"]["items"]["properties"]["glyph"]["enum"]
    assert set(enum) == VALID_GLYPHS
    assert len(enum) == len(set(enum))


def test_app_js_glyph_info_matches_vocabulary() -> None:
    """Web UI の表示名。欠けるとポップオーバーが glyph の生 ID を出す。"""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    block = re.search(r"var GLYPH_INFO = \{(.*?)\n  \};", source, re.S)
    assert block, "app.js の GLYPH_INFO が見つからない"
    keys = set(re.findall(r"^\s*(\w+):\s*\{", block.group(1), re.M))
    assert keys == VALID_GLYPHS


def test_app_css_has_a_badge_class_per_glyph() -> None:
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    classes = set(re.findall(r"\.glyph\.(\w+)", css))
    assert VALID_GLYPHS <= classes


def test_portal_agent_glyph_table_matches_adapter() -> None:
    """ポータル版の埋め込みスクリプトも同じ 10 種を持つ。

    tension が欠けていて描けない (arrow へ落ちる) 既知の不整合の回帰防止。
    """
    from cc_orchestrator.portal_agent import BUILDER_SCRIPT

    table = re.search(r"^G = \{(.*?)^\}", BUILDER_SCRIPT, re.S | re.M)
    assert table, "BUILDER_SCRIPT の G テーブルが見つからない"
    assert set(re.findall(r'"(\w+)":\s*\(', table.group(1))) == VALID_GLYPHS
    pre = re.search(r"^PRE_EM = \{(.*?)\}\n", BUILDER_SCRIPT, re.S | re.M)
    assert pre and set(re.findall(r'"(\w+)":', pre.group(1))) == VALID_GLYPHS


@pytest.mark.parametrize("glyph", sorted(VALID_GLYPHS))
def test_new_glyphs_render_without_keyerror(glyph: str) -> None:
    """10 種すべてが SVG / .excalidraw のどちらでも描ける。"""
    kg = kg_with([{"id": "r001", "from": "c001", "to": "c002",
                   "label": "関係", "glyph": glyph}])
    plan = compute_layout(kg)
    svg = build_svg(plan)
    assert svg.startswith("<svg") and GLYPH_STYLES[glyph]["strokeColor"] in svg
    scene = build_scene(plan)
    assert any(e["id"] == "edge-r001" for e in scene["elements"])


# ================================================ M1: project_glyph 11 規則


def test_rule1_user_origin_wins_over_layer_tags() -> None:
    """人間が選んだ記号は機械が塗り替えない (裁定 D)。層タグより強い。"""
    edge = {"glyph": "wave", "origin": "user_edited",
            "layer_tags": tagged(layer_D=["refutes"])}
    assert project_glyph(edge) == "wave"
    edge["origin"] = "user_added"
    assert project_glyph(edge) == "wave"


def test_rule2_passthrough_when_no_layer_tags() -> None:
    """R1.5 世代 (層タグなし) は素通し。**挙動不変の要**。"""
    for glyph in sorted(VALID_GLYPHS):
        assert project_glyph({"glyph": glyph}) == glyph
        assert project_glyph({"glyph": glyph, "layer_tags": tagged()}) == glyph


def test_rule3_refutes_lights_zigzag() -> None:
    """矛盾は層 D の refutes でのみ点灯 (層 C に否定は入れない)。"""
    edge = {"glyph": "wave", "layer_tags": tagged(layer_D=["refutes"],
                                                  layer_C=["causes"])}
    assert project_glyph(edge) == "zigzag"


def test_rule4_corroborated_causes_becomes_arrow() -> None:
    edge = {"glyph": "wave", "layer_tags": tagged(layer_C=["causes"]),
            "validation": {"combined": 0.81}}
    assert project_glyph(edge) == CAUSAL_GLYPH


def test_rule5_questions_beats_precedes_and_below() -> None:
    edge = {"glyph": "wave",
            "layer_tags": tagged(layer_C=["precedes"], layer_D=["questions"],
                                 layer_A=["is_a"])}
    assert project_glyph(edge) == "question"


def test_rule6_precedes_beats_isa() -> None:
    edge = {"glyph": "wave",
            "layer_tags": tagged(layer_C=["precedes"], layer_A=["is_a", "part_of"])}
    assert project_glyph(edge) == "precedes"


def test_rule7_is_a_becomes_isa() -> None:
    edge = {"glyph": "wave", "layer_tags": tagged(layer_A=["is_a", "part_of"])}
    assert project_glyph(edge) == "isa"


def test_rule8_part_of_becomes_partof() -> None:
    edge = {"glyph": "wave", "layer_tags": tagged(layer_A=["part_of"],
                                                  layer_D=["corroborates"])}
    assert project_glyph(edge) == "partof"


@pytest.mark.parametrize("tag", ["corroborates", "agrees_with"])
def test_rule9_corroborates_or_agrees_becomes_double(tag: str) -> None:
    """⇒(double) = 層D corroborates / agrees_with (裁定 F)。"""
    assert project_glyph({"glyph": "wave", "layer_tags": tagged(layer_D=[tag])}) == "double"


def test_rule10_uncorroborated_causes_demotes_to_wave() -> None:
    """裏付けの無い causes は相関へ降格する (相関を因果へ昇格させない)。"""
    edge = {"glyph": "arrow", "layer_tags": tagged(layer_C=["causes"])}
    assert project_glyph(edge) == "wave"


def test_rule11_default_is_wave() -> None:
    edge = {"glyph": "arrow", "layer_tags": tagged(layer_B=["result_of"],
                                                   layer_C=["correlates_with"])}
    assert project_glyph(edge) == "wave"


def test_projection_is_pure_and_deterministic() -> None:
    """純関数: 同じ入力から常に同じ結果。edge も書き換えない。"""
    edge = {"glyph": "wave", "layer_tags": tagged(layer_A=["is_a"]), "label": "は一種"}
    snapshot = json.dumps(edge, sort_keys=True)
    results = {project_glyph(edge) for _ in range(5)}
    assert results == {"isa"}
    assert json.dumps(edge, sort_keys=True) == snapshot


def test_demotion_records_demoted_from_for_kpi_continuity() -> None:
    """規則⑩ の降格は causal_check に印を残す (causal_precision_log の連続性)。"""
    from cc_core.evaluation import causal_precision_log
    from cc_core.layers import apply_glyph_projection

    edge = {"id": "r001", "glyph": "arrow", "from": "c001", "to": "c002",
            "layer_tags": tagged(layer_C=["causes"])}
    assert apply_glyph_projection(edge) == "wave"
    assert edge["causal_check"]["demoted_from"] == CAUSAL_GLYPH
    kpi = causal_precision_log({"edges": [edge]})
    assert kpi["causal_candidates"] == 1 and kpi["demoted_to_correlation"] == 1


def test_corroborated_three_paths() -> None:
    """裏付けの根拠は 3 経路 (学習の上書き / 検証スコア / R1.5 互換)。"""
    assert corroborated({"causal_override": "allow"})               # ①学習
    assert corroborated({"validation": {"combined": 0.75}})         # ②閾値ちょうど
    assert not corroborated({"validation": {"combined": 0.74}})
    # ③互換モード: causal_check があり demoted_from が無ければ 3 点セット通過
    assert corroborated({"causal_check": {"verifier_verdict": "skipped"}})
    assert not corroborated({"causal_check": {"demoted_from": "arrow"}})
    assert not corroborated({})           # 何の記録も無ければ因果にしない


def test_learned_allow_overrides_low_validation_score() -> None:
    """人間が一度 allow と判断した対は、低い検証スコアでも因果のまま。"""
    edge = {"glyph": "wave", "causal_override": "allow",
            "validation": {"combined": 0.1},
            "layer_tags": tagged(layer_C=["causes"])}
    assert project_glyph(edge) == CAUSAL_GLYPH


# ============================================== M1: layer_tags の正規化


def test_vocabulary_is_thirty_terms_in_four_layers() -> None:
    assert (len(LAYER_A), len(LAYER_B), len(LAYER_C), len(LAYER_D)) == (6, 8, 8, 8)
    all_tags = LAYER_A + LAYER_B + LAYER_C + LAYER_D
    assert len(all_tags) == 30 and len(set(all_tags)) == 30


def test_normalize_layer_tags_accepts_hyphen_and_case() -> None:
    tags, dropped = normalize_layer_tags({"layer-a": ["Is-A", "PART_OF"],
                                          "D": ["Agrees-With"]})
    assert tags["layer_A"] == ["is_a", "part_of"]
    assert tags["layer_D"] == ["agrees_with"]
    assert dropped == []


def test_normalize_layer_tags_drops_unknown_and_reports() -> None:
    """未知タグは**捨てて報告**する (黙って通すと投影が予期せぬ記号を出す)。"""
    tags, dropped = normalize_layer_tags({
        "layer_C": ["causes", "teleports_to"],
        "layer_A": ["causes"],          # 層をまたいだ誤配置も未知扱い
        "layer_Z": ["whatever"],
    })
    assert tags["layer_C"] == ["causes"]
    assert tags["layer_A"] == []
    assert len(dropped) == 3
    assert any("teleports_to" in d for d in dropped)
    assert any("layer_Z" in d for d in dropped)


def test_normalize_layer_tags_always_returns_four_layers() -> None:
    tags, _ = normalize_layer_tags({"layer_C": "causes"})   # 裸の文字列も受ける
    assert set(tags) == set(LAYER_VOCAB)
    assert tags["layer_C"] == ["causes"]


def test_normalize_layer_tags_dedupes_and_orders_by_vocabulary() -> None:
    """出力順を語彙表に固定する — 同じ入力から常に同じ JSON になるように。"""
    tags, _ = normalize_layer_tags({"layer_C": ["correlates_with", "causes", "causes"]})
    assert tags["layer_C"] == ["causes", "correlates_with"]


def test_normalize_kg_normalizes_layer_tags_and_reports() -> None:
    kg, report = normalize_kg(kg_with([{
        "id": "r001", "from": "c001", "to": "c002", "glyph": "wave",
        "layer_tags": {"layer_C": ["causes", "nonsense"]},
    }]))
    assert kg["edges"][0]["layer_tags"]["layer_C"] == ["causes"]
    assert any("nonsense" in key for key in report.repairs)


def test_normalize_kg_leaves_r15_edges_untouched() -> None:
    """層タグの無いエッジに勝手なキーを生やさない (旧世代との差分を保つ)。"""
    kg, _ = normalize_kg(kg_with([{"id": "r001", "from": "c001", "to": "c002",
                                   "glyph": "wave"}]))
    assert "layer_tags" not in kg["edges"][0]
    assert "polarity" not in kg["edges"][0]


def test_normalize_kg_validates_polarity_enum() -> None:
    kg, report = normalize_kg(kg_with([
        {"id": "r001", "from": "c001", "to": "c002", "glyph": "wave",
         "polarity": "negative"},
        {"id": "r002", "from": "c002", "to": "c001", "glyph": "wave",
         "polarity": "とても positive"},
    ]))
    assert kg["edges"][0]["polarity"] == "negative"
    assert "polarity" not in kg["edges"][1]      # enum 外は破棄 (充填は ⑦meta)
    assert any("polarity" in key for key in report.repairs)


def test_normalize_kg_type_checks_onto_class_and_claim_refs() -> None:
    kg, report = normalize_kg(kg_with(
        [{"id": "r001", "from": "c001", "to": "c002", "glyph": "wave",
          "claim_refs": ["np:ab12"]}],
        nodes=[{"id": "c001", "label": "A", "community_id": "comm_001",
                "onto_class": "bfo:MaterialEntity", "claim_refs": ["np:ab12"]},
               {"id": "c002", "label": "B", "community_id": "comm_001",
                "onto_class": {"bad": "shape"}}]))
    assert kg["nodes"][0]["onto_class"] == "bfo:MaterialEntity"
    assert kg["nodes"][0]["claim_refs"] == ["np:ab12"]
    assert "onto_class" not in kg["nodes"][1]
    assert kg["edges"][0]["claim_refs"] == ["np:ab12"]
    assert report.repairs


def test_normalize_kg_keeps_layer_model_stamp() -> None:
    """R2a の kg を再取り込みしても世代印を落とさない。"""
    raw = kg_with([{"id": "r001", "from": "c001", "to": "c002", "glyph": "wave"}])
    raw["layer_model"] = LAYER_MODEL
    kg, _ = normalize_kg(raw)
    assert kg["layer_model"] == LAYER_MODEL


# ==================================================== M1: 運搬とスキーマ


def test_layer_fields_reach_the_plan() -> None:
    """kg -> plan の運搬。detail.py の carry リストから漏れると plan に届かない。"""
    kg = kg_with(
        [{"id": "r001", "from": "c001", "to": "c002", "glyph": "isa",
          "layer_tags": tagged(layer_A=["is_a"]), "claim_refs": ["np:ab12"],
          "polarity": "neutral"}],
        nodes=[{"id": "c001", "label": "概念A", "community_id": "comm_001",
                "onto_class": "bfo:MaterialEntity", "claim_refs": ["np:ab12"]},
               {"id": "c002", "label": "概念B", "community_id": "comm_001"}])
    view = project(build_multilevel_plan(kg, default_level="detailed"), "detailed")
    edge = next(e for e in view["edges"] if e["id"] == "r001")
    assert edge["layer_tags"]["layer_A"] == ["is_a"]
    assert edge["claim_refs"] == ["np:ab12"]
    node = next(n for n in view["nodes"] if n["id"] == "c001")
    assert node["onto_class"] == "bfo:MaterialEntity"
    assert node["claim_refs"] == ["np:ab12"]
    assert validate_layout_plan(view).valid


def test_carry_lists_cover_the_new_fields() -> None:
    """新フィールドを足したら carry リストも更新する、を機械で担保する。"""
    assert {"layer_tags", "claim_refs", "polarity", "provenance"} <= set(EDGE_CARRY)
    assert {"onto_class", "claim_refs"} <= set(NODE_CARRY)


def test_web_view_fields_cover_the_new_fields() -> None:
    from cc_web.sessions import EDGE_FIELDS, NODE_FIELDS

    assert {"layer_tags", "claim_refs"} <= set(EDGE_FIELDS)
    assert {"onto_class", "claim_refs"} <= set(NODE_FIELDS)


def test_schema_round_trip_with_all_r2a_fields() -> None:
    """新フィールド入りの plan がスキーマを通り、書いて読んでも同じ形で残る。"""
    kg = kg_with(
        [{"id": "r001", "from": "c001", "to": "c002", "glyph": "question",
          "label": "本当か", "layer_tags": tagged(layer_D=["questions"]),
          "claim_refs": ["np:ab12"], "polarity": "negative",
          "provenance": {"extractor_model": "gpt-5.6-sol", "timestamp": "2026-08-07",
                         "validator_ids": ["llm-verifier:terra"],
                         "human_reviewed": False}}],
        nodes=[{"id": "c001", "label": "概念A", "community_id": "comm_001",
                "onto_class": "UNKNOWN", "claim_refs": ["np:ab12"]},
               {"id": "c002", "label": "概念B", "community_id": "comm_001"}])
    view = project(build_multilevel_plan(kg, default_level="detailed"), "detailed")
    result = validate_layout_plan(view)
    assert result.valid, result.errors
    reloaded = json.loads(json.dumps(view, ensure_ascii=False))
    assert reloaded == view and validate_layout_plan(reloaded).valid


def test_schema_rejects_unknown_layer_key() -> None:
    """additionalProperties:false — 語彙外の層キーはスキーマで止まる。"""
    kg = kg_with([{"id": "r001", "from": "c001", "to": "c002", "glyph": "wave",
                   "layer_tags": {"layer_E": ["whatever"]}}])
    plan = project(build_multilevel_plan(kg, default_level="detailed"), "detailed")
    assert not validate_layout_plan(plan).valid


# ============================================ M2: ⑦meta (polarity/provenance)


def test_meta_fills_polarity_with_neutral() -> None:
    kg = kg_with([{"id": "r001", "from": "c001", "to": "c002", "glyph": "wave"},
                  {"id": "r002", "from": "c002", "to": "c001", "glyph": "wave",
                   "polarity": "negative"}])
    stats = apply_meta(kg, extractor_model="gpt-5.6-sol")
    assert kg["edges"][0]["polarity"] == "neutral"
    assert kg["edges"][1]["polarity"] == "negative"      # 既存値は上書きしない
    assert stats["polarity_filled"] == 1


def test_meta_writes_provenance_with_actual_validators() -> None:
    """validator_ids は**実際に走った**検証器のみ (走っていない ID を書かない)。"""
    kg = kg_with([{"id": "r001", "from": "c001", "to": "c002", "glyph": "wave"}])
    apply_meta(kg, extractor_model="gpt-5.6-sol",
               validator_ids=["llm-verifier:terra"], timestamp="2026-08-07T00:00:00")
    prov = kg["edges"][0]["provenance"]
    assert prov == {"extractor_model": "gpt-5.6-sol",
                    "timestamp": "2026-08-07T00:00:00",
                    "validator_ids": ["llm-verifier:terra"],
                    "human_reviewed": False}

    kg2 = kg_with([{"id": "r001", "from": "c001", "to": "c002", "glyph": "wave"}])
    apply_meta(kg2, extractor_model="kg_file")
    assert kg2["edges"][0]["provenance"]["validator_ids"] == []


def test_meta_preserves_human_reviewed() -> None:
    """人が見た事実を機械が消さない。"""
    kg = kg_with([{"id": "r001", "from": "c001", "to": "c002", "glyph": "wave",
                   "provenance": {"human_reviewed": True, "note": "査読済み"}}])
    apply_meta(kg, extractor_model="gpt-5.6-sol")
    prov = kg["edges"][0]["provenance"]
    assert prov["human_reviewed"] is True and prov["note"] == "査読済み"


def test_meta_stamps_layer_model() -> None:
    kg = kg_with([{"id": "r001", "from": "c001", "to": "c002", "glyph": "wave"}])
    apply_meta(kg, extractor_model="gpt-5.6-sol")
    assert kg["layer_model"] == LAYER_MODEL == "r2a"


def test_meta_does_not_change_r15_glyphs() -> None:
    """**挙動不変**: 層タグが無い世代では meta を通しても記号が動かない。"""
    edges = [{"id": f"r{i:03d}", "from": "c001", "to": "c002", "glyph": g,
              "label": "関係"}
             for i, g in enumerate(sorted(VALID_GLYPHS))]
    kg = kg_with(edges)
    before = [e["glyph"] for e in kg["edges"]]
    stats = apply_meta(kg, extractor_model="gpt-5.6-sol")
    assert [e["glyph"] for e in kg["edges"]] == before
    assert stats["glyph_changed"] == 0 and stats["demoted_from_causal"] == 0
    assert all("causal_check" not in e for e in kg["edges"])


def test_meta_projects_when_layer_tags_are_present() -> None:
    kg = kg_with([{"id": "r001", "from": "c001", "to": "c002", "glyph": "wave",
                   "layer_tags": tagged(layer_A=["is_a"])}])
    stats = apply_meta(kg, extractor_model="gpt-5.6-sol")
    assert kg["edges"][0]["glyph"] == "isa"
    assert stats["glyph_changed"] == 1


def test_meta_output_is_deterministic() -> None:
    """同じ入力 + 同じ timestamp なら同じ JSON (差分が読めるようにするため)。"""
    def run() -> str:
        kg = kg_with([{"id": "r001", "from": "c001", "to": "c002", "glyph": "wave"}])
        apply_meta(kg, extractor_model="gpt-5.6-sol", timestamp="2026-08-07T00:00:00")
        return json.dumps(kg, sort_keys=True, ensure_ascii=False)

    assert run() == run()


def test_verifier_id_shortens_model_name() -> None:
    assert verifier_id("gpt-5.6-terra") == "llm-verifier:terra"


def test_old_session_without_polarity_still_loads() -> None:
    """旧セッション (polarity なし) の読込・plan 生成・検証が通る。"""
    kg = kg_with([{"id": "r001", "from": "c001", "to": "c002", "glyph": "wave",
                   "label": "関連"}])
    plan = build_multilevel_plan(kg, default_level="standard")
    view = project(plan, "standard")
    assert validate_layout_plan(view).valid
    assert "polarity" not in view["edges"][0]


def test_extraction_agent_asks_for_polarity() -> None:
    from cc_orchestrator.agents_def import EXTRACTION_INSTRUCTIONS

    assert "polarity" in EXTRACTION_INSTRUCTIONS
    for value in ("positive", "negative", "neutral"):
        assert value in EXTRACTION_INSTRUCTIONS


# ==================================================== M2: パイプライン統合


@pytest.fixture
def offline_run(tmp_path, monkeypatch):
    """production/ を汚さずに offline パイプラインを 1 回まわす。"""
    monkeypatch.chdir(tmp_path)
    from cc_orchestrator.pipeline import run_pipeline

    def _run(**extra):
        summary = run_pipeline(
            "今週の研究を概念地図として整理して",
            target="file", kg_file=str(PRODUCTION / "tests/fixtures/kg_sample.json"),
            offline=True, verify_causal=False, export_svg=False, **extra)
        session = summary["session"]
        kg = json.loads(
            (tmp_path / f"graphs/kg_session_{session}.json").read_text("utf-8"))
        plan = json.loads(
            (tmp_path / f"graphs/layout_plan_session_{session}.json").read_text("utf-8"))
        return summary, kg, plan
    return _run


def test_pipeline_defaults_to_layers_off(offline_run) -> None:
    """M1〜M6 の間は layers=False が既定 (本番挙動は R1.5 のまま)。"""
    summary, _, _ = offline_run()
    assert summary["layers"]["status"] == "disabled"


def test_pipeline_layers_flag_is_honest_when_it_cannot_run(offline_run) -> None:
    """やらなかったことを黙って成功にしない。

    offline は LLM を呼べない。再利用できるサイドカーも無い (fixture の
    kg_file は kg_session_* 命名ではない) ので、理由の分かる status を返す。
    """
    summary, _, _ = offline_run(layers=True)
    assert summary["layers"]["status"] == "skipped_offline"
    assert summary["layers"]["reason"]


def test_pipeline_meta_runs_regardless_of_layers_flag(offline_run) -> None:
    summary, kg, plan = offline_run()
    assert kg["layer_model"] == LAYER_MODEL
    assert summary["meta"]["edges"] == len(kg["edges"])
    assert all(e["polarity"] == "neutral" for e in kg["edges"])
    assert all(e["provenance"]["human_reviewed"] is False for e in kg["edges"])
    # kg_file 経由は抽出 LLM を通っていない — モデル名を騙らない
    assert {e["provenance"]["extractor_model"] for e in kg["edges"]} == {"kg_file"}
    # offline は独立検証器を持てないので validator_ids は空が正しい記録
    assert all(e["provenance"]["validator_ids"] == [] for e in kg["edges"])
    # 運搬され、スキーマも通る
    assert all("polarity" in e and "provenance" in e for e in plan["edges"])
    assert validate_layout_plan(project(plan, "standard")).valid


def test_pipeline_glyphs_match_r15(offline_run) -> None:
    """**挙動不変の受け入れ基準**: 同じ kg_file から R1.5 と同じ glyph 分布。"""
    _, kg, plan = offline_run()
    from collections import Counter

    assert Counter(e["glyph"] for e in kg["edges"]) == Counter(
        {"wave": 7, "hole": 3, "double": 2, "tension": 1})
    assert Counter(e["glyph"] for e in plan["edges"]) == Counter(
        {"wave": 7, "hole": 3, "double": 2, "tension": 1})
    assert len(kg["nodes"]) == 19 and len(kg["edges"]) == 13
