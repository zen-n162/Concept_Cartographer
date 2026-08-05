"""Local E2E (メモ §8 最小描画テスト).

前提: 2 プロセスが起動していること
  ./scripts/start_canvas.sh   (127.0.0.1:3000)
  ./scripts/start_gateway.sh  (127.0.0.1:8000/mcp)
起動していなければ自動 skip。実行: pytest -m e2e
"""

import json
import urllib.request
from pathlib import Path

import pytest

from cc_core.adapter import render_layout_plan
from cc_core.mcp_client import ExcalidrawClient, extract_json
from cc_core.verify import verify_scene

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
HEALTH_URL = "http://127.0.0.1:8000/healthz"

pytestmark = pytest.mark.e2e


def _gateway_up() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


requires_gateway = pytest.mark.skipif(
    not _gateway_up(), reason="gateway not running at 127.0.0.1:8000 (see scripts/)"
)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@requires_gateway
async def test_tools_list_contains_required_tools():
    async with ExcalidrawClient() as client:
        names = set(await client.list_tool_names())
    required = {
        "create_element", "describe_scene", "query_elements",
        "export_scene", "export_to_image", "clear_canvas", "delete_element",
    }
    assert required <= names, f"missing: {required - names}"


@requires_gateway
async def test_render_min_plan_and_verify():
    plan = _load("layout_plan_min.json")
    async with ExcalidrawClient() as client:
        result = await render_layout_plan(plan, client, clear_before=True)
        assert result.success, result.errors
        # islands(1)*2 + nodes(2) + edges(1) = 5 elements
        assert len(result.created) == 5
        report = await verify_scene(plan, client)
        assert report["passed"], json.dumps(report, ensure_ascii=False)[:2000]


@requires_gateway
async def test_render_gap_plan_draws_dashed_translucent():
    plan = _load("layout_plan_gap.json")
    async with ExcalidrawClient() as client:
        result = await render_layout_plan(plan, client, clear_before=True)
        assert result.success, result.errors
        report = await verify_scene(plan, client)
        assert report["passed"], json.dumps(report, ensure_ascii=False)[:2000]

        elements = await client.call_json("query_elements", {})
        by_id = {el["id"]: el for el in elements}
        gap_island = by_id["isl-comm_gap_001"]
        assert gap_island["strokeStyle"] == "dashed"
        assert gap_island["opacity"] < 100
        gap_edge = by_id["edge-r002"]
        assert gap_edge["strokeStyle"] == "dashed"
        assert gap_edge["opacity"] < 100


@requires_gateway
async def test_export_scene_roundtrip(tmp_path):
    plan = _load("layout_plan_min.json")
    async with ExcalidrawClient() as client:
        result = await render_layout_plan(plan, client, clear_before=True)
        assert result.success
        scene_text = await client.call("export_scene")
        scene = extract_json(scene_text)
    assert scene["type"] == "excalidraw"
    assert len(scene["elements"]) == 5
    out = tmp_path / "scene.excalidraw"
    out.write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
    assert out.stat().st_size > 100


@requires_gateway
async def test_rollback_on_invalid_plan_leaves_canvas_clean():
    """バリデーション不合格の plan は 1 要素も描画されない。"""
    plan = _load("layout_plan_min.json")
    plan["edges"][0]["to"] = "c999"  # dangling reference
    async with ExcalidrawClient() as client:
        await client.call("clear_canvas")
        result = await render_layout_plan(plan, client, clear_before=False)
        assert not result.success
        elements = await client.call_json("query_elements", {})
        assert elements == []
