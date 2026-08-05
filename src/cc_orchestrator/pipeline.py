"""cartographer-workflow の実行 (workflows/cartographer_workflow.yaml の順序を実装)。

  ingest -> cc-extraction -> cc-layout -> cc-projection -> cc-verification
  (FAIL 時は projection を 1 回だけ再試行)

エージェントの頭脳は Foundry Agent Service (gpt-5.6 sol/terra/luna)、
描画は target に応じてローカル MCP または VM-Excalidraw-MCP。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from cc_core.logging_util import get_logger
from cc_core.mcp_client import extract_json
from cc_core.validate import validate_layout_plan
from cc_orchestrator.agents_def import AGENT_SPECS
from cc_orchestrator.foundry_agents import FoundryAgents
from cc_orchestrator.ingest import bundle, ingest
from cc_orchestrator.tool_exec import ToolExecutor

logger = get_logger("cc_orchestrator.pipeline")


def ensure_agents(client: FoundryAgents) -> dict[str, str]:
    """4 エージェントを作成/更新して name -> id を返す。"""
    ids = {}
    for name, spec in AGENT_SPECS.items():
        ids[name] = client.ensure_agent(
            name, spec["model"], spec["instructions"], spec["tools"])
    return ids


def run_pipeline(
    message: str,
    *,
    target: str = "vm",
    paths: list[str] | None = None,
    kg_file: str | None = None,
) -> dict[str, Any]:
    client = FoundryAgents()
    agents = ensure_agents(client)
    executor = ToolExecutor(target=target)
    session = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    summary: dict[str, Any] = {"session": session, "target": target}

    # ---- ⓪/① Ingest (kg 直接指定ならスキップ) ----
    if kg_file:
        kg = json.loads(Path(kg_file).read_text(encoding="utf-8"))
        summary["ingest"] = {"mode": "kg_file", "file": kg_file}
    else:
        docs, window = ingest(message, paths or [])
        summary["ingest"] = {
            "window": window,
            "files": [{"name": d.name, "source": d.source,
                       "modified": d.modified.strftime("%Y-%m-%d")} for d in docs],
        }
        if not docs:
            summary["status"] = "no_documents"
            summary["hint"] = ("対象期間内の資料が見つかりません。inbox/ に PDF/docx/md/txt を"
                               "置くか --path で資料フォルダを指定してください。")
            return summary

        # ---- ② Extraction (gpt-5.6-sol) ----
        logger.info("extraction start files=%d", len(docs))
        reply = client.run_agent(
            agents["cc-extraction"],
            f"依頼: {message}\n\n以下の研究資料から knowledge_graph JSON を抽出:\n\n{bundle(docs)}",
            json_response=True, fallback_spec=AGENT_SPECS["cc-extraction"])
        kg = extract_json(reply)
        if not kg.get("nodes"):
            raise RuntimeError("extraction returned no nodes")
        summary["knowledge_graph"] = {
            "nodes": len(kg.get("nodes", [])), "edges": len(kg.get("edges", [])),
            "communities": len(kg.get("communities", [])),
        }
        kg_path = Path("graphs") / f"kg_session_{session}.json"
        kg_path.parent.mkdir(exist_ok=True)
        kg_path.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["knowledge_graph"]["saved"] = str(kg_path)

    # ---- ③ Layout (gpt-5.6-luna, ツールは決定的コード) ----
    reply = client.run_agent(
        agents["cc-layout"],
        "この knowledge_graph の layout_plan を作成:\n" + json.dumps(kg, ensure_ascii=False),
        tool_executor=executor, json_response=True,
        fallback_spec=AGENT_SPECS["cc-layout"])
    layout_status = extract_json(reply)
    if layout_status.get("status") != "LAYOUT_OK" or executor.last_plan is None:
        raise RuntimeError(f"layout failed: {layout_status}")
    plan = executor.last_plan
    check = validate_layout_plan(plan)  # 防御的再検証
    if not check.valid:
        raise RuntimeError(f"layout plan invalid: {check.errors[:3]}")
    plan_path = Path("graphs") / f"layout_plan_session_{session}.json"
    plan_path.parent.mkdir(exist_ok=True)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["layout"] = {**layout_status, "saved": str(plan_path)}

    # ---- ④⑤ Projection + Verification (FAIL 時 1 回再試行) ----
    plan_json = json.dumps(plan, ensure_ascii=False)
    verdict: dict[str, Any] = {}
    for attempt in (1, 2):
        reply = client.run_agent(
            agents["cc-projection"],
            "この layout_plan を描画:\n" + plan_json,
            tool_executor=executor, json_response=True,
            fallback_spec=AGENT_SPECS["cc-projection"])
        render_status = extract_json(reply)
        summary["projection"] = render_status
        if render_status.get("status") != "RENDER_OK":
            raise RuntimeError(f"projection failed: {render_status}")

        reply = client.run_agent(
            agents["cc-verification"],
            "直前に描画した layout_plan を検証してください。plan:\n" + plan_json,
            tool_executor=executor, json_response=True,
            fallback_spec=AGENT_SPECS["cc-verification"])
        verdict = extract_json(reply)
        summary["verification"] = verdict
        if verdict.get("verdict") == "PASS":
            break
        logger.warning("verification FAIL (attempt %d)", attempt)

    # ---- ⑥ Export (ローカルキャンバスから) ----
    export = executor.export_excalidraw(f"exports/session_{session}.excalidraw")
    summary["export"] = export
    summary["status"] = "success" if verdict.get("verdict") == "PASS" else "verify_failed"
    summary["view"] = {
        "local_canvas": "http://127.0.0.1:3000",
        "vm_canvas": "VM 内 127.0.0.1:3000 (Bastion 接続時)" if target == "vm" else None,
    }
    return summary
