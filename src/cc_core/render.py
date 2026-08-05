"""CLI: render a layout_plan.json onto the Excalidraw canvas and verify it.

エージェントなしで再現できる描画パイプラインの基準線 (メモ §8 最小描画テスト)。

Usage:
    # layout_plan から描画
    python -m cc_core.render fixtures/layout_plan_min.json

    # knowledge_graph から描画 (compute_layout を経由)
    python -m cc_core.render graphs/kg_s1290162_m3.json --from-kg

Options: [--url URL] [--no-clear] [--no-verify] [--export-dir exports]
Exit code 0 = render + verify passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from cc_core.adapter import render_layout_plan
from cc_core.layout import compute_layout
from cc_core.logging_util import get_logger
from cc_core.mcp_client import DEFAULT_URL, ExcalidrawClient, extract_json
from cc_core.validate import validate_layout_plan
from cc_core.verify import verify_scene

logger = get_logger("cc_core.render")


async def run(args: argparse.Namespace) -> int:
    doc = json.loads(Path(args.plan).read_text(encoding="utf-8"))

    if args.from_kg:
        plan = compute_layout(doc, detail_level=args.detail_level)
        if args.save_plan:
            Path(args.save_plan).write_text(
                json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.info("layout_plan saved to %s", args.save_plan)
    else:
        plan = doc

    validation = validate_layout_plan(plan)
    if not validation.valid:
        print(json.dumps(validation.to_dict(), ensure_ascii=False, indent=2))
        return 2
    for w in validation.warnings:
        logger.warning("validate: %s", w)

    async with ExcalidrawClient(args.url) as client:
        result = await render_layout_plan(plan, client, clear_before=not args.no_clear)
        if not result.success:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 3

        report = None
        if not args.no_verify:
            report = await verify_scene(plan, client)

        if args.export_dir:
            export_dir = Path(args.export_dir)
            export_dir.mkdir(parents=True, exist_ok=True)
            stem = Path(args.plan).stem
            # .excalidraw: fetch scene JSON from the server, write client-side
            scene_text = await client.call("export_scene")
            scene = extract_json(scene_text)
            (export_dir / f"{stem}.excalidraw").write_text(
                json.dumps(scene, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            # SVG: returned inline (PNG export requires a connected browser client
            # and a server-side filePath, so it is optional here)
            try:
                svg = await client.call("export_to_image", {"format": "svg"})
                if svg.strip().startswith("<"):
                    (export_dir / f"{stem}.svg").write_text(svg, encoding="utf-8")
            except Exception as exc:  # SVG export needs a connected canvas browser
                logger.warning("svg export skipped: %s", type(exc).__name__)

        summary = {
            "rendered_elements": len(result.created),
            "verify_passed": report["passed"] if report else None,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if report and not report["passed"]:
            print(json.dumps(
                {k: report[k] for k in ("missing_elements", "label_mismatches", "gap_style_violations")},
                ensure_ascii=False, indent=2,
            ))
            return 4
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", help="path to layout_plan.json (or knowledge_graph.json with --from-kg)")
    parser.add_argument("--from-kg", action="store_true",
                        help="入力を knowledge_graph として扱い compute_layout を通す")
    parser.add_argument("--detail-level", default="standard",
                        choices=["overview", "standard", "detailed"])
    parser.add_argument("--save-plan", default=None,
                        help="--from-kg のとき、生成した layout_plan の保存先")
    parser.add_argument("--url", default=DEFAULT_URL, help="Excalidraw MCP streamable HTTP URL")
    parser.add_argument("--no-clear", action="store_true", help="do not clear canvas first")
    parser.add_argument("--no-verify", action="store_true", help="skip describe/query verification")
    parser.add_argument("--export-dir", default="exports", help="output dir for .excalidraw/.svg ('' to skip)")
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
