"""エージェントの function tool をローカルで実行する層。

layout 系 (compute_layout / validate_layout_plan) は常にローカルの決定的コード。
描画・検証 (render_layout_plan / verify_scene / describe_scene) は target により:
  - "local": ローカルの Excalidraw MCP (127.0.0.1:8000/mcp)
  - "vm":    VM-Excalidraw-MCP (az vm run-command 中継; 併せてローカルにもミラー描画
             して手元のブラウザで見られるようにする)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from cc_core import adapter, layout, validate, verify
from cc_core.logging_util import get_logger
from cc_core.mcp_client import ExcalidrawClient
from cc_orchestrator import vm_relay

logger = get_logger("cc_orchestrator.tools")

LOCAL_MCP = "http://127.0.0.1:8000/mcp"


def _as_dict(value: Any) -> dict:
    if isinstance(value, str):
        return json.loads(value)
    return value


class ToolExecutor:
    """target ('local' | 'vm') に応じてツールを実行。最後に使った plan を保持する。"""

    def __init__(self, target: str = "vm") -> None:
        assert target in ("local", "vm")
        self.target = target
        self.last_plan: dict | None = None
        self.last_render: dict | None = None
        self.local_available = True

    # -- entry point (FoundryAgents.run_agent から呼ばれる) --
    def __call__(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"tool_{name}", None)
        if handler is None:
            return {"error": f"unknown tool {name}"}
        return handler(args)

    # -- layout 系 (常にローカル決定的コード) --
    def tool_compute_layout(self, args: dict) -> dict:
        kg = _as_dict(args["knowledge_graph"])
        plan = layout.compute_layout(kg, args.get("detail_level", "standard"))
        self.last_plan = plan
        return plan

    def tool_validate_layout_plan(self, args: dict) -> dict:
        plan = _as_dict(args["plan"])
        self.last_plan = plan
        return validate.validate_layout_plan(plan).to_dict()

    # -- 描画系 --
    def tool_render_layout_plan(self, args: dict) -> dict:
        plan = _as_dict(args["plan"])
        self.last_plan = plan
        if self.target == "vm":
            result = vm_relay.vm_call("render", plan)
            self._mirror_local(plan)  # 手元で見るためのミラー (best-effort)
            self.last_render = result
            return result
        result = asyncio.run(self._local_render(plan))
        self.last_render = result
        return result

    def tool_verify_scene(self, args: dict) -> dict:
        plan = _as_dict(args.get("plan") or self.last_plan)
        if self.target == "vm":
            # render 時の VM 内検証結果があればそれを一次情報として返す
            if self.last_render and "verify" in self.last_render:
                return self.last_render["verify"]
            return vm_relay.vm_call("verify", plan)
        return asyncio.run(self._local_verify(plan))

    def tool_describe_scene(self, args: dict) -> dict:
        if self.target == "vm":
            if self.last_render and "verify" in self.last_render:
                return {"describe_scene": self.last_render["verify"].get(
                    "describe_scene_head", "")}
            return {"describe_scene": vm_relay.vm_call(
                "verify", self.last_plan or {}).get("describe_scene_head", "")}
        async def _run() -> dict:
            async with ExcalidrawClient(LOCAL_MCP) as client:
                return {"describe_scene": (await client.call("describe_scene"))[:1500]}
        return asyncio.run(_run())

    # -- helpers --
    async def _local_render(self, plan: dict) -> dict:
        async with ExcalidrawClient(LOCAL_MCP) as client:
            result = await adapter.render_layout_plan(plan, client, clear_before=True)
            return result.to_dict()

    async def _local_verify(self, plan: dict) -> dict:
        async with ExcalidrawClient(LOCAL_MCP) as client:
            report = await verify.verify_scene(plan, client)
            report["describe_scene"] = str(report.get("describe_scene", ""))[:1200]
            return report

    def _mirror_local(self, plan: dict) -> None:
        if not self.local_available:
            return
        try:
            asyncio.run(self._local_render(plan))
            logger.info("mirrored scene to local canvas (127.0.0.1:3000)")
        except Exception as exc:
            self.local_available = False
            logger.warning("local mirror skipped: %s", type(exc).__name__)

    # -- export (パイプライン終端で直接呼ぶ; エージェント経由ではない) --
    def export_excalidraw(self, out_path: str) -> str | None:
        """ローカルキャンバス (ミラー済み) から .excalidraw を書き出す。"""
        from pathlib import Path

        from cc_core.mcp_client import extract_json

        async def _run() -> str:
            async with ExcalidrawClient(LOCAL_MCP) as client:
                scene = extract_json(await client.call("export_scene"))
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_text(
                json.dumps(scene, ensure_ascii=False, indent=2), encoding="utf-8")
            return out_path
        try:
            return asyncio.run(_run())
        except Exception as exc:
            logger.warning("export skipped: %s", type(exc).__name__)
            return None
