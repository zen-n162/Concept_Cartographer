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
from typing import Any, Callable

from cc_core.causal import apply_relation_policy
from cc_core.detail import build_multilevel_plan, check_level_bands, project
from cc_core.evaluation import summarize
from cc_core.gaps import detect_gaps
from cc_core.learning import (
    apply_learned,
    build_prompt_hints,
    load_learned,
    note_cues_kept,
)
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

ProgressFn = Callable[[str, str], None]
"""進捗フック: (stage_key, 日本語ラベル)。Web UI の進捗チェックリスト用。"""

# 進捗ステージ。UI 側が「未着手/実行中/完了」を描くための固定順序でもあるため、
# 並びを変えるときは cc_web/static/app.js の STAGES も揃えること。
STAGES: tuple[tuple[str, str], ...] = (
    ("routing", "経路判定"),
    ("ingest", "資料収集"),
    ("extract", "概念抽出"),
    ("relate", "関係の検証"),
    ("detail", "詳細度の計算"),
    ("gaps", "ギャップ検出"),
    ("render", "描画"),
    ("verify", "独立検証"),
    ("export", "出力"),
)
STAGE_LABELS: dict[str, str] = dict(STAGES)


def _notify(progress: ProgressFn | None, key: str) -> None:
    """進捗を通知する。表示都合の失敗で本処理を止めない (例外は握りつぶす)。"""
    if progress is None:
        return
    try:
        progress(key, STAGE_LABELS[key])
    except Exception as exc:  # pragma: no cover - 通知側の事故は本処理に無関係
        logger.debug("progress hook error: %s", type(exc).__name__)


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
    progress: ProgressFn | None = None,
    offline: bool = False,
    learned: bool = True,
) -> dict[str, Any]:
    """概念地図生成の全経路。

    progress: 各ステージ開始時に (key, 日本語ラベル) で呼ばれるフック。
    offline:  Foundry を一切呼ばない実行モード (Web の再描画・テスト用)。
              保存済み KG から詳細度計算以降だけを回すため kg_file が必須。
              LLM 抽出も因果の独立検証も無いので、結果は語彙証拠のみに基づく。
    learned:  過去の修正からの学習を適用するか (編集/学習設計書 §5.3)。
              False で ①抽出ヒント ②自動適用 ③因果上書き のすべてを止める。
              適用した場合は必ず summary["learned"] に内訳が出る (黙って直さない)。
    """
    if offline and not kg_file:
        raise ValueError("offline モードは kg_file が必須です (LLM 抽出を行わないため)")

    # offline では FoundryAgentsV2 を生成しない。生成だけで Azure 認証と
    # エージェント確保 (ensure_agents) が走り、閉域・テストで失敗するため。
    client = None if offline else FoundryAgentsV2()
    if client is not None:
        ensure_agents(client)
    executor = ToolExecutor(target=target)
    session = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    summary: dict[str, Any] = {"session": session, "target": target}
    if offline:
        summary["offline"] = True

    # ---- ⓪ Query Routing (v3 §4.1 / 計画 §6) ----
    _notify(progress, "routing")
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
    _notify(progress, "ingest")
    learned_store = load_learned() if learned else None
    if kg_file:
        kg, norm = normalize_kg(json.loads(Path(kg_file).read_text(encoding="utf-8")))
        summary["ingest"] = {"mode": "kg_file", "file": kg_file}
        if norm.repairs:
            summary["ingest"]["normalized"] = norm.to_dict()
        # 抽出済みの KG を読んだ時点で「概念抽出」は完了している (UI の
        # チェックリストが途中で止まって見えないよう、ここで通知する)
        _notify(progress, "extract")
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
        # フック 1: 過去の修正からの注意を抽出プロンプト末尾に足す (§5.3)。
        # エージェント定義 (agents_def) は変えない — バージョンを増殖させず、
        # 実行ごとに最新のヒントを使うため。
        hints = build_prompt_hints(learned_store)
        if hints:
            prompt += hints
            summary["learned_hints"] = hints.count("\n- ")

        if docs:
            prompt += f"\n=== 添付資料 ({len(docs)} 件) ===\n{bundle(docs)[:LOCAL_BUDGET]}"
        else:
            prompt += "\n(ローカル添付資料はありません)"

        logger.info("extraction start local_docs=%d workiq=%s", len(docs), not local_only)
        _notify(progress, "extract")
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

    # ---- フック 2: 学習の自動適用 (§5.3) ----
    # 改名辞書・除外リストを当て、因果上書きの印を付ける。**必ず内訳を返す**
    # ので、何を機械が直したかは常に summary から追える (黙って直さない)。
    kg, learned_report = apply_learned(kg, learned_store, enabled=bool(learned))
    summary["learned"] = learned_report

    # ---- ④ Relate: 因果3点セット + 矛盾の非断定化 (裁定 7) ----
    # フック 3: causal_override が付いた対は 3 点セットを走らせず確定させる
    # (apply_relation_policy が edge["causal_override"] を見る)。
    # offline は独立検証器 (別モデル判定) を持てないため verifier=None。
    # 3 点セットの 3 点目が欠けるので、通る因果は語彙証拠のみの根拠になる。
    _notify(progress, "relate")
    verifier = _causal_verifier(client) if (verify_causal and client) else None
    kg, causal_stats = apply_relation_policy(kg, verifier=verifier)
    summary["relation_policy"] = causal_stats

    # 因果として維持された語彙証拠を数える (§5.1 cue_stats)。R1 は記録のみで、
    # 閾値を超えた語彙の扱いは人が判断する (§12)。
    if learned:
        try:
            note_cues_kept([hit for e in kg.get("edges", [])
                            if e.get("glyph") == "arrow"
                            for hit in (e.get("causal_check") or {}).get("lexicon_hit", [])])
        except OSError as exc:  # 統計が書けなくても生成は続ける
            logger.warning("cue_stats not recorded: %s", type(exc).__name__)

    # ---- 原本 KG の保存 (編集の base) ----
    # **関係ポリシー適用後**を保存するのが要点。ここが利用者に見えている状態で
    # あり、編集はこの上に積まれる。ポリシー適用**前**を base にすると、
    # 1 か所の編集で rebuild したときに降格済みの相関が生の因果矢印へ戻り、
    # 3 点セット (裁定 7) が黙って無効化されてしまう。
    # kg_file 経由でもセッション固有の原本を残す — 原本が無いセッションは
    # cc_core.editing が「原本 + 追記ログ」で再構成できず、編集できないため。
    kg_path = Path("graphs") / f"kg_session_{session}.json"
    kg_path.parent.mkdir(exist_ok=True)
    kg_path.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["knowledge_graph"] = {
        "nodes": len(kg["nodes"]), "edges": len(kg.get("edges", [])),
        "communities": len(kg.get("communities", [])),
        "source_files": kg.get("source_files", []),
        "saved": str(kg_path),
    }

    # ---- 可変詳細度: 3 レベル同梱を 1 回で生成 (§4) ----
    _notify(progress, "detail")
    plan = build_multilevel_plan(kg, default_level=level,
                                 language=decision.language)
    band_problems = check_level_bands(plan)
    if band_problems:
        logger.warning("detail level band problems: %s", band_problems)
    summary["levels"] = plan["levels"]
    summary["band_check"] = band_problems or "OK"

    # ---- ギャップ候補 (裁定 8) ----
    _notify(progress, "gaps")
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
    _notify(progress, "render")
    view = project(plan, level)
    view_json = json.dumps(view, ensure_ascii=False)
    verdict: dict[str, Any] = {}
    if offline:
        # エージェントを介さず実行系を直接叩く。往復が無いので再試行も不要。
        render_status = executor("render_layout_plan", {"plan": view})
        if not render_status.get("success"):
            raise RuntimeError(f"projection failed: {render_status.get('errors')}")
        summary["projection"] = {
            "status": "RENDER_OK",
            "created": len(render_status.get("created", [])),
            "mode": render_status.get("mode", target),
        }
        _notify(progress, "verify")
        report = executor("verify_scene", {})
        verdict = {
            "verdict": "PASS" if report.get("passed") else "FAIL",
            "summary": (f"要素 {report.get('canvas_element_count', 0)} / 期待 "
                        f"{report.get('expected_element_count', 0)}"
                        f" (欠落 {len(report.get('missing_elements', []))} / "
                        f"ラベル不一致 {len(report.get('label_mismatches', []))})"),
        }
        summary["verification"] = verdict
    else:
        for attempt in (1, 2):
            render_status = extract_json(client.run(
                "cc-projection", "この layout_plan を描画:\n" + view_json,
                tool_executor=executor))
            summary["projection"] = render_status
            if render_status.get("status") != "RENDER_OK":
                raise RuntimeError(f"projection failed: {render_status}")

            _notify(progress, "verify")
            verdict = extract_json(client.run(
                "cc-verification",
                "直前に描画した layout_plan を検証してください。plan:\n" + view_json,
                tool_executor=executor))
            summary["verification"] = verdict
            if verdict.get("verdict") == "PASS":
                break
            logger.warning("verification FAIL (attempt %d)", attempt)

    # ---- 出力 ----
    _notify(progress, "export")
    if offline and target == "file":
        # file 経路の offline はローカルキャンバスへ描いていない。live canvas を
        # export すると別セッションの内容を書き出してしまうため plan から直接作る。
        from cc_core.excalidraw_file import write_scene
        Path("exports").mkdir(parents=True, exist_ok=True)
        summary["export"] = {"excalidraw": write_scene(
            view, f"exports/session_{session}.excalidraw")}
    else:
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
