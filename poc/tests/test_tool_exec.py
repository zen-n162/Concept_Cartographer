"""ToolExecutor の頑健性テスト。

エージェントが knowledge_graph と layout_plan を取り違えて渡してきても、
パイプラインが壊れない (正しい描画対象を保持し続ける) ことを保証する。
2026-08-05 の実障害: validate に KG が渡され、last_plan がそれで上書きされて
「スキーマ違反: graph_version と source_files」で全体が失敗した。
"""

import json
from pathlib import Path

import pytest

from cc_core.layout import compute_layout
from cc_orchestrator.tool_exec import ToolExecutor

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def kg():
    return json.loads((FIXTURES / "kg_min.json").read_text(encoding="utf-8"))


@pytest.fixture
def ex():
    return ToolExecutor(target="local")


def test_compute_layout_sets_last_plan(ex, kg):
    plan = ex("compute_layout", {"knowledge_graph": kg})
    assert ex.last_plan == plan
    assert plan["nodes"][0]["x"] is not None


def test_validate_with_kg_falls_back_to_computed_plan(ex, kg):
    """KG を validate に渡されても、直前の layout_plan を検証して valid を返す。"""
    plan = ex("compute_layout", {"knowledge_graph": kg})
    result = ex("validate_layout_plan", {"plan": kg})  # ← 取り違え
    assert result["valid"], result["errors"]
    assert ex.last_plan == plan, "last_plan が KG で上書きされてはいけない"


def test_validate_does_not_overwrite_last_plan(ex, kg):
    plan = ex("compute_layout", {"knowledge_graph": kg})
    ex("validate_layout_plan", {"plan": plan})
    assert ex.last_plan == plan


def test_validate_without_prior_layout_is_graceful(ex, kg):
    result = ex("validate_layout_plan", {"plan": kg})
    assert result["valid"] is False
    assert "compute_layout" in result["errors"][0]


def test_kg_is_not_mistaken_for_plan(ex, kg):
    assert not ex._looks_like_plan(kg)
    assert ex._looks_like_plan(compute_layout(kg))


def test_unknown_tool_returns_error(ex):
    assert "error" in ex("no_such_tool", {})
