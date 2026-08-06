"""実運用版パイプライン (R1)。

  ⓪Routing → ①Ingest → ③Concept(抽出) → ④Relate(因果3点セット/矛盾非断定)
  → 可変詳細度(3レベル同梱) → ギャップ検出 → ⑧Project(描画) → 検証 → 評価

PoC からの主な変更 (実運用計画):
- ⓪ Query Routing を入口に置く (§6)。地図生成でない要求はフルパイプラインを回さない
- 可変詳細度: 生成は 1 回、3 レベルを同梱して切替は再計算なし (§4)
- 因果は 3 点セット通過時のみ、矛盾は R1 では非断定へ降格 (裁定 7)
- ギャップは 4 点メタデータ付き候補 + confirm/dismiss (裁定 8)
- run-command 中継は廃止。描画先は MCP か .excalidraw/SVG 直接生成 (§3-2)
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from cc_core.causal import apply_relation_policy
from cc_core.detail import build_multilevel_plan, check_level_bands, project
from cc_core.evaluation import summarize
from cc_core.gaps import detect_gaps
from cc_core.logging_util import get_logger
from cc_core.mcp_client import extract_json
from cc_core.normalize import normalize_kg
from cc_core.svg_export import write_svg
from cc_core.validate import validate_layout_plan
from cc_orchestrator.agents_def import AGENT_SPECS
from cc_orchestrator.foundry_v2 import FoundryAgentsV2
from cc_orchestrator.ingest import bundle, ingest
from cc_orchestrator.routing import RouteDecision, route
from cc_orchestrator.tool_exec import ToolExecutor

logger = get_logger("cc_orchestrator.pipeline")

LOCAL_BUDGET = 40000


def ensure_agents(client: FoundryAgentsV2) -> dict[str, str]:
    names = {}
    for name, spec in AGENT_SPECS.items():
        names[name] = client.ensure_agent(
            name, spec["model"], spec["instructions"], spec["tools"],
            effort=spec.get("effort", "medium"),
            description=spec.get("description", ""),
            welcome=spec.get("welcome"))
    return names


def _causal_verifier(client: FoundryAgentsV2):
    """独立検証器 (裁定 7 の 3 点目)。描画検証と同じ「別モデル判定」パターン。

    cc-verification (gpt-5.6-terra) に因果の可否だけを判定させる。抽出側
    (gpt-5.6-sol) とは別モデルなので、同一モデルの自己確認にならない。
    """
    def verify(edge: dict[str, Any], evidence_text: str) -> bool:
        prompt = (
            "次の関係が『因果』と言えるか判定してください。\n"
            "因果と認めるのは、根拠テキストに機序の記述・介入・反事実の"
            "いずれかが**明示**されている場合のみです。相関・併存・時間的前後"
            "だけでは因果と認めません。\n"
            f"関係: {edge.get('from')} → {edge.get('to')} 「{edge.get('label', '')}」\n"
            f"根拠テキスト: {evidence_text[:600]}\n"
            'JSON のみで回答: {"causal": true|false, "why": "<30字以内>"}'
        )
        try:
            res = extract_json(client.run("cc-verification", prompt))
            return bool(res.get("causal"))
        except Exception as exc:
            logger.warning("causal verifier error: %s", type(exc).__name__)
            raise
    return verify


def run_pipeline(
    message: str,
    *,
    target: str = "local",
    paths: list[str] | None = None,
    kg_file: str | None = None,
    local_only: bool = False,
    detail_level: str | None = None,
    verify_causal: bool = True,
    export_svg: bool = True,
) -> dict[str, Any]:
    client = FoundryAgentsV2()
    ensure_agents(client)
    executor = ToolExecutor(target=target)
    session = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    summary: dict[str, Any] = {"session": session, "target": target}

    # ---- ⓪ Query Routing (v3 §4.1 / 計画 §6) ----
    decision: RouteDecision = route(message)
    summary["routing"] = decision.to_dict()
    level = detail_level or decision.detail_level or "standard"
    summary["detail_level"] = level

    if decision.route != "map" and not kg_file:
        # 地図生成でない要求にフルパイプラインを回さない (コスト・時間の一次緩和)
        agent = "cc-extraction"  # R1 は KB 検索役を兼ねる
        prompt = (message if decision.route == "basic"
                  else f"次の質問に、必要なら Work IQ / KB で調べて簡潔に答えてください:\n{message}")
        summary["answer"] = client.run(agent, prompt)
        summary["status"] = "answered"
        logger.info("routed to %s (no map generation)", decision.route)
        return summary

    # ---- ①② Ingest + Extraction ----
    if kg_file:
        kg, norm = normalize_kg(json.loads(Path(kg_file).read_text(encoding="utf-8")))
        summary["ingest"] = {"mode": "kg_file", "file": kg_file}
        if norm.repairs:
            summary["ingest"]["normalized"] = norm.to_dict()
    else:
        docs, window = ingest(message, paths or [])
        summary["ingest"] = {
            "window": window,
            "local_files": [{"name": d.name, "modified": d.modified.strftime("%Y-%m-%d")}
                            for d in docs],
            "workiq": "disabled" if local_only else "enabled",
        }
        lang_note = ""
        if decision.language == "en":
            lang_note = "\nラベルは英語で出力してください。"
        elif decision.language == "ja":
            lang_note = "\nラベルは日本語で出力してください。"
        tag_note = (f"\n対象を次のタグに絞ってください: {', '.join(decision.tags)}"
                    if decision.tags else "")

        prompt = (
            f"依頼: {message}\n"
            f"今日は {dt.datetime.now():%Y-%m-%d (%a)} です。対象期間: {window}。"
            f"{lang_note}{tag_note}\n"
        )
        if local_only:
            prompt += "\nWork IQ ツールは使わず、以下の添付資料のみから抽出してください。\n"
        else:
            prompt += ("\nWork IQ ツールで OneDrive / SharePoint から対象期間の研究資料を"
                       "収集し、下の添付資料と併せて knowledge_graph を抽出してください。\n")
        prompt += (
            "\n重要: 各エッジには evidence_span を **配列** で付けてください。\n"
            '  "evidence_span": [{"document_id": "<ファイルID>", '
            '"surface": "<原文のままの引用>"}]\n'
            "surface は要約せず原文のまま入れてください (後段で因果の語彙証拠を"
            "検査するため)。文字位置が分かる場合のみ char_start / char_end を"
            "整数で 追加してください。分からなければ省略して構いません。\n"
        )
        if docs:
            prompt += f"\n=== 添付資料 ({len(docs)} 件) ===\n{bundle(docs)[:LOCAL_BUDGET]}"
        else:
            prompt += "\n(ローカル添付資料はありません)"

        logger.info("extraction start local_docs=%d workiq=%s", len(docs), not local_only)
        kg = extract_json(client.run("cc-extraction", prompt))
        if kg.get("error") == "no_documents":
            summary["status"] = "no_documents"
            summary["hint"] = (
                f"対象期間の研究資料が見つかりません ({kg.get('detail', '')})。"
                "inbox/ に資料を置くか --path で指定してください。")
            return summary
        if not kg.get("nodes"):
            raise RuntimeError(f"extraction returned no nodes: {str(kg)[:200]}")

        # LLM 出力は指示どおりの形とは限らない。契約形へ正規化してから先へ渡す
        # (実測: evidence_span を単一オブジェクトで返す / char offset が null)
        kg, norm = normalize_kg(kg)
        if norm.repairs or norm.warnings:
            summary["normalized"] = norm.to_dict()
            logger.info("kg normalized: %s", norm.repairs)

        kg_path = Path("graphs") / f"kg_session_{session}.json"
        kg_path.parent.mkdir(exist_ok=True)
        kg_path.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["knowledge_graph"] = {
            "nodes": len(kg["nodes"]), "edges": len(kg.get("edges", [])),
            "communities": len(kg.get("communities", [])),
            "source_files": kg.get("source_files", []),
            "saved": str(kg_path),
        }

    # ---- ④ Relate: 因果3点セット + 矛盾の非断定化 (裁定 7) ----
    verifier = _causal_verifier(client) if verify_causal else None
    kg, causal_stats = apply_relation_policy(kg, verifier=verifier)
    summary["relation_policy"] = causal_stats

    # ---- 可変詳細度: 3 レベル同梱を 1 回で生成 (§4) ----
    plan = build_multilevel_plan(kg, default_level=level,
                                 language=decision.language)
    band_problems = check_level_bands(plan)
    if band_problems:
        logger.warning("detail level band problems: %s", band_problems)
    summary["levels"] = plan["levels"]
    summary["band_check"] = band_problems or "OK"

    # ---- ギャップ候補 (裁定 8) ----
    gap_list = detect_gaps(kg)
    plan["gaps"] = [g.to_dict() for g in gap_list]
    summary["gaps"] = {
        "candidates": len(gap_list),
        "by_type": {t: sum(1 for g in gap_list if g.presumed_type == t)
                    for t in ("data", "extraction", "true", "unknown")},
    }

    check = validate_layout_plan(plan)
    if not check.valid:
        raise RuntimeError(f"layout plan invalid: {check.errors[:3]}")
    plan_path = Path("graphs") / f"layout_plan_session_{session}.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["layout"] = {"saved": str(plan_path)}

    # ---- ⑧ Project: 既定レベルを描画 + 検証 (FAIL 時 1 回再試行) ----
    view = project(plan, level)
    view_json = json.dumps(view, ensure_ascii=False)
    verdict: dict[str, Any] = {}
    for attempt in (1, 2):
        render_status = extract_json(client.run(
            "cc-projection", "この layout_plan を描画:\n" + view_json,
            tool_executor=executor))
        summary["projection"] = render_status
        if render_status.get("status") != "RENDER_OK":
            raise RuntimeError(f"projection failed: {render_status}")

        verdict = extract_json(client.run(
            "cc-verification",
            "直前に描画した layout_plan を検証してください。plan:\n" + view_json,
            tool_executor=executor))
        summary["verification"] = verdict
        if verdict.get("verdict") == "PASS":
            break
        logger.warning("verification FAIL (attempt %d)", attempt)

    # ---- 出力 ----
    summary["export"] = {"excalidraw": executor.export_excalidraw(
        f"exports/session_{session}.excalidraw")}
    if export_svg:
        svgs = {}
        for lv in ("overview", "standard", "detailed"):
            svgs[lv] = str(write_svg(project(plan, lv),
                                     f"exports/session_{session}_{lv}.svg"))
        summary["export"]["svg"] = svgs

    summary["kpi"] = summarize(view, [])
    summary["status"] = "success" if verdict.get("verdict") == "PASS" else "verify_failed"
    summary["view"] = {"local_canvas": "http://127.0.0.1:3000"}
    return summary


def switch_level(plan_path: str, level: str) -> dict[str, Any]:
    """保存済み plan の詳細度を切り替える (LLM 呼び出しゼロ・再レイアウトなし)。

    v3 §2.4 の「切替は再生成を伴わずクライアント側で完結」に対応する入口。
    """
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    view = project(plan, level)
    return view
