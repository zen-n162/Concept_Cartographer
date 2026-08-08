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
import os
import re
from pathlib import Path
from typing import Any

from cc_core import editing, gap_report, layers_store
from cc_core.community import LEVEL_ORDER, expand_aggregate
from cc_core.detail import project
from cc_core.evaluation import summarize
from cc_core.gaps import apply_decision, usefulness_rate
from cc_core.learning import update_from_edit
from cc_core.logging_util import get_logger
from cc_core.svg_export import write_svg
from cc_orchestrator.tool_exec import ToolExecutor

logger = get_logger("cc_web.sessions")

GRAPHS_DIR = "graphs"
SVG_CACHE_DIR = "exports/web"
PLAN_PREFIX = "layout_plan_session_"
CANVAS_URL_ENV = "EXCALIDRAW_CANVAS_URL"

# セッション ID はファイル名の一部になる。`..` や `/` を弾いて
# パストラバーサルを防ぐ (受け取った文字列でパスを組み立てないのが原則だが、
# 二重の防御として ID 自体も制限する)。
SESSION_RE = re.compile(r"^[0-9A-Za-z_\-]{1,64}$")

# view JSON へ通すエッジ属性 (v4核§6.3)。plan の内部キー (_conf 等) は出さない。
# layer_tags / claim_refs / onto_class は R2a の機械タグ (設計書 §3.1)。
# クリック展開で「なぜこの記号なのか」を説明するために UI まで運ぶ。
EDGE_FIELDS = ("id", "from", "to", "label", "glyph", "confidence",
               "epistemic_status", "evidence_span", "causal_check",
               "member_edge_ids", "polarity", "provenance", "origin",
               "layer_tags", "claim_refs", "validation")
NODE_FIELDS = ("id", "label", "kind", "community_id", "importance",
               "aggregate_id", "visible_at", "origin",
               "onto_class", "claim_refs")
# 島のうち「semantic エンジンのときだけ載る」項目。値が無ければキーごと落とす。
ISLAND_OPTIONAL_FIELDS = ("layout_mode", "tint")


class SessionNotFound(LookupError):
    """未知のセッション ID (または plan ファイルが無い)。"""


class GapNotFound(LookupError):
    """未知の gap_id。"""


class LayersNotFound(LookupError):
    """layers サイドカーが無い (R2a 以前の生成、または多層分析を切った run)。"""


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
        # layout_mode (レイアウト v3 §2) と tint (§5) は semantic エンジンの
        # ときだけ島に載る。無いときはキーごと出さない — grid で作った既存
        # セッションの応答を変えないため (UI は知らないキーを無視する)。
        "islands": [
            {k: v for k, v in (("community_id", i.get("community_id")),
                               ("name", i.get("name")),
                               ("layout_mode", i.get("layout_mode")),
                               ("tint", i.get("tint")))
             if not (k in ISLAND_OPTIONAL_FIELDS and v is None)}
            for i in view.get("islands", [])
        ],
        # 裁定 AO: Standard と Detailed が同数で、かつ資料からこれ以上は
        # 抽出できないときだけ入る。無いときも "" を返してキーの有無で
        # 表示側が分岐しないようにする。
        "detail_note": view.get("detail_note") or "",
        "editable": editing.kg_file(session, graphs_dir=GRAPHS_DIR).exists(),
    }


# ------------------------------------------------------------ layers (R2a)


# 主張 1 件のうち UI へ運ぶキー。サイドカーの zones は文が全部入って重いので
# 返さない — クリック展開に要るのは「主張の本文と検証結果」だけ。
CLAIM_FIELDS = ("nanopub_id", "assertion", "validation", "provenance")


def layers_of(session: str) -> dict[str, Any]:
    """layers サイドカーを UI 向けに返す (R2a 設計書 §10)。

    エッジのクリック展開が `claim_refs` (nanopub_id) から主張の本文へ辿れる
    ようにするのが主目的。**無い場合は例外**にして、呼び出し側が 404 と
    「この地図は R2a 以前の生成です」を返す — 空の 200 を返すと、
    「主張が 0 件だった地図」と「そもそも層を持たない地図」が区別できない。
    """
    if not valid_session(session):
        raise LayersNotFound(f"不正なセッション ID: {session}")
    if not layers_store.exists(session, graphs_dir=GRAPHS_DIR):
        raise LayersNotFound(
            "この地図は R2a 以前の生成です (多層分析の記録がありません)。"
            "同じ資料でもう一度生成すると、主張と検証の記録が付きます。")
    doc = layers_store.load(session, graphs_dir=GRAPHS_DIR)
    claims = [{k: c[k] for k in CLAIM_FIELDS if k in c}
              for c in doc.get("claims") or () if isinstance(c, dict)]
    return {
        "session": session,
        "version": doc.get("version"),
        "splitter": doc.get("splitter"),
        "claims": claims,
        "arguments": doc.get("arguments") or [],
        "refutes": doc.get("refutes") or [],
        "stats": doc.get("stats") or {},
    }


# --------------------------------------------------------- Excalidraw 描画


def canvas_url() -> str:
    """ローカル Excalidraw canvas の URL (設計書 §2.2, 既定 127.0.0.1:3000)。

    呼び出しのたびに読む (import 時に固定しない) — テストが monkeypatch で
    差し替えられるようにするため。ACA へ移設する際もこの環境変数の
    差し替えだけで済む。
    """
    return os.environ.get(CANVAS_URL_ENV, "http://127.0.0.1:3000")


class RenderConnectionError(RuntimeError):
    """ローカルの Excalidraw (canvas / MCP) に接続できない (設計書 §2.1)。"""


def _connection_message() -> str:
    return (f"ローカルの Excalidraw ({canvas_url()}) に接続できません。"
            "起動していない可能性があります。")


def render_to_canvas(session: str, level: str) -> dict[str, Any]:
    """指定した詳細度の plan をローカル Excalidraw canvas へ描画する (設計書 §2.1)。

    view_of() と同じ `project(plan, level)` で投影し、描画は既存の
    `ToolExecutor(target="local").tool_render_layout_plan` にそのまま渡す
    (新規の描画コードは書かない; 設計書 §1)。canvas は 1 面しかなく
    clear_before=True で描き直すため、呼び出し元 (app.py) は JobManager と
    同じロックで直列化すること (生成ジョブ・編集と奪い合わないため)。
    """
    check_level(level)
    plan = load_plan(session)
    view = project(plan, level)
    try:
        result = ToolExecutor(target="local").tool_render_layout_plan({"plan": view})
    except Exception as exc:  # MCP/canvas 未起動・接続不可など (§2.1)
        logger.warning("render_to_canvas: connection failed (%s)", type(exc).__name__)
        raise RenderConnectionError(_connection_message()) from exc
    if not result.get("success"):
        logger.warning("render_to_canvas: failed %s", result.get("errors"))
        raise RenderConnectionError(_connection_message())
    elements = len(result.get("created", []))
    logger.info("render_to_canvas session=%s level=%s elements=%d",
                session, level, elements)
    return {"url": canvas_url(), "elements": elements, "level": level}


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


def build_gap_report(session: str) -> dict[str, Any]:
    """ギャップレポートを作って exports/ に保存し、JSON を返す (設計 §2.1)。

    CLI の `--gap-report` と**同じ cc_core の関数**を呼ぶ薄いラッパ。
    LLM クライアントは作れたら使う。az トークンが無い環境で 500 を返すのは
    誤り — finding だけのレポートは正常な成果物なので、静かに縮退させる。
    """
    from cc_store import SessionStore

    client = None
    try:
        from cc_orchestrator.foundry_v2 import FoundryAgentsV2
        client = FoundryAgentsV2()
    except Exception as exc:
        logger.info("gap report: LLM 提案なし (%s)", type(exc).__name__)

    report = gap_report.build_gap_report(
        session, SessionStore(GRAPHS_DIR), client=client)
    saved = gap_report.save_report(report)
    report["saved"] = {k: str(v) for k, v in saved.items()}
    return report


# ------------------------------------------------------------------ 展開


# ------------------------------------------------------------------ 編集


def purge_svg_cache(session: str) -> int:
    """そのセッションの SVG キャッシュを捨てる (編集/学習設計書 §8.1)。

    plan の mtime 比較でも再生成はされるが、編集の直後に**確実に**古い絵が
    出ないよう明示的に消す (キャッシュが原因で「直したのに変わらない」と
    見えるのが編集機能では一番まずい)。
    """
    base = Path(SVG_CACHE_DIR)
    if not base.exists():
        return 0
    removed = 0
    for path in base.glob(f"{session}_*.svg"):
        try:
            path.unlink()
            removed += 1
        except OSError:  # 他プロセスが掴んでいても致命ではない
            logger.warning("svg cache not removed: %s", path.name)
    return removed


def list_edits(session: str) -> dict[str, Any]:
    """編集履歴 + fold 警告。原本 KG が無い古いセッションでも 200 で返す。"""
    plan_path(session)  # 未知セッションは SessionNotFound
    edits = editing.load_edits(session, graphs_dir=GRAPHS_DIR)
    warnings: list[str] = []
    editable = editing.kg_file(session, graphs_dir=GRAPHS_DIR).exists()
    if editable:
        _, warnings = editing.apply_edits(
            editing.load_kg(session, graphs_dir=GRAPHS_DIR), edits)
    else:
        warnings.append(
            "このセッションには原本の knowledge_graph がないため編集できません")
    return {"edits": editing.annotate_edits(edits), "warnings": warnings,
            "editable": editable}


def _after_edit(session: str, edit: dict[str, Any],
                level: str | None) -> dict[str, Any]:
    """編集 1 件の適用後処理: rebuild → キャッシュ破棄 → 学習更新 → view。"""
    plan = editing.rebuild_session(session, graphs_dir=GRAPHS_DIR)
    purge_svg_cache(session)
    delta = update_from_edit(edit, session, graphs_dir=GRAPHS_DIR)
    lv = level or plan.get("detail_level", "standard")
    if lv not in LEVEL_ORDER:
        lv = "standard"
    return {
        "edit": edit,
        "view": view_of(session, lv),
        "learned_delta": delta,
        "levels": plan.get("levels", {}),
        "warnings": (plan.get("provenance") or {}).get("edit_warnings", []),
    }


def apply_edit(session: str, op: dict[str, Any], *, user: str,
               level: str | None = None) -> dict[str, Any]:
    """編集を 1 件追記して plan を再構成する (CLI と同じ cc_core.editing 経由)。"""
    edit = editing.append_edit(session, op, graphs_dir=GRAPHS_DIR, user=user)
    return _after_edit(session, edit, level)


def revert_edit(session: str, edit_id: str, *, user: str,
                level: str | None = None) -> dict[str, Any]:
    """編集を取り消す (取り消し行の追記 → 再構成)。"""
    edit = editing.append_revert(session, edit_id, graphs_dir=GRAPHS_DIR, user=user)
    return _after_edit(session, edit, level)


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
