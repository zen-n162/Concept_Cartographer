"""保存済みセッション (layout_plan) の読取と操作 (設計書 §4)。

生成済みの `graphs/layout_plan_session_*.json` が唯一の真実。Web 側は状態を
持たず、毎回このファイルを読む。詳細度の切替・集約の展開・ギャップ確定は
すべて cc_core の既存 API をそのまま呼ぶ (CLI と同じ結果になるようにする)。

SVG は plan から決定的に作れるので、`exports/web/` に **キャッシュ**して
plan より新しければ再生成しない。詳細度の切替が LLM 呼び出しゼロで済む
(v3 §2.4) という性質を、Web でも体感できる速度で出すため。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from cc_core.community import LEVEL_ORDER, expand_aggregate
from cc_core.detail import project
from cc_core.evaluation import summarize
from cc_core.gaps import apply_decision, usefulness_rate
from cc_core.logging_util import get_logger
from cc_core.svg_export import write_svg

logger = get_logger("cc_web.sessions")

GRAPHS_DIR = "graphs"
SVG_CACHE_DIR = "exports/web"
PLAN_PREFIX = "layout_plan_session_"

# セッション ID はファイル名の一部になる。`..` や `/` を弾いて
# パストラバーサルを防ぐ (受け取った文字列でパスを組み立てないのが原則だが、
# 二重の防御として ID 自体も制限する)。
SESSION_RE = re.compile(r"^[0-9A-Za-z_\-]{1,64}$")

# view JSON へ通すエッジ属性 (v4核§6.3)。plan の内部キー (_conf 等) は出さない。
EDGE_FIELDS = ("id", "from", "to", "label", "glyph", "confidence",
               "epistemic_status", "evidence_span", "causal_check",
               "member_edge_ids", "polarity", "provenance")
NODE_FIELDS = ("id", "label", "kind", "community_id", "importance",
               "aggregate_id", "visible_at")


class SessionNotFound(LookupError):
    """未知のセッション ID (または plan ファイルが無い)。"""


class GapNotFound(LookupError):
    """未知の gap_id。"""


def valid_session(session: str) -> bool:
    return bool(SESSION_RE.match(session or ""))


def plan_path(session: str) -> Path:
    if not valid_session(session):
        raise SessionNotFound(f"不正なセッション ID: {session}")
    path = Path(GRAPHS_DIR) / f"{PLAN_PREFIX}{session}.json"
    if not path.exists():
        raise SessionNotFound(f"セッションが見つかりません: {session}")
    return path


def load_plan(session: str) -> dict[str, Any]:
    return json.loads(plan_path(session).read_text(encoding="utf-8"))


def save_plan(session: str, plan: dict[str, Any]) -> None:
    path = plan_path(session)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")


def check_level(level: str) -> str:
    if level not in LEVEL_ORDER:
        raise ValueError(f"未知の詳細度: {level}")
    return level


def list_sessions(titles: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """graphs/ の plan を新しい順に一覧する。title は履歴の依頼文。"""
    titles = titles or {}
    base = Path(GRAPHS_DIR)
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(base.glob(f"{PLAN_PREFIX}*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True):
        session = path.stem[len(PLAN_PREFIX):]
        if not valid_session(session):
            continue
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):  # 生成途中・破損は一覧から外す
            logger.warning("skip unreadable plan: %s", path.name)
            continue
        out.append({
            "session": session,
            "created_at": dt.datetime.fromtimestamp(
                path.stat().st_mtime).isoformat(timespec="seconds"),
            "title": titles.get(session) or session,
            "levels": plan.get("levels", {}),
            "default_level": plan.get("detail_level", "standard"),
        })
    return out


def session_detail(session: str) -> dict[str, Any]:
    """セッション 1 件のメタ情報 + KPI スナップショット。"""
    plan = load_plan(session)
    level = plan.get("detail_level", "standard")
    view = project(plan, level if level in LEVEL_ORDER else "standard")
    return {
        "session": session,
        "levels": plan.get("levels", {}),
        "default_level": level,
        "gaps_usefulness": usefulness_rate(plan),
        "kpi": summarize(view, []),
    }


# ------------------------------------------------------------------ SVG


def svg_file(session: str, level: str) -> Path:
    """指定レベルの SVG を返す (plan より新しければキャッシュを再利用)。"""
    check_level(level)
    src = plan_path(session)
    out = Path(SVG_CACHE_DIR) / f"{session}_{level}.svg"
    if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
        return out
    plan = load_plan(session)
    write_svg(project(plan, level), out)
    logger.info("svg regenerated session=%s level=%s", session, level)
    return out


# ------------------------------------------------------------------ view


def view_of(session: str, level: str) -> dict[str, Any]:
    """UI が地図と突合するための JSON。plan 全体 (_level_plans) は返さない。"""
    check_level(level)
    plan = load_plan(session)
    view = project(plan, level)
    return {
        "session": session,
        "level": level,
        "nodes": [{k: n[k] for k in NODE_FIELDS if k in n} for n in view["nodes"]],
        "edges": [{k: e[k] for k in EDGE_FIELDS if k in e}
                  for e in view.get("edges", [])],
        "aggregates": view.get("aggregates", []),
        "gaps": view.get("gaps", []),
        "levels": view.get("levels", {}),
    }


# ------------------------------------------------------------------ ギャップ


def decide_gap(session: str, gap_id: str, decision: str,
               *, user_id: str) -> dict[str, Any]:
    """ギャップ候補を確定する。確定は人間が行う (v4核§8) の入口。

    未知の gap_id は GapNotFound、確定済みの再確定は cc_core の
    GapDecisionError (呼び出し側で 409 にする) として区別する。
    """
    plan = load_plan(session)
    if not any(g.get("gap_id") == gap_id for g in plan.get("gaps", [])):
        raise GapNotFound(f"gap_id が見つかりません: {gap_id}")
    gap = apply_decision(plan, gap_id, decision, user_id=user_id)
    save_plan(session, plan)
    return {"gap": gap, "usefulness": usefulness_rate(plan)}


# ------------------------------------------------------------------ 展開


def expand(session: str, aggregate_id: str) -> dict[str, Any]:
    """集約ノードのメンバーを返す (v3 §2.4④ ドリルダウン)。

    ラベルは detailed レベルの plan から引く。集約は下位レベルで畳まれた
    ノードなので、現在のレベルの nodes には出てこないため。
    """
    plan = load_plan(session)
    members = expand_aggregate(plan, aggregate_id)  # 未知なら KeyError
    definition = next((a for a in plan.get("aggregates", [])
                       if a["id"] == aggregate_id), {})
    detailed = plan.get("_level_plans", {}).get("detailed", {})
    labels = {n["id"]: n.get("label", n["id"]) for n in detailed.get("nodes", [])}
    return {
        "aggregate": definition,
        "members": [{"id": m, "label": labels.get(m, m)} for m in members],
    }
