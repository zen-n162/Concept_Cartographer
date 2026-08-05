"""Concept Cartographer チャット CLI。

例:
  # 一発実行 (既定: VM-Excalidraw-MCP へ描画 + ローカルへミラー)
  python -m cc_orchestrator.chat "今週の研究を概念地図として整理して"

  # 資料フォルダを指定
  python -m cc_orchestrator.chat "今週の研究を整理して" --path ~/Documents/研究ノート

  # ローカル描画のみ (VM を使わない高速モード)
  python -m cc_orchestrator.chat "直近30日の研究を整理して" --target local

  # 対話モード
  python -m cc_orchestrator.chat

  # エージェント登録/更新のみ
  python -m cc_orchestrator.chat --setup-agents
"""

from __future__ import annotations

import argparse
import json
import sys

from cc_orchestrator.foundry_agents import FoundryAgents
from cc_orchestrator.pipeline import ensure_agents, run_pipeline


def _print_summary(s: dict) -> None:
    print()
    if s.get("status") == "no_documents":
        print(f"⚠ {s['hint']}")
        return
    ing = s.get("ingest", {})
    if "files" in ing:
        print(f"📄 取込 ({ing.get('window')}): {len(ing['files'])} 件")
        for f in ing["files"]:
            print(f"   - {f['name']}  [{f['source']}, {f['modified']}]")
    kgs = s.get("knowledge_graph", {})
    if kgs:
        print(f"🧠 抽出: 概念 {kgs.get('nodes')} / 関係 {kgs.get('edges')}"
              f" / 島 {kgs.get('communities')}  → {kgs.get('saved')}")
    lay = s.get("layout", {})
    if lay:
        print(f"📐 レイアウト: nodes={lay.get('nodes')} edges={lay.get('edges')}"
              f" islands={lay.get('islands')}")
    ver = s.get("verification", {})
    mark = "✅" if s.get("status") == "success" else "❌"
    print(f"{mark} 検証: {ver.get('verdict')}  {ver.get('summary', '')}")
    if s.get("export"):
        print(f"💾 エクスポート: {s['export']}")
    view = s.get("view", {})
    print(f"🖼  閲覧: {view.get('local_canvas')} (ローカルミラー)")
    if view.get("vm_canvas"):
        print(f"        VM 側: {view['vm_canvas']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Concept Cartographer chat")
    ap.add_argument("message", nargs="?", help="依頼文 (省略時は対話モード)")
    ap.add_argument("--target", choices=["vm", "local"], default="vm",
                    help="描画先 (既定 vm: VM-Excalidraw-MCP + ローカルミラー)")
    ap.add_argument("--path", action="append", default=[],
                    help="資料フォルダ (複数可)。OneDrive/SharePoint 同期フォルダも可")
    ap.add_argument("--kg", default=None, help="抽出をスキップし knowledge_graph JSON を使用")
    ap.add_argument("--setup-agents", action="store_true",
                    help="Foundry に 4 エージェントを登録/更新して終了")
    args = ap.parse_args()

    if args.setup_agents:
        ids = ensure_agents(FoundryAgents())
        print(json.dumps(ids, indent=2))
        return

    def run_once(message: str) -> None:
        print(f"⏳ 実行中 (target={args.target})… VM 描画は数分かかります")
        try:
            summary = run_pipeline(message, target=args.target,
                                   paths=args.path, kg_file=args.kg)
        except Exception as exc:
            print(f"❌ 失敗: {type(exc).__name__}: {exc}", file=sys.stderr)
            return
        _print_summary(summary)

    if args.message:
        run_once(args.message)
        return

    print("Concept Cartographer (終了: Ctrl-C / 空行)")
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
