"""cartographer-workflow の実行 (workflows/cartographer_workflow.yaml の順序を実装)。

  cc-extraction (Work IQ で資料収集 + KG 抽出)
    -> cc-layout (compute_layout)
    -> cc-projection (render_layout_plan)
    -> cc-verification (verify_scene)   FAIL 時は projection を 1 回だけ再試行

エージェントは Foundry の新 Agents API (kind: prompt, gpt-5.6 sol/luna/terra)。
資料は Work IQ (OneDrive/SharePoint/Copilot) から Foundry 側で読む。ローカルの
inbox/--path に資料があれば補助入力として一緒に渡す。
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
from cc_orchestrator.foundry_v2 import FoundryAgentsV2
from cc_orchestrator.ingest import bundle, ingest
from cc_orchestrator.tool_exec import ToolExecutor

logger = get_logger("cc_orchestrator.pipeline")

LOCAL_BUDGET = 40000


def ensure_agents(client: FoundryAgentsV2) -> dict[str, str]:
    """4 エージェントを作成/更新 (既存なら新バージョン) して name -> name を返す。"""
    names = {}
    for name, spec in AGENT_SPECS.items():
        names[name] = client.ensure_agent(
            name, spec["model"], spec["instructions"], spec["tools"],
            effort=spec.get("effort", "medium"),
            description=spec.get("description", ""),
            welcome=spec.get("welcome"))
    return names


def run_pipeline(
    message: str,
    *,
    target: str = "vm",
    paths: list[str] | None = None,
    kg_file: str | None = None,
    local_only: bool = False,
) -> dict[str, Any]:
    client = FoundryAgentsV2()
    ensure_agents(client)
    executor = ToolExecutor(target=target)
    session = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    summary: dict[str, Any] = {"session": session, "target": target}

    # ---- ①② Ingest + Extraction ----
    if kg_file:
        kg = json.loads(Path(kg_file).read_text(encoding="utf-8"))
        summary["ingest"] = {"mode": "kg_file", "file": kg_file}
    else:
        docs, window = ingest(message, paths or [])  # ローカル資料 (任意)
        summary["ingest"] = {
            "window": window,
            "local_files": [{"name": d.name, "modified": d.modified.strftime("%Y-%m-%d")}
                            for d in docs],
            "workiq": "disabled" if local_only else "enabled",
        }
        prompt = (
            f"依頼: {message}\n"
            f"今日は {dt.datetime.now():%Y-%m-%d (%a)} です。対象期間: {window}。\n"
        )
        if local_only:
            prompt += "\nWork IQ ツールは使わず、以下の添付資料のみから抽出してください。\n"
        else:
            prompt += ("\nWork IQ ツールで OneDrive / SharePoint から対象期間の研究資料を"
                       "収集し、下の添付資料と併せて knowledge_graph を抽出してください。\n")
        if docs:
            prompt += f"\n=== 添付資料 ({len(docs)} 件) ===\n{bundle(docs)[:LOCAL_BUDGET]}"
        else:
            prompt += "\n(ローカル添付資料はありません)"

        logger.info("extraction start local_docs=%d workiq=%s", len(docs), not local_only)
        kg = extract_json(client.run(
            "cc-extraction", prompt, tool_executor=None))
        if kg.get("error") == "no_documents":
            summary["status"] = "no_documents"
            summary["hint"] = (
                f"対象期間の研究資料が見つかりません ({kg.get('detail', '')})。"
                "inbox/ に資料を置くか --path で指定してください。")
            return summary
        if not kg.get("nodes"):
            raise RuntimeError(f"extraction returned no nodes: {str(kg)[:200]}")
        summary["knowledge_graph"] = {
            "nodes": len(kg.get("nodes", [])), "edges": len(kg.get("edges", [])),
            "communities": len(kg.get("communities", [])),
            "source_files": kg.get("source_files", []),
        }
        kg_path = Path("graphs") / f"kg_session_{session}.json"
        kg_path.parent.mkdir(exist_ok=True)
        kg_path.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["knowledge_graph"]["saved"] = str(kg_path)

    # ---- ③ Layout ----
    layout_status = extract_json(client.run(
        "cc-layout",
        "この knowledge_graph の layout_plan を作成:\n" + json.dumps(kg, ensure_ascii=False),
        tool_executor=executor))
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
        render_status = extract_json(client.run(
            "cc-projection", "この layout_plan を描画:\n" + plan_json,
            tool_executor=executor))
        summary["projection"] = render_status
        if render_status.get("status") != "RENDER_OK":
            raise RuntimeError(f"projection failed: {render_status}")

        verdict = extract_json(client.run(
            "cc-verification",
            "直前に描画した layout_plan を検証してください。plan:\n" + plan_json,
            tool_executor=executor))
        summary["verification"] = verdict
        if verdict.get("verdict") == "PASS":
            break
        logger.warning("verification FAIL (attempt %d)", attempt)

    # ---- ⑥ Export ----
    summary["export"] = executor.export_excalidraw(f"exports/session_{session}.excalidraw")
    summary["status"] = "success" if verdict.get("verdict") == "PASS" else "verify_failed"
    summary["view"] = {
        "local_canvas": "http://127.0.0.1:3000",
        "vm_canvas": "VM 内 127.0.0.1:3000 (Bastion 接続時)" if target == "vm" else None,
    }
    return summary
