#!/usr/bin/env python
"""PDF -> プレーンテキスト抽出 (extraction-agent への入力を作る前段)。

Usage:
    python scripts/extract_pdf_text.py s1290162_GT.pdf -o /tmp/thesis.txt
    python scripts/extract_pdf_text.py paper.pdf --pages 1-5

注意 (メモ §4): 抽出テキストは論文本文そのものなので、logs/ や exports/ ではなく
リポジトリ外 (/tmp 等) に置くこと。既定の出力先は標準出力。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    sys.exit("pypdf が必要です:  pip install -e '.[ingest]'")


def parse_pages(spec: str | None, total: int) -> range:
    if not spec:
        return range(total)
    if "-" in spec:
        a, b = spec.split("-", 1)
        return range(int(a) - 1, min(int(b), total))
    return range(int(spec) - 1, int(spec))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", default=None, help="出力先 (既定: 標準出力)")
    ap.add_argument("--pages", default=None, help="例: 1-5 / 3 (既定: 全ページ)")
    args = ap.parse_args()

    reader = PdfReader(args.pdf)
    chunks: list[str] = []
    for i in parse_pages(args.pages, len(reader.pages)):
        chunks.append(f"===== PAGE {i + 1} =====")
        chunks.append(reader.pages[i].extract_text() or "")
    text = "\n".join(chunks)

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"{len(text)} chars -> {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
