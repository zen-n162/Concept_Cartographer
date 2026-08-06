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

logger = get_logger("cc_orchestrator.tools")

LOCAL_MCP = "http://127.0.0.1:8000/mcp"


def _as_dict(value: Any) -> dict:
    if isinstance(value, str):
        return json.loads(value)
    return value


class ToolExecutor:
    """target ('local' | 'vm') に応じてツールを実行。最後に使った plan を保持する。"""

    def __init__(self, target: str = "local") -> None:
        assert target in ("local", "file")
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

    @staticmethod
    def _looks_like_plan(obj: Any) -> bool:
        """layout_plan か否か (knowledge_graph と取り違えられても気づけるように)。"""
        return (isinstance(obj, dict) and "islands" in obj and "nodes" in obj
                and isinstance(obj.get("nodes"), list) and obj["nodes"]
                and "x" in obj["nodes"][0])

    def tool_validate_layout_plan(self, args: dict) -> dict:
        """layout_plan を検証する。

        エージェントが knowledge_graph を渡してくることがあるため、layout_plan で
        なければ直前の compute_layout 結果を検証する。また **last_plan を上書き
        しない** — 描画対象は compute_layout / render が決めた物を正とする。
        """
        passed = _as_dict(args["plan"]) if args.get("plan") is not None else None
        if self._looks_like_plan(passed):
            plan = passed
        elif self.last_plan is not None:
            logger.info("validate: layout_plan 以外が渡されたため直前の計算結果を検証")
            plan = self.last_plan
        else:
            return {"valid": False,
                    "errors": ["先に compute_layout を呼んで layout_plan を作ってください"],
                    "warnings": []}
        return validate.validate_layout_plan(plan).to_dict()

    # -- 描画系 --
    def tool_render_layout_plan(self, args: dict) -> dict:
        passed = _as_dict(args["plan"]) if args.get("plan") is not None else None
        # 描画対象も取り違えを防ぐ (KG を渡されたら直前の layout_plan を描く)
        if self._looks_like_plan(passed):
            plan = passed
        elif self.last_plan is not None:
            logger.info("render: layout_plan 以外が渡されたため直前の計算結果を描画")
            plan = self.last_plan
        else:
            return {"success": False,
                    "errors": ["先に compute_layout を呼んで layout_plan を作ってください"]}
        self.last_plan = plan
        if self.target == "file":
            # MCP を使わずファイルへ直接書き出す (ACA 到達前の fallback)
            from cc_core.excalidraw_file import build_scene
            scene = build_scene(plan)
            result = {"success": True, "created": [e["id"] for e in scene["elements"]],
                      "element_map": {e["id"]: e["id"] for e in scene["elements"]},
                      "errors": [], "rolled_back": False, "mode": "file"}
            self.last_render = result
            return result
        result = asyncio.run(self._local_render(plan))
        self.last_render = result
        return result

    def tool_verify_scene(self, args: dict) -> dict:
        plan = _as_dict(args.get("plan") or self.last_plan)
        if self.target == "file":
            # ファイル経路では生成済みシーンと計画を突合する (MCP 不要)
            from cc_core.excalidraw_file import build_scene
            from cc_core.verify import verify_scene_offline
            return verify_scene_offline(plan, build_scene(plan))
        return asyncio.run(self._local_verify(plan))

    def tool_describe_scene(self, args: dict) -> dict:
        if self.target == "file":
            plan = self.last_plan or {}
            return {"describe_scene":
                    f"nodes={len(plan.get('nodes', []))} "
                    f"edges={len(plan.get('edges', []))} "
                    f"islands={len(plan.get('islands', []))} (file mode)"}
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
