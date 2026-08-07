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


# ------------------------------------------------ 復唱を信用しない (実測 2026-08-07)


def _plan_with_two_islands() -> dict:
    return {
        "detail_level": "standard",
        "nodes": [
            {"id": "n1", "label": "概念A", "community_id": "comm_000",
             "x": 0, "y": 0, "size": 120},
            {"id": "n2", "label": "概念B", "community_id": "comm_001",
             "x": 300, "y": 0, "size": 120},
        ],
        "edges": [],
        "islands": [
            {"community_id": "comm_000", "name": "島0", "bbox": [-40, -40, 200, 140],
             "is_gap": False},
            {"community_id": "comm_001", "name": "島1", "bbox": [260, -40, 500, 140],
             "is_gap": False},
        ],
        "gaps": [],
    }


def test_render_ignores_mangled_agent_echo():
    """確定 plan がある間は、島が欠けた復唱を渡されても正しい plan を描く。

    Web 実測 (session 20260807_165151): cc-projection の復唱から島 1 件が
    欠落し RENDER_FAILED になった。ツールは確定 plan を正とすること。
    """
    from cc_orchestrator.tool_exec import ToolExecutor

    good = _plan_with_two_islands()
    mangled = json.loads(json.dumps(good))
    mangled["islands"] = mangled["islands"][1:]   # comm_000 の島を落とす (復唱破損の再現)

    ex = ToolExecutor(target="file")
    ex.authoritative_plan = good
    result = ex.tool_render_layout_plan({"plan": mangled})
    assert result["success"], result
    assert ex.last_plan == good                    # 描いたのは確定 plan

    # 検証も確定 plan と突合する (復唱どうしの突合にしない)
    report = ex.tool_verify_scene({"plan": mangled})
    assert report["passed"], report


def test_render_uses_passed_plan_when_no_authoritative():
    """確定 plan が無い経路 (CLI --render 等) は従来どおり引数の plan を使う。"""
    from cc_orchestrator.tool_exec import ToolExecutor

    good = _plan_with_two_islands()
    ex = ToolExecutor(target="file")
    assert ex.authoritative_plan is None
    result = ex.tool_render_layout_plan({"plan": good})
    assert result["success"], result
    assert ex.last_plan == good
