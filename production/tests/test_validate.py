import copy
import json
from pathlib import Path

import pytest

from cc_core.validate import validate_layout_plan

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def plan_min():
    return json.loads((FIXTURES / "layout_plan_min.json").read_text(encoding="utf-8"))


@pytest.fixture
def plan_gap():
    return json.loads((FIXTURES / "layout_plan_gap.json").read_text(encoding="utf-8"))


def test_min_fixture_valid(plan_min):
    result = validate_layout_plan(plan_min)
    assert result.valid, result.errors
    assert result.errors == []


def test_gap_fixture_valid(plan_gap):
    result = validate_layout_plan(plan_gap)
    assert result.valid, result.errors


def test_duplicate_node_id(plan_min):
    plan = copy.deepcopy(plan_min)
    plan["nodes"].append(dict(plan["nodes"][0]))
    result = validate_layout_plan(plan)
    assert not result.valid
    assert any("duplicate node id" in e for e in result.errors)


def test_edge_to_missing_node(plan_min):
    plan = copy.deepcopy(plan_min)
    plan["edges"][0]["to"] = "c999"
    result = validate_layout_plan(plan)
    assert not result.valid
    assert any("c999" in e for e in result.errors)


def test_self_loop_rejected(plan_min):
    plan = copy.deepcopy(plan_min)
    plan["edges"][0]["to"] = plan["edges"][0]["from"]
    result = validate_layout_plan(plan)
    assert not result.valid
    assert any("self-loop" in e for e in result.errors)


def test_node_without_island(plan_min):
    plan = copy.deepcopy(plan_min)
    plan["nodes"][0]["community_id"] = "comm_nowhere"
    result = validate_layout_plan(plan)
    assert not result.valid
    assert any("no island entry" in e for e in result.errors)


def test_unknown_glyph_rejected_by_schema(plan_min):
    plan = copy.deepcopy(plan_min)
    plan["edges"][0]["glyph"] = "sparkle"
    result = validate_layout_plan(plan)
    assert not result.valid
    assert any("schema" in e for e in result.errors)


def test_missing_provenance_rejected(plan_min):
    plan = copy.deepcopy(plan_min)
    del plan["provenance"]
    result = validate_layout_plan(plan)
    assert not result.valid


def test_node_outside_bbox_is_warning(plan_min):
    plan = copy.deepcopy(plan_min)
    plan["nodes"][0]["x"] = 99999
    result = validate_layout_plan(plan)
    assert result.valid  # warning, not error
    assert any("outside island" in w for w in result.warnings)


# --- verify のラベル正規化 (2026-08-05 の偽陽性 FAIL 対策) ---

def test_label_normalization_ignores_wrapping_newlines():
    """ブラウザ接続時にフロントが挿入する折り返し改行を差分としない。"""
    from cc_core.verify import _normalize_label
    assert _normalize_label("研究情報の\n散在") == _normalize_label("研究情報の散在")
    assert _normalize_label(" ⇒ 補強 ") == _normalize_label("⇒ 補強")
    assert _normalize_label(None) == ""
    assert _normalize_label("A") != _normalize_label("B")


def test_labels_match_tolerates_transport_corruption():
    """MCP ゲートウェイの多バイト破損 (U+FFFD) は前方一致で許容【実測 r010】。"""
    from cc_core.verify import _labels_match

    assert _labels_match("他領域の分割を…", "他領域の分割を���")
    assert _labels_match("研究情報の散在", "研究情報の\n散在")   # 折返しは従来どおり
    assert not _labels_match("他領域の分割を…", "別のラベル�")  # 前方不一致は検出
    assert not _labels_match("正しいラベル", "違うラベル")
