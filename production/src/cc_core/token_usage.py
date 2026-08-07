"""LLM 使用量の記録と集計 (裁定 Z。コスト設計 §3)。

節約は測れないと続かない。1 実行あたりの実トークン数を summary へ載せ、
`logs/token_usage.jsonl` へ 1 行ずつ積み、`--token-report` で日別に集計する。

## 数字をでっち上げない

フィールド名は **実応答で確認済み** (2026-08-07 / Responses API `/openai/v1/responses`):

    "usage": {"input_tokens": 989,
              "input_tokens_details": {"cached_tokens": 0},
              "output_tokens": 31,
              "output_tokens_details": {"reasoning_tokens": 0},
              "total_tokens": 1020}

将来 API が名前を変えたり、応答に usage が無かったりした場合は、その回を
`unknown` として数え、表示は**「不明」**にする。0 を足して「入力 0」と見せると
「使っていない」と読めてしまい、記録として嘘になるため。

## 単価

単価は未判明なので掛け算はしない。判明したら `CC_PRICE_IN` / `CC_PRICE_OUT`
(**1000 トークンあたりの円**) を渡すと `--token-report` が円換算を添える。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

from cc_core.logging_util import get_logger

logger = get_logger("cc_core.token_usage")

USAGE_LOG = "logs/token_usage.jsonl"
ENV_PRICE_IN = "CC_PRICE_IN"
ENV_PRICE_OUT = "CC_PRICE_OUT"

UNKNOWN_LABEL = "不明"

# 日別集計が持つ数値列 (定義・加算・合計の 3 か所で同じ並びを使う)
COLUMNS = ("input", "output", "calls", "runs", "cached_runs", "unknown")


def blank() -> dict[str, int]:
    """空の集計器。calls は HTTP 往復 (ツール応答の返送も 1 回と数える)。

    `cached_input` だけを内訳として持つ (表示で「入力のうち再利用」に使う)。
    reasoning_tokens は output_tokens の**内数**で、請求の総量は output で
    足りるため持たない — 読まれない数字を溜めても増えるのは維持費だけ。
    """
    return {"input": 0, "output": 0, "calls": 0, "cached_input": 0, "unknown": 0}


def _as_int(value: Any) -> int | None:
    """True/False を 1/0 として数えないための厳しめの整数判定。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def add_response(totals: dict[str, int], usage: Any) -> dict[str, int]:
    """1 応答ぶんの usage を積む。**取れなければ unknown を 1 増やす**。

    呼び出しそのものは起きているので calls は必ず増やす。「何 call 走ったか」と
    「そのうち何 call のトークン数が分かったか」は別の情報として残す。
    """
    totals["calls"] = totals.get("calls", 0) + 1
    if not isinstance(usage, Mapping):
        totals["unknown"] = totals.get("unknown", 0) + 1
        return totals
    inp = _as_int(usage.get("input_tokens"))
    out = _as_int(usage.get("output_tokens"))
    if inp is None or out is None:
        totals["unknown"] = totals.get("unknown", 0) + 1
        return totals
    totals["input"] = totals.get("input", 0) + inp
    totals["output"] = totals.get("output", 0) + out
    details_in = usage.get("input_tokens_details")
    if isinstance(details_in, Mapping):
        cached = _as_int(details_in.get("cached_tokens"))
        if cached:
            totals["cached_input"] = totals.get("cached_input", 0) + cached
    return totals


def is_unknown(tokens: Mapping[str, Any] | None) -> bool:
    """トークン数が 1 件も取れなかったか (call はあったのに全部 unknown)。"""
    if not tokens:
        return False
    return bool(tokens.get("calls")) and int(tokens.get("unknown") or 0) >= int(
        tokens.get("calls") or 0)


def format_line(tokens: Mapping[str, Any] | None) -> str:
    """CLI 最終行 (設計 §3)。0 call の実行にも「0 call」と言い切る。"""
    if not tokens:
        return ""
    calls = int(tokens.get("calls") or 0)
    if not calls:
        return "🔢 トークン: LLM 呼び出しなし (0 call)"
    if is_unknown(tokens):
        return (f"🔢 トークン: {UNKNOWN_LABEL} (LLM {calls} call — "
                "応答に usage がありませんでした)")
    line = (f"🔢 トークン: 入力 {int(tokens.get('input') or 0):,} / "
            f"出力 {int(tokens.get('output') or 0):,} (LLM {calls} call)")
    extra = []
    cached = int(tokens.get("cached_input") or 0)
    if cached:
        extra.append(f"入力のうち再利用 {cached:,}")
    unknown = int(tokens.get("unknown") or 0)
    if unknown:
        extra.append(f"{unknown} call は {UNKNOWN_LABEL}")
    return line + (f"  ({' / '.join(extra)})" if extra else "")


# ------------------------------------------------------------------ 追記


def append_log(route: str, tokens: Mapping[str, Any] | None, *,
               session: str | None = None, cached: bool = False,
               path: str | Path = USAGE_LOG,
               now: dt.datetime | None = None) -> dict[str, Any] | None:
    """1 実行 1 行を jsonl へ積む。**書けなくても実行は成功扱い**。

    キャッシュ命中の行も (0 call として) 残す。「使わずに済んだ回数」が
    分からないと、節約できているかどうかが測れないため。
    """
    counted = tokens or {}
    row: dict[str, Any] = {
        "ts": (now or dt.datetime.now()).isoformat(timespec="seconds"),
        "route": str(route or ""),
        "input": int(counted.get("input") or 0),
        "output": int(counted.get("output") or 0),
        "calls": int(counted.get("calls") or 0),
        "cached": bool(cached),
    }
    if session:
        row["session"] = session
    unknown = int(counted.get("unknown") or 0)
    if unknown:
        row["unknown"] = unknown
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("token_usage を書けません: %s", type(exc).__name__)
        return None
    return row


def read_log(path: str | Path = USAGE_LOG) -> list[dict[str, Any]]:
    """jsonl を読む (壊れた行は飛ばす)。"""
    target = Path(path)
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


# ------------------------------------------------------------------ 集計


def _price(env_name: str) -> float | None:
    raw = os.environ.get(env_name)
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("%s の値が数値ではありません — 円換算を省きます", env_name)
        return None


def daily_report(path: str | Path = USAGE_LOG) -> dict[str, Any]:
    """日別集計 (設計 §3 の `--token-report`)。"""
    rows = read_log(path)
    days: dict[str, dict[str, int]] = defaultdict(
        lambda: {k: 0 for k in COLUMNS})
    for row in rows:
        day = str(row.get("ts") or "")[:10]
        if not day:
            continue
        bucket = days[day]
        for key in ("input", "output", "calls", "unknown"):
            bucket[key] += int(row.get(key) or 0)
        bucket["runs"] += 1
        if row.get("cached"):
            bucket["cached_runs"] += 1
    ordered = [{**days[day], "day": day} for day in sorted(days)]
    total = {k: sum(d[k] for d in ordered) for k in COLUMNS}
    return {"days": ordered, "total": total, "rows": len(rows),
            "path": str(path),
            "price_in": _price(ENV_PRICE_IN), "price_out": _price(ENV_PRICE_OUT)}


def _yen(report: Mapping[str, Any], input_tokens: int, output_tokens: int) -> str:
    """円換算。**単価が両方揃っているときだけ**添える (片方だけの推計はしない)。"""
    price_in, price_out = report.get("price_in"), report.get("price_out")
    if price_in is None or price_out is None:
        return ""
    yen = input_tokens / 1000 * float(price_in) + output_tokens / 1000 * float(price_out)
    return f"  ≈ {yen:,.1f} 円"


def _shown_width(text: str) -> int:
    """表示幅 (東アジア文字幅 W/F を 2 桁と数える)。

    `f"{s:<12}"` は**文字数**で数えるので、全角の見出しが混じると表が崩れる
    (「再利用」3 文字 = 6 桁)。cc_orchestrator.chat._pad と同じ考え方。
    """
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _lpad(text: str, width: int) -> str:
    return text + " " * max(0, width - _shown_width(text))


def _rpad(text: str, width: int) -> str:
    return " " * max(0, width - _shown_width(text)) + text


LABEL_WIDTH = 12


def _nums(bucket: Mapping[str, Any]) -> str:
    """数値列だけ (見出しは呼び出し側が _lpad で付ける)。"""
    return (f"{int(bucket.get('input') or 0):>12,}"
            + f"{int(bucket.get('output') or 0):>11,}"
            + f"{int(bucket.get('calls') or 0):>11,}"
            + f"{int(bucket.get('runs') or 0):>7}"
            + f"{int(bucket.get('cached_runs') or 0):>8}")


def format_report(report: Mapping[str, Any]) -> str:
    """`--token-report` の表示 (単価が無ければ掛けない)。"""
    lines: list[str] = []
    total = report.get("total") or {}
    if not report.get("rows"):
        return ("🔢 トークン記録はまだありません "
                f"({report.get('path')})\n"
                "   通常実行を 1 回行うと記録が始まります "
                "(テストモードの再利用も 0 call の行として残ります)。")
    lines.append(f"🔢 トークン使用量  ({report.get('path')} / {report['rows']} 行)")
    lines.append("")
    lines.append("   " + _lpad("日付", LABEL_WIDTH) + _rpad("入力", 12)
                 + _rpad("出力", 11) + _rpad("LLM call", 11)
                 + _rpad("実行", 7) + _rpad("再利用", 8))
    for day in report.get("days") or ():
        lines.append("   " + _lpad(day["day"], LABEL_WIDTH) + _nums(day)
                     + _yen(report, day["input"], day["output"]))
    lines.append("   " + _lpad("合計", LABEL_WIDTH) + _nums(total)
                 + _yen(report, total.get("input", 0), total.get("output", 0)))
    if total.get("unknown"):
        lines.append(f"   ※ {total['unknown']} call は usage を取得できず "
                     f"{UNKNOWN_LABEL} (上の数字には入っていません)")
    if report.get("price_in") is None or report.get("price_out") is None:
        lines.append(f"   ※ 単価未設定のため円換算は出しません "
                     f"({ENV_PRICE_IN} / {ENV_PRICE_OUT} に 1000 トークンあたりの"
                     "円を入れると付きます)")
    if total.get("cached_runs"):
        lines.append(f"   ♻ うち {total['cached_runs']} 実行はテストモードの"
                     "再利用で LLM を呼んでいません")
    return "\n".join(lines)


__all__ = ["USAGE_LOG", "add_response", "append_log", "blank", "daily_report",
           "format_line", "format_report", "is_unknown", "read_log"]
