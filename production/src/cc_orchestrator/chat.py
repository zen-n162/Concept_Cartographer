"""Concept Cartographer 実運用版 チャット CLI (R1)。

例:
  # 地図生成 (詳細度は依頼文から自動判定。既定 standard)
  python -m cc_orchestrator.chat "今週の研究を概念地図として整理して"
  python -m cc_orchestrator.chat "今月の研究をざっくり全体像で"        # -> overview
  python -m cc_orchestrator.chat "直近30日を詳しく" --level detailed

  # 生成済み地図の詳細度を切り替える (LLM 呼び出しゼロ・再レイアウトなし)
  python -m cc_orchestrator.chat --switch graphs/layout_plan_session_X.json --level overview

  # 生成済み plan を今すぐローカル canvas へ描く (生成し直さない)
  python -m cc_orchestrator.chat --render graphs/layout_plan_session_X.json --level standard

  # 集約ノードを展開する (ドリルダウン)
  python -m cc_orchestrator.chat --expand agg-comm_001 --plan graphs/layout_plan_session_X.json

  # ギャップ候補の確定 (confirm / dismiss)
  python -m cc_orchestrator.chat --gap-list --plan <plan.json>
  python -m cc_orchestrator.chat --gap-confirm gap-isolated-c003 --plan <plan.json>

  # 概念図の編集 (原本は不変。編集ログへ追記し plan を再構成する)
  python -m cc_orchestrator.chat --plan <plan.json> \
      --edit '{"op":"rename_node","target":"c001","payload":{"label":"新しい名前"}}'
  python -m cc_orchestrator.chat --plan <plan.json> --edit-file ops.json
  python -m cc_orchestrator.chat --plan <plan.json> --list-edits
  python -m cc_orchestrator.chat --plan <plan.json> --revert-edit e-20260807-001

  # 多層分析 (R2a。既定 ON)
  python -m cc_orchestrator.chat "今週の研究を..." --no-layers      # 切る
  python -m cc_orchestrator.chat --layers-summary graphs/layout_plan_session_X.json
  python -m cc_orchestrator.chat --layers-summary 20260807_120000

  # コーパス索引 (R2b。LLM 不要。索引は派生キャッシュで消しても作り直せる)
  python -m cc_orchestrator.chat --reindex
  python -m cc_orchestrator.chat --search "データ同化"

  # 質問に答える (R2b。地図は作らず、索引の材料から出典つきで答える)
  python -m cc_orchestrator.chat "SuperPCAとスーパーピクセルの関係は?"   # local
  python -m cc_orchestrator.chat "私の研究の全体像をまとめて"            # global
  python -m cc_orchestrator.chat "全体像を踏まえた上で比較して"          # hybrid

  # 過去の修正からの学習
  python -m cc_orchestrator.chat --show-learned
  python -m cc_orchestrator.chat --relearn
  python -m cc_orchestrator.chat "今週の研究を..." --no-learned

  # エージェント登録/更新
  python -m cc_orchestrator.chat --setup-agents
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from cc_core import layers_store
from cc_core.community import expand_aggregate
from cc_core.detail import project
from cc_core.editing import (
    EditError,
    KG_PREFIX,
    PLAN_PREFIX,
    annotate_edits,
    append_edit,
    load_edits,
    rebuild_session,
)
from cc_core.gaps import apply_decision, usefulness_rate
from cc_core.verifiers import rejection_path
from cc_core.learning import (
    cue_warnings,
    load_learned,
    relearn,
    report_line,
    summarize,
    update_from_edit,
)
from cc_core.svg_export import write_svg
from cc_store import SessionStore, rebuild_index
from cc_orchestrator.foundry_v2 import FoundryAgentsV2
from cc_orchestrator.pipeline import ensure_agents, run_pipeline
from cc_orchestrator.tool_exec import ToolExecutor

LEVELS = ("overview", "standard", "detailed")


SOURCE_MARK = {"node": "●", "edge": "→", "community": "◆"}


def _print_sources(s: dict) -> None:
    """QA 経路の出典ブロック (R2b 設計書 §2 の表示)。

    「どのセッションのどの資料から言っているのか」が無い答えは検証できない。
    出典が 1 件も無いときは黙らず「なし」と書く — 出典欄が消えているのと
    「出典が無いことを確かめた」のは別の情報なので。
    """
    sources = s.get("sources") or []
    info = s.get("qa") or {}
    if not info and not sources:
        return
    print()
    if sources:
        print(f"📚 出典 ({len(sources)} 件)")
        for src in sources[:10]:
            where = src.get("session") or "コーパス全体"
            doc = f" / {src['document_id']}" if src.get("document_id") else ""
            print(f"   {SOURCE_MARK.get(src.get('kind'), '·')} "
                  f"{src.get('label', '')}  [{where}{doc}]")
        if len(sources) > 10:
            print(f"   … 他 {len(sources) - 10} 件")
    else:
        print("📚 出典: なし")
    if not info:
        return
    bits = [f"LLM {info.get('llm_calls', 0)} call"]
    if info.get("cache_hits"):
        bits.append(f"要約キャッシュ命中 {info['cache_hits']}")
    if info.get("sessions"):
        bits.append(f"セッション {len(info['sessions'])}")
    if info.get("communities"):
        bits.append(f"テーマ {len(info['communities'])}")
    if info.get("truncated"):
        bits.append("近傍は上限で打ち切り")
    if info.get("budget_exceeded"):
        bits.append("呼び出し上限に到達")
    if info.get("insufficient"):
        bits.append("材料不足")
    if info.get("offline"):
        bits.append("オフライン")
    print(f"   ({' / '.join(bits)})")


def _print_summary(s: dict) -> None:
    print()
    if s.get("status") == "answered":
        print(f"💬 [{s['routing']['route']} 経路] {s['routing']['rationale']}")
        print(s.get("answer", ""))
        _print_sources(s)
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
    lr = s.get("learned") or {}
    if lr.get("enabled"):
        print(f"🎓 {report_line(lr)}"
              + (" (詳細は summary.learned.details)" if lr.get("details") else ""))
    ly = s.get("layers") or {}
    if ly.get("status") and ly["status"] != "disabled":
        st = ly.get("stats") or {}
        va = s.get("validation") or {}
        print(f"🧩 多層分析 [{ly['status']}]: 文 {st.get('sentences', 0)} / "
              f"ラベル {st.get('zoned', 0)} / 主張 {st.get('claims', 0)}"
              f" (検証済 {st.get('validated', 0)} / 却下 {st.get('rejected', 0)})"
              f" / 矛盾 {st.get('refutes', 0)} / LLM {st.get('llm_calls', 0)} call"
              + (f"   rejection_log: {va['rejection_log']}"
                 if va.get("rejection_log") else ""))
    gp = s.get("gaps", {})
    if gp:
        print(f"❓ ギャップ候補: {gp['candidates']} 件 {gp['by_type']}")
        if gp.get("by_gap_type"):
            kinds = gp["by_gap_type"]
            print(f"   型: 構造 {kinds.get('structural', 0)} / "
                  f"言説 {kinds.get('discourse', 0)} / 因果 {kinds.get('causal', 0)}")
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


# ------------------------------------------------------------------ 編集


def _session_of(plan_path: str) -> tuple[str, Path]:
    """--plan からセッション ID と graphs ディレクトリを取り出す。"""
    stem = Path(plan_path).stem
    if not stem.startswith(PLAN_PREFIX):
        sys.exit(f"--plan には {PLAN_PREFIX}*.json を指定してください: {plan_path}")
    return stem[len(PLAN_PREFIX):], Path(plan_path).parent


def _print_edits(session: str, graphs_dir: Path) -> None:
    rows = annotate_edits(load_edits(session, graphs_dir=graphs_dir))
    if not rows:
        print("✏️  編集はまだありません")
        return
    live = sum(1 for r in rows if r["op"] != "revert" and not r["reverted"])
    print(f"✏️  編集 {len(rows)} 行 (有効 {live} / 取り消し済み "
          f"{sum(1 for r in rows if r['reverted'])})")
    for row in rows:
        mark = "✖" if row["reverted"] else ("↩" if row["op"] == "revert" else "•")
        detail = json.dumps(row.get("payload") or {}, ensure_ascii=False)
        line = (f"  {mark} {row['edit_id']}  {row['op']:<13s} "
                f"{str(row.get('target') or '-'):<16s} {detail[:52]}")
        print(line + (f"   [取り消し: {row.get('reverted_by')}]" if row["reverted"] else ""))


def _print_learned(store: dict) -> None:
    s = summarize(store)
    print(f"🎓 学習ストア (scope={s['scope']} / 更新 {s['updated_at'] or '—'})")
    print(f"   用語辞書  {s['lexicon']} 件 (自動適用 {s['lexicon_auto']})")
    for e in store.get("lexicon", [])[:8]:
        print(f"     {'✓' if e.get('auto') else '·'} 「{e['from']}」→「{e['to']}」"
              f" (n={e.get('n', 1)})")
    print(f"   除外リスト {s['stoplist']} 件 (自動適用 {s['stoplist_auto']})")
    for e in store.get("stoplist", [])[:8]:
        print(f"     {'✓' if e.get('auto') else '·'} 「{e['label']}」 (n={e.get('n', 1)})")
    print(f"   因果上書き {s['causal_overrides']} 件 {s['by_decision']}")
    for o in store.get("causal_overrides", [])[:8]:
        print(f"     · 「{o['from_label']}」→「{o['to_label']}」: {o['decision']}"
              f" ({o.get('source', '')})")
    print(f"   事例ヒント {s['few_shot']} 件 / 語彙統計 {s['cue_stats']} 語")
    for f in store.get("few_shot", [])[:5]:
        print(f"     · {f['text']}")
    for w in cue_warnings(store):
        print(f"   ⚠ {w}")
    print("   ※ 「学習」はモデルの再学習ではありません。用語辞書・除外リスト・"
          "因果上書きの決定的な適用と、抽出プロンプトへの注意書き注入です。")


def _layers_target(target: str) -> tuple[str, Path]:
    """--layers-summary の引数から (セッション ID, graphs ディレクトリ) を取る。

    plan / kg / layers のどのファイル名でも、素のセッション ID でも受ける。
    「どれを渡せばいいか」を利用者に覚えさせないため。
    """
    path = Path(target)
    if path.suffix != ".json":
        return target, Path(layers_store.GRAPHS_DIR)
    stem = path.stem
    for prefix in (PLAN_PREFIX, KG_PREFIX, layers_store.LAYERS_PREFIX):
        if stem.startswith(prefix):
            return stem[len(prefix):], path.parent
    return stem, path.parent


def _print_layers_summary(target: str) -> None:
    """--layers-summary: layers サイドカーの要約 (LLM 不要・R2a 設計書 §10)。

    Web の結果カード (主張 n 件・検証済 m 件・矛盾 k 件) と同じ数を CLI でも
    見られるようにする。rejection_log は**パスを出すだけ**にせず件数も数える —
    「何が落ちたか」を追う入口がここになるため。
    """
    session, graphs_dir = _layers_target(target)
    if not layers_store.exists(session, graphs_dir=graphs_dir):
        sys.exit(f"❌ layers サイドカーがありません: "
                 f"{layers_store.path(session, graphs_dir=graphs_dir)}\n"
                 "   この地図は R2a 以前の生成か、多層分析を切って生成されています。")
    doc = layers_store.load(session, graphs_dir=graphs_dir)
    st = doc.get("stats") or {}
    print(f"🧩 多層分析 session={session} (v{doc.get('version')} / "
          f"splitter={doc.get('splitter')})")
    print(f"   文 {st.get('sentences', 0)} → ラベル {st.get('zoned', 0)} / "
          f"主張 {st.get('claims', 0)} (検証済 {st.get('validated', 0)} / "
          f"却下 {st.get('rejected', 0)}) / 論証 {st.get('arguments', 0)} / "
          f"矛盾 {st.get('refutes', 0)} / LLM {st.get('llm_calls', 0)} call")

    claims = [c for c in doc.get("claims") or [] if isinstance(c, dict)]
    mark = {"validated": "✅", "uncertain": "△", "rejected": "✖"}
    for claim in claims[:12]:
        validation = claim.get("validation") or {}
        status = str(validation.get("status") or "—")
        combined = validation.get("combined")
        text = str((claim.get("assertion") or {}).get("claim_text") or "")
        print(f"   {mark.get(status, '·')} [{status}"
              + (f" {combined:.2f}" if isinstance(combined, (int, float)) else "")
              + f"] {text[:56]}")
    if len(claims) > 12:
        print(f"   … 他 {len(claims) - 12} 件")

    refutes = [r for r in doc.get("refutes") or []
               if isinstance(r, dict) and r.get("verdict") == "refutes"]
    if refutes:
        print(f"   ⚡ 矛盾 {len(refutes)} 組:")
        for record in refutes[:5]:
            print(f"      · {str(record.get('rationale') or '')[:60]}")

    log = rejection_path(session)
    if log.exists():
        rows = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
        print(f"   📄 rejection_log: {log} ({len(rows)} 行)")
    else:
        print("   📄 rejection_log: なし (却下された主張・因果候補はありません)")


# ------------------------------------------------- コーパス索引 (R2b §1)


def _print_reindex(graphs_dir: str | Path = layers_store.GRAPHS_DIR) -> None:
    """--reindex: 全セッションから索引とコーパスを作り直す (LLM 呼び出しゼロ)。

    索引は**派生キャッシュ**なので、これは「壊れたら押すボタン」であって
    日常操作ではない (指紋が変われば検索時に自動で作り直される)。手動の口を
    残すのは、自動再構築が効かないほど壊れたときの最後の逃げ道として。
    """
    store = SessionStore(graphs_dir)
    counts = rebuild_index(store)
    print(f"🔁 索引を再構築しました: {store.corpus_dir}")
    print(f"   セッション {counts['sessions']} / 概念 {counts['nodes']} 行 "
          f"(併合後 {counts['corpus_nodes']}) / 関係 {counts['edges']} 行 "
          f"(併合後 {counts['corpus_edges']}) / コーパス島 {counts['communities']}")


def _print_search(query: str, graphs_dir: str | Path = layers_store.GRAPHS_DIR,
                  limit: int = 8) -> None:
    """--search: 索引の横断検索 (動作確認用の薄い口)。

    回答生成は R2b-2 の QA 経路が行う。ここは「索引に何が入っているか」を
    人が目で確かめるためのもの。
    """
    store = SessionStore(graphs_dir)
    hits = store.search_nodes(query, limit=limit)
    if not hits:
        print(f"🔎 「{query}」に当たる概念・関係はありません "
              f"(セッション {len(store.list_sessions())} 件を検索)")
        return
    print(f"🔎 「{query}」: {len(hits)} 件")
    for hit in hits:
        if hit["kind"] == "node":
            print(f"   ● {hit['label']}  [{hit['session']} / {hit['node_id']}]"
                  f"  重要度 {float(hit.get('importance') or 0.0):.2f}"
                  f"  島 {hit.get('corpus_community') or '—'}"
                  + ("  ← 完全一致" if hit.get("exact") else ""))
        else:
            print(f"   → {hit['from_norm']} —[{hit.get('glyph')}: "
                  f"{hit.get('label') or '—'}]→ {hit['to_norm']}"
                  f"  [{hit['session']} / {hit['edge_id']}]")
            evidence = str(hit.get("evidence") or "").strip()
            if evidence:
                print(f"      根拠: {evidence[:70]}")


def _run_edits(args: argparse.Namespace) -> None:
    """--edit / --edit-file / --revert-edit を適用し plan を再構成する。"""
    session, graphs_dir = _session_of(args.plan)
    ops: list[dict] = []
    if args.edit:
        parsed = json.loads(args.edit)
        ops.extend(parsed if isinstance(parsed, list) else [parsed])
    if args.edit_file:
        parsed = json.loads(Path(args.edit_file).read_text(encoding="utf-8"))
        ops.extend(parsed if isinstance(parsed, list) else [parsed])
    if args.revert_edit:
        ops.append({"op": "revert", "target": args.revert_edit})

    applied: list[dict] = []
    for op in ops:
        try:
            row = append_edit(session, op, graphs_dir=graphs_dir, user=args.user)
        except EditError as exc:
            sys.exit(f"❌ {exc}")
        applied.append(row)
        print(f"✏️  {row['edit_id']}  {row['op']} {row.get('target') or ''}")

    plan = rebuild_session(session, graphs_dir=graphs_dir, default_level=args.level)
    lv = plan.get("levels", {})
    print(f"🔍 再構成: " + "  ".join(f"{k}={v['nodes']}" for k, v in lv.items())
          + f"   (編集 {plan['provenance'].get('edit_count')} 件反映)")
    for w in plan.get("provenance", {}).get("edit_warnings", []):
        print(f"   ⚠ {w}")

    delta = update_from_edit(applied[-1], session, graphs_dir=graphs_dir)
    if delta["changed"]:
        print(f"🎓 学習を更新: {delta['changed']}")

    if args.target == "file":
        from cc_core.excalidraw_file import write_scene
        for level in LEVELS:
            out = write_svg(project(plan, level),
                            f"exports/session_{session}_{level}.svg")
            print(f"   SVG[{level}]: {out}")
        scene = write_scene(project(plan, plan.get("detail_level", "standard")),
                            f"exports/session_{session}.excalidraw")
        print(f"💾 {scene}")


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
    ap.add_argument("--render", default=None, metavar="PLAN",
                    help="plan を再生成せず今すぐローカル canvas へ描画 (--level と併用)")
    ap.add_argument("--expand", default=None, metavar="AGG_ID",
                    help="集約ノードを展開してメンバーを表示")
    ap.add_argument("--gap-list", action="store_true", help="ギャップ候補を一覧")
    ap.add_argument("--gap-confirm", default=None, metavar="GAP_ID")
    ap.add_argument("--gap-dismiss", default=None, metavar="GAP_ID")
    ap.add_argument("--user", default="local-user", help="確定操作の記録者")
    # --- 編集とフィードバック学習 (編集/学習設計書 §7) ---
    ap.add_argument("--edit", default=None, metavar="JSON",
                    help='編集 1 件 (例: \'{"op":"rename_node","target":"c001",'
                         '"payload":{"label":"新名"}}\')')
    ap.add_argument("--edit-file", default=None, metavar="FILE",
                    help="編集操作の JSON 配列ファイル (バッチ)")
    ap.add_argument("--list-edits", action="store_true",
                    help="編集履歴を一覧 (取り消し済みマーク付き)")
    ap.add_argument("--revert-edit", default=None, metavar="EDIT_ID",
                    help="編集を取り消す (取り消し行を追記)")
    ap.add_argument("--show-learned", action="store_true",
                    help="learned.json の要約を表示")
    ap.add_argument("--relearn", action="store_true",
                    help="編集ログから learned.json を再構成")
    ap.add_argument("--no-learned", action="store_true",
                    help="過去の修正からの学習を適用しない")
    # --- R2a 知識モデル多層化 (R2a 設計書 §10) ---
    ap.add_argument("--no-layers", action="store_true",
                    help="多層分析 (文脈ラベル・主張抽出・検証・論証) を行わない")
    ap.add_argument("--layers-summary", default=None, metavar="PLAN|SESSION",
                    help="生成済みセッションの多層分析を要約表示 (LLM 不要)")
    # --- R2b 検索・スケール (R2b 設計書 §1) ---
    ap.add_argument("--reindex", action="store_true",
                    help="コーパス索引を再構築して件数を表示 (LLM 不要)")
    ap.add_argument("--search", default=None, metavar="QUERY",
                    help="全セッション横断で概念・関係を検索 (LLM 不要)")
    args = ap.parse_args()

    if args.layers_summary:
        _print_layers_summary(args.layers_summary)
        return

    # --- コーパス索引 (LLM 不要。索引は派生キャッシュ: 裁定 J) ---
    if args.reindex:
        _print_reindex()
        if not args.search:
            return
    if args.search:
        _print_search(args.search)
        return

    if args.setup_agents:
        print(json.dumps(ensure_agents(FoundryAgentsV2()), indent=2, ensure_ascii=False))
        return

    # --- 学習ストア (LLM 不要) ---
    if args.relearn:
        store = relearn()
        print("🔁 編集ログから learned.json を再構成しました\n")
        _print_learned(store)
        return
    if args.show_learned:
        _print_learned(load_learned())
        return

    # --- 編集 (LLM 不要。Web と同一の cc_core.editing を通す) ---
    if args.edit or args.edit_file or args.revert_edit or args.list_edits:
        if not args.plan:
            sys.exit("編集操作には --plan が必要です")
        if args.list_edits and not (args.edit or args.edit_file or args.revert_edit):
            session, graphs_dir = _session_of(args.plan)
            _print_edits(session, graphs_dir)
            return
        _run_edits(args)
        if args.list_edits:
            session, graphs_dir = _session_of(args.plan)
            print()
            _print_edits(session, graphs_dir)
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

    # --- ローカル canvas への描画のみ (再生成なし: ミニ設計 §4) ---
    if args.render:
        if not Path(args.render).exists():
            sys.exit(f"❌ plan が見つかりません: {args.render}")
        plan = _load_plan(args.render)
        level = args.level or "standard"
        view = project(plan, level)
        result = ToolExecutor(target="local").tool_render_layout_plan({"plan": view})
        if not result.get("success"):
            sys.exit(f"❌ 描画に失敗しました: {'; '.join(result.get('errors', []))}")
        url = os.environ.get("EXCALIDRAW_CANVAS_URL", "http://127.0.0.1:3000")
        print(f"🖼  {level}: {len(result.get('created', []))} 要素を描画しました\n💾 {url}")
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
                verify_causal=not args.no_causal_verify, export_svg=not args.no_svg,
                learned=not args.no_learned, layers=not args.no_layers)
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
