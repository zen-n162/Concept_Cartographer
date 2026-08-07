"""決定的な文分割 (R2a 設計書 §5)。

ゾーニング (文脈ラベル付け) と主張抽出は「文」を単位にする。その文が
**毎回同じ切れ方をする**ことが前提で、そうでないと sentence_id が run ごとに
変わり、layers サイドカーと kg の突合が壊れる。だからここは LLM を使わず、
規則だけで切る。

規則 (§5):
  - 終端は `。！？!?` または**連続改行**
  - `「」『』（）()` の内側の終端文字では切らない (深さカウンタ)
  - 空白のみの文は捨てる
  - 500 字を超えたら強制分割 (括弧の閉じ忘れ等の異常データで 1 文が
    資料 1 本ぶんに膨らむのを防ぐ安全弁)

`sentence_id = f"{document_id}#{idx:04d}#{sha256(text)[:8]}"` — 位置 (idx) と
内容 (ハッシュ) の両方を含める。前半の文が 1 つ増えると後続の idx はずれるが、
ハッシュが変わらないので「同じ文が動いた」ことは追える。

Stanza / GiNZA への差し替えは将来の課題で、いまはインタフェース
(`split_sentences` と `SentenceSpan`) だけを固定する。差し替えたときは
`SPLITTER_VERSION` を上げ、サイドカーに刻んだ版と照合できるようにする。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

SPLITTER_VERSION = "regex/1"

# 1 文の上限。超えたら強制的に切る (異常データ対策の安全弁)
MAX_SENTENCE_CHARS = 500

# 文の終端になる文字。全角・半角の両方を見る
TERMINATORS = "。！？!?"

# 深さカウンタで保護する括弧。開きと閉じを同じ添字で対にする
OPENERS = "「『（("
CLOSERS = "」』）)"


@dataclass(frozen=True)
class SentenceSpan:
    """1 文とその出所 (§5)。

    `text == source[char_start:char_end]` が常に成り立つ — この不変則が
    あるので、後段は char offset だけで原文へ戻れる。
    """

    sentence_id: str
    text: str
    char_start: int
    char_end: int
    document_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"sentence_id": self.sentence_id, "text": self.text,
                "char_start": self.char_start, "char_end": self.char_end,
                "document_id": self.document_id}


def sentence_id(document_id: str, idx: int, text: str) -> str:
    """文 ID を組み立てる (§5)。内容ハッシュ込みなので同じ文なら同じ後半になる。"""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"{document_id}#{idx:04d}#{digest}"


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    """前後の空白を除いた範囲を返す (offset を原文基準のまま保つ)。"""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _raw_segments(text: str) -> list[tuple[int, int]]:
    """終端記号と連続改行で切った素の範囲 (空白除去前) を返す。"""
    segments: list[tuple[int, int]] = []
    depth = 0
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in OPENERS:
            depth += 1
            i += 1
            continue
        if ch in CLOSERS:
            # 閉じが余っても負にしない (対応の取れない引用符は珍しくない)
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0 and ch in TERMINATORS:
            # 「本当ですか!?」のように終端記号が連続する場合はまとめて 1 文の末尾にする
            j = i + 1
            while j < n and text[j] in TERMINATORS:
                j += 1
            segments.append((start, j))
            start = j
            i = j
            continue
        if depth == 0 and ch == "\n":
            # 連続改行 (間に空白があってもよい) が段落境界。単独の改行では切らない
            j = i + 1
            newlines = 1
            while j < n and text[j].isspace():
                if text[j] == "\n":
                    newlines += 1
                j += 1
            if newlines >= 2:
                segments.append((start, j))
                start = j
                i = j
                continue
            i = j
            continue
        i += 1
    if start < n:
        segments.append((start, n))
    return segments


def _force_split(start: int, end: int) -> list[tuple[int, int]]:
    """500 字を超える範囲を機械的に刻む (§5 の強制分割)。"""
    if end - start <= MAX_SENTENCE_CHARS:
        return [(start, end)]
    out: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        stop = min(cursor + MAX_SENTENCE_CHARS, end)
        out.append((cursor, stop))
        cursor = stop
    return out


def split_sentences(text: Any, document_id: str) -> list[SentenceSpan]:
    """文へ切る (§5)。同じ入力からは**常に同じ結果**を返す。

    document_id はそのまま sentence_id の前半になるので、資料を跨いで
    一意になる値 (ファイル名など) を渡すこと。
    """
    if not isinstance(text, str) or not text.strip():
        return []
    document_id = str(document_id or "doc")

    spans: list[SentenceSpan] = []
    for raw_start, raw_end in _raw_segments(text):
        start, end = _trim_span(text, raw_start, raw_end)
        if start >= end:
            continue                                   # 空白のみの文は捨てる
        for piece_start, piece_end in _force_split(start, end):
            piece_start, piece_end = _trim_span(text, piece_start, piece_end)
            if piece_start >= piece_end:
                continue
            body = text[piece_start:piece_end]
            spans.append(SentenceSpan(
                sentence_id=sentence_id(document_id, len(spans), body),
                text=body, char_start=piece_start, char_end=piece_end,
                document_id=document_id))
    return spans
