"""cc-tools MCP server: deterministic tools for Foundry agents.

公開ツール (Phase 3 のエージェントが利用):
  - validate_layout_plan : layout_plan の schema + 意味検証
  - compute_layout       : knowledge_graph -> layout_plan (決定的レイアウト)
  - render_layout_plan   : layout_plan -> Excalidraw 描画 (island->node->edge,
                           element ID 管理・リトライ・ロールバック込み)
  - verify_scene         : 描画結果を query/describe で突合
  - export_map           : .excalidraw / SVG のシーン取得

起動:
  python -m cc_tools.server            # streamable HTTP, 0.0.0.0:8080/mcp (コンテナ内)
  CC_TOOLS_HOST=127.0.0.1 python -m cc_tools.server   # ローカル検証

環境変数:
  EXCALIDRAW_MCP_URL      Excalidraw MCP gateway (default http://127.0.0.1:8000/mcp)
  CC_TOOLS_HOST / CC_TOOLS_PORT
"""

from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from cc_core import adapter, layout, validate, verify
from cc_core.logging_util import get_logger
from cc_core.mcp_client import ExcalidrawClient, extract_json

logger = get_logger("cc_tools")

mcp = FastMCP(
    "cc-tools",
    host=os.environ.get("CC_TOOLS_HOST", "0.0.0.0"),
    port=int(os.environ.get("CC_TOOLS_PORT", "8080")),
)


def _as_dict(value: Any, name: str) -> dict[str, Any]:
    """Accept dict or JSON string (LLM tool calls sometimes double-encode)."""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


@mcp.tool()
def validate_layout_plan(plan: dict | str) -> dict:
    """layout_plan.json をスキーマ検証し、ID 重複・参照切れ等の意味検証を行う。

    Returns {valid, errors, warnings}.
    """
    return validate.validate_layout_plan(_as_dict(plan, "plan")).to_dict()


@mcp.tool()
def compute_layout(knowledge_graph: dict | str, detail_level: str = "standard") -> dict:
    """knowledge_graph から layout_plan を決定的に計算する (座標を LLM で作らないこと)。

    knowledge_graph: {graph_version, nodes:[{id,label,community_id?}],
    edges:[{from,to,label?,glyph?}], communities:[{id,name,is_gap?}]}
    Returns a layout_plan that already passes validate_layout_plan.
    """
    plan = layout.compute_layout(_as_dict(knowledge_graph, "knowledge_graph"), detail_level)
    result = validate.validate_layout_plan(plan)
    if not result.valid:  # defensive: layout engine bug
        raise ValueError(f"layout engine produced invalid plan: {result.errors}")
    return plan


@mcp.tool()
async def render_layout_plan(plan: dict | str, clear_before: bool = True) -> dict:
    """layout_plan を Excalidraw キャンバスに描画する (island -> node -> edge)。

    失敗時は作成済み要素をロールバックする。
    Returns {success, element_map, created, errors, rolled_back}.
    """
    plan_dict = _as_dict(plan, "plan")
    async with ExcalidrawClient() as client:
        result = await adapter.render_layout_plan(plan_dict, client, clear_before=clear_before)
    return result.to_dict()


@mcp.tool()
async def verify_scene(plan: dict | str) -> dict:
    """描画結果を layout_plan と突合する (要素数・ID・ラベル・gap 描画スタイル)。

    Returns {passed, missing_elements, label_mismatches, gap_style_violations,
    describe_scene, ...}.
    """
    plan_dict = _as_dict(plan, "plan")
    async with ExcalidrawClient() as client:
        return await verify.verify_scene(plan_dict, client)


@mcp.tool()
async def export_map(format: str = "excalidraw") -> dict:
    """現在のシーンをエクスポートする。format: 'excalidraw' | 'svg'。

    Returns {format, data}. PNG はブラウザ接続が必要なため canvas UI 側で取得する。
    """
    async with ExcalidrawClient() as client:
        if format == "excalidraw":
            scene = extract_json(await client.call("export_scene"))
            return {"format": "excalidraw", "data": scene}
        if format == "svg":
            svg = await client.call("export_to_image", {"format": "svg"})
            return {"format": "svg", "data": svg}
    raise ValueError("format must be 'excalidraw' or 'svg'")


if __name__ == "__main__":
    logger.info("starting cc-tools MCP (streamable HTTP)")
    mcp.run(transport="streamable-http")
