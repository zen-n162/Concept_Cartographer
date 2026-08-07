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
        # パイプラインが確定させた plan。設定されている間は、エージェントが
        # ツール引数に復唱してきた plan を**信用しない** (LLM の復唱は島の欠落
        # など静かに壊れることがある【実測 2026-08-07: RENDER_FAILED】)。
        self.authoritative_plan: dict | None = None

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
        if self.authoritative_plan is not None:
            # 確定 plan がある間はそれだけを描く。復唱との差異は握り潰さず記録
            plan = self.authoritative_plan
            if passed is not None and passed != plan:
                logger.warning(
                    "render: エージェントの復唱が確定 plan と異なるため無視 "
                    "(nodes %s→%s / islands %s→%s)",
                    len(passed.get("nodes", []) or []), len(plan.get("nodes", [])),
                    len(passed.get("islands", []) or []), len(plan.get("islands", [])))
        # 取り違え防止 (KG を渡されたら直前の layout_plan を描く)
        elif self._looks_like_plan(passed):
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
        # 検証も確定 plan を正とする (復唱された plan で検証すると、壊れた復唱
        # どうしの突合になり検証の意味が消えるため)
        plan = _as_dict(self.authoritative_plan or args.get("plan") or self.last_plan)
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
        """.excalidraw を書き出す。

        target=file は **MCP を呼ばない** — file モードは「MCP なしで成立する」
        のが設計契約 (計画 §3-2 の fallback 要件) で、ここだけライブゲートウェイに
        依存すると、canvas 停止時に出力が欠け、テストも外部状態で揺れる
        【実測 2026-08-07: 稼働中ゲートウェイの状態次第で asyncio が壊れた】。
        描いた計画 (authoritative_plan / last_plan) から直接生成する。
        """
        from pathlib import Path

        from cc_core.mcp_client import extract_json

        if self.target == "file":
            plan = self.authoritative_plan or self.last_plan
            if plan is None:
                logger.warning("export skipped: plan がありません (file mode)")
                return None
            from cc_core.excalidraw_file import write_scene
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            return str(write_scene(plan, out_path))

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
