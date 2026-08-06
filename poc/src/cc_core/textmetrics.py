"""日本語混在テキストの表示幅推定と折り返し。

Excalidraw は描画時にコンテナ幅へ合わせてラベルを折り返す。レイアウト側が
その結果を知らないままだと、ラベルがノードやスキマからはみ出して重なる。
ここで幅を見積もり、layout がノードの大きさと間隔を決められるようにする。

単位は em (フォントサイズ 1 に対する比)。全角は 1.0、半角は 0.55 とみなす。
"""

from __future__ import annotations

import unicodedata

FULL_WIDTH = "WFA"  # East Asian Wide / Fullwidth / Ambiguous
HALF_EM = 0.55


def char_width(ch: str) -> float:
    return 1.0 if unicodedata.east_asian_width(ch) in FULL_WIDTH else HALF_EM


def display_width(text: str) -> float:
    """テキストの表示幅を em 単位で返す。"""
    return sum(char_width(c) for c in text or "")


def text_px(text: str, font_size: float) -> float:
    """1 行で描いたときのピクセル幅。"""
    return display_width(text) * font_size


def wrap_to_lines(text: str, max_em: float, max_lines: int = 3) -> list[str]:
    """max_em を超えないよう折り返す (日本語は任意位置で折れる)。

    max_lines を超える場合は最終行を省略記号で丸める。
    """
    lines: list[str] = []
    cur, cur_w = "", 0.0
    for ch in text or "":
        w = char_width(ch)
        if cur and cur_w + w > max_em:
            lines.append(cur)
            cur, cur_w = ch, w
            if len(lines) == max_lines:
                break
        else:
            cur += ch
            cur_w += w
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if not lines:
        return [""]
    # 収まりきらなかった分は最終行を丸める
    consumed = sum(len(x) for x in lines)
    if consumed < len(text or ""):
        lines[-1] = lines[-1][:-1] + "…"
    return lines


def truncate(text: str, max_em: float) -> str:
    """max_em に収まるよう 1 行に丸める (超過時は末尾を … に)。"""
    if display_width(text) <= max_em:
        return text
    out, w = "", 0.0
    for ch in text:
        cw = char_width(ch)
        if w + cw > max_em - 0.6:  # … の分を空ける
            break
        out += ch
        w += cw
    return out + "…"


def balanced_lines(text: str, max_lines: int = 2) -> tuple[int, float]:
    """行数を max_lines 以内に抑えたときの (行数, 1行あたりの em 幅) を返す。

    行数はできるだけ少なく、かつ各行の長さが揃うように選ぶ。
    """
    total = display_width(text)
    for lines in range(1, max_lines + 1):
        per_line = total / lines
        if per_line <= 12.0:  # 1 行 12em (全角12文字) 以内なら十分読める
            return lines, per_line
    return max_lines, total / max_lines
