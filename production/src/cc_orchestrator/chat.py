"""Concept Cartographer 実運用版 チャット CLI (R1)。

例:
  # 地図生成 (詳細度は依頼文から自動判定。既定 standard)
  python -m cc_orchestrator.chat "今週の研究を概念地図として整理して"
  python -m cc_orchestrator.chat "今月の研究をざっくり全体像で"        # -> overview
  python -m cc_orchestrator.chat "直近30日を詳しく" --level detailed

  # 生成済み地図の詳細度を切り替える (LLM 呼び出しゼロ・再レイアウトなし)
  python -m cc_orchestrator.chat --switch graphs/layout_plan_session_X.json --level overview

  # 集約ノードを展開する (ドリルダウン)
  python -m cc_orchestrator.chat --expand agg-comm_001 --plan graphs/layout_plan_session_X.json

  # ギャップ候補の確定 (confirm / dismiss)
  python -m cc_orchestrator.chat --gap-list --plan <plan.json>
  python -m cc_orchestrator.chat --gap-confirm gap-isolated-c003 --plan <plan.json>

  # エージェント登録/更新
  python -m cc_orchestrator.chat --setup-agents
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cc_core.community import expand_aggregate
from cc_core.detail import project
from cc_core.gaps import apply_decision, usefulness_rate
from cc_core.svg_export import write_svg
from cc_orchestrator.foundry_v2 import FoundryAgentsV2
from cc_orchestrator.pipeline import ensure_agents, run_pipeline

LEVELS = ("overview", "standard", "detailed")


def _print_summary(s: dict) -> None:
    print()
    if s.get("status") == "answered":
        print(f"💬 [{s['routing']['route']} 経路] {s['routing']['rationale']}")
        print(s.get("answer", ""))
        return
    if s.get("status") == "no_documents":
        print(f"⚠ {s['hint']}")
        return

    r = s.get("routing", {})
    print(f"🧭 経路: {r.get('route')} ({r.get('rationale')})"
          f"{' / 言語=' + r['language'] if r.get('language') else ''}"
          f"{' / タグ=' + ','.join(r['tags']) if r.get('tags') else ''}")
    ing = s.get("ingest", {})
    if "window" in ing:
        wq = "Work IQ 有効" if ing.get("workiq") == "enabled" else "ローカルのみ"
        print(f"📄 取込 ({ing.get('window')} / {wq})")
        for f in ing.get("local_files", []):
            print(f"   - {f['name']}  [local, {f['modified']}]")
    kgs = s.get("knowledge_graph", {})
    if kgs:
        for sf in kgs.get("source_files", []):
            print(f"   - {sf}  [Work IQ]")
        print(f"🧠 抽出: 概念 {kgs.get('nodes')} / 関係 {kgs.get('edges')}"
              f" / 島 {kgs.get('communities')}")
    rp = s.get("relation_policy", {})
    if rp:
        print(f"🔬 関係検証: 因果を維持 {rp.get('causal_kept')} / "
              f"相関へ降格 {rp.get('causal_demoted')} / "
              f"矛盾を非断定化 {rp.get('contradiction_demoted')}")
    lv = s.get("levels", {})
    if lv:
        cur = s.get("detail_level")
        cells = "  ".join(
            f"{'▶' if k == cur else ' '}{k}={v['nodes']}"
            + (f"(集約{v['aggregates']})" if v.get("aggregates") else "")
            for k, v in lv.items())
        print(f"🔍 詳細度: {cells}   帯検査: {s.get('band_check')}")
    gp = s.get("gaps", {})
    if gp:
        print(f"❓ ギャップ候補: {gp['candidates']} 件 {gp['by_type']}")
    ver = s.get("verification", {})
    mark = "✅" if s.get("status") == "success" else "❌"
    print(f"{mark} 検証: {ver.get('verdict')}  {ver.get('summary', '')}")
    kpi = s.get("kpi", {})
    if kpi:
        ed = kpi.get("evidence_display", {})
        ca = kpi.get("causal", {})
        print(f"📊 KPI: evidence表示率={ed.get('rate')} (目標{ed.get('target')}) / "
              f"因果候補{ca.get('causal_candidates')}件中{ca.get('kept_as_causal')}件維持")
    ex = s.get("export", {})
    if ex.get("excalidraw"):
        print(f"💾 {ex['excalidraw']}")
    for k, v in (ex.get("svg") or {}).items():
        print(f"   SVG[{k}]: {v}")
    print(f"🖼  閲覧: {s.get('view', {}).get('local_canvas')}")


def _load_plan(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser(description="Concept Cartographer (実運用版 R1)")
    ap.add_argument("message", nargs="?", help="依頼文 (省略時は対話モード)")
    ap.add_argument("--level", choices=LEVELS, default=None,
                    help="詳細度 (省略時は依頼文から判定、既定 standard)")
    ap.add_argument("--target", choices=["local", "file"], default="local",
                    help="描画先: local=Excalidraw MCP / file=ファイル出力のみ")
    ap.add_argument("--path", action="append", default=[], help="資料フォルダ (複数可)")
    ap.add_argument("--kg", default=None, help="抽出をスキップし knowledge_graph を使用")
    ap.add_argument("--local-only", action="store_true", help="Work IQ を使わない")
    ap.add_argument("--no-causal-verify", action="store_true",
                    help="因果の独立検証を省く (語彙証拠のみで判定)")
    ap.add_argument("--no-svg", action="store_true", help="SVG 出力を省く")
    ap.add_argument("--setup-agents", action="store_true", help="エージェント登録/更新")
    # --- 生成済み plan への操作 (LLM 不要) ---
    ap.add_argument("--plan", default=None, help="操作対象の layout_plan.json")
    ap.add_argument("--switch", default=None, metavar="PLAN",
                    help="詳細度を切り替えて SVG 出力 (--level と併用)")
    ap.add_argument("--expand", default=None, metavar="AGG_ID",
                    help="集約ノードを展開してメンバーを表示")
    ap.add_argument("--gap-list", action="store_true", help="ギャップ候補を一覧")
    ap.add_argument("--gap-confirm", default=None, metavar="GAP_ID")
    ap.add_argument("--gap-dismiss", default=None, metavar="GAP_ID")
    ap.add_argument("--user", default="local-user", help="確定操作の記録者")
    args = ap.parse_args()

    if args.setup_agents:
        print(json.dumps(ensure_agents(FoundryAgentsV2()), indent=2, ensure_ascii=False))
        return

    # --- 詳細度切替 (再生成なし: v3 §2.4) ---
    if args.switch:
        plan = _load_plan(args.switch)
        level = args.level or "standard"
        view = project(plan, level)
        out = write_svg(view, f"exports/switch_{level}.svg")
        agg = sum(1 for n in view["nodes"] if n.get("kind") == "aggregate")
        note = " (再計算が必要)" if view.get("_needs_recompute") else " (再生成なし)"
        print(f"🔍 {level}: {len(view['nodes'])} ノード (集約 {agg}) / "
              f"{len(view['edges'])} 関係{note}\n💾 {out}")
        return

    # --- ドリルダウン (v3 §2.4④) ---
    if args.expand:
        if not args.plan:
            sys.exit("--expand には --plan が必要です")
        plan = _load_plan(args.plan)
        members = expand_aggregate(plan, args.expand)
        agg = next(a for a in plan["aggregates"] if a["id"] == args.expand)
        labels = {n["id"]: n.get("label", n["id"])
                  for lp in plan.get("_level_plans", {}).values()
                  for n in lp["nodes"]}
        print(f"📂 {agg['summary_label']} を展開 ({len(members)} 概念):")
        for m in members:
            print(f"   - {labels.get(m, m)}")
        return

    # --- ギャップ操作 (裁定 8 / G4) ---
    if args.gap_list or args.gap_confirm or args.gap_dismiss:
        if not args.plan:
            sys.exit("ギャップ操作には --plan が必要です")
        plan = _load_plan(args.plan)
        if args.gap_confirm or args.gap_dismiss:
            gid = args.gap_confirm or args.gap_dismiss
            decision = "confirm" if args.gap_confirm else "dismiss"
            try:
                g = apply_decision(plan, gid, decision, user_id=args.user)
            except Exception as exc:
                sys.exit(f"❌ {exc}")
            Path(args.plan).write_text(
                json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"✅ {gid} -> {g['status']} ({g['confirmed_by']} / {g['confirmed_at']})")
        rate = usefulness_rate(plan)
        print(f"\n❓ ギャップ候補 {rate['total_candidates']} 件 "
              f"(確定 {rate['decided']}: 有用 {rate['confirmed']} / 却下 {rate['dismissed']}"
              f" → 有用率 {rate['usefulness_rate']})")
        for g in plan.get("gaps", []):
            mark = {"candidate": "○", "confirmed": "✅", "dismissed": "✖"}[g["status"]]
            print(f"  {mark} {g['gap_id']:26s} 信頼度{g['confidence']:.2f} "
                  f"[{g['presumed_type']}] {g['reason'][:42]}…")
        return

    def run_once(message: str) -> None:
        print(f"⏳ 実行中 (target={args.target})…")
        try:
            summary = run_pipeline(
                message, target=args.target, paths=args.path, kg_file=args.kg,
                local_only=args.local_only, detail_level=args.level,
                verify_causal=not args.no_causal_verify, export_svg=not args.no_svg)
        except Exception as exc:
            print(f"❌ 失敗: {type(exc).__name__}: {exc}", file=sys.stderr)
            return
        _print_summary(summary)

    if args.message:
        run_once(args.message)
        return

    print("Concept Cartographer 実運用版 (終了: Ctrl-C / 空行)")
    while True:
        try:
            message = input("\nあなた> ").strip()
        except (KeyboardInterrupt, EOFError):
            break
        if not message:
            break
        run_once(message)


if __name__ == "__main__":
    main()
