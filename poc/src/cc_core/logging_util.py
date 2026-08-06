"""Sanitized logging (引き継ぎメモ §10-9).

ノードラベル・エッジラベル等の本文をログへ出さない。ID とラベルのハッシュ
先頭 8 桁のみを記録する。研究テキスト・秘密情報がログ経由で漏れないための
プロジェクト共通規約。
"""

from __future__ import annotations

import hashlib
import logging
import os


def label_digest(text: str | None) -> str:
    """Return a short non-reversible digest for a label, for correlation in logs."""
    if not text:
        return "-"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(os.environ.get("CC_LOG_LEVEL", "INFO"))
        logger.propagate = False
    return logger
