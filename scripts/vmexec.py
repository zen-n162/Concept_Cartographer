#!/usr/bin/env python3
"""VM 上で実行される描画エグゼキュータ (az vm run-command から呼ばれる).

/opt/cartographer/app/vmexec.py として配備され、同 VM の Excalidraw MCP
gateway (127.0.0.1:8000/mcp) に対して cc_core を実行する。

Usage (run-command 内):
    vmexec.py render  <base64(layout_plan.json)>   # clear + render + verify
    vmexec.py verify  <base64(layout_plan.json)>
    vmexec.py status

出力: 1 行の JSON (run-command の stdout 上限 ~4KB に収める)。
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys

sys.path.insert(0, "/opt/cartographer/app/src")

from cc_core.adapter import render_layout_plan  # noqa: E402
from cc_core.mcp_client import ExcalidrawClient  # noqa: E402
from cc_core.verify import verify_scene  # noqa: E402


def _decode(arg: str) -> dict:
    return json.loads(base64.b64decode(arg).decode("utf-8"))


def _compact(report: dict) -> dict:
    """run-command の stdout 上限に収まるよう describe_scene を要約する。"""
    out = dict(report)
    desc = out.pop("describe_scene", "") or ""
    out["describe_scene_head"] = desc[:600]
    out["missing_elements"] = out.get("missing_elements", [])[:20]
    out["label_mismatches"] = out.get("label_mismatches", [])[:10]
    return out


async def main() -> int:
    cmd = sys.argv[1]
    async with ExcalidrawClient("http://127.0.0.1:8000/mcp") as client:
        if cmd == "status":
            names = await client.list_tool_names()
            els = await client.call_json("query_elements", {})
            print(json.dumps({"ok": True, "tools": len(names), "elements": len(els)}))
            return 0
        plan = _decode(sys.argv[2])
        if cmd == "render":
            result = await render_layout_plan(plan, client, clear_before=True)
            if not result.success:
                print(json.dumps({"success": False, "errors": result.errors[:5],
                                  "rolled_back": result.rolled_back}, ensure_ascii=False))
                return 1
            report = await verify_scene(plan, client)
            print(json.dumps({"success": True, "created": len(result.created),
                              "verify": _compact(report)}, ensure_ascii=False))
            return 0
        if cmd == "verify":
            report = await verify_scene(plan, client)
            print(json.dumps(_compact(report), ensure_ascii=False))
            return 0
    print(json.dumps({"error": f"unknown command {cmd}"}))
    return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
