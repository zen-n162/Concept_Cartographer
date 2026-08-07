"""概念図の編集 — 原本不変 + 追記のみ (編集/学習設計書 §4)。

設計原則 (§1):
- **原本不変**: 抽出結果 `graphs/kg_session_{s}.json` は書き換えない。編集は
  `graphs/edits_session_{s}.jsonl` へ 1 行 1 操作で追記するだけ。
  現在の姿は常に `fold(base_kg, edits)` で決定的に再構成できる。
- **undo も追記**: 取り消しは `{"op": "revert", "target": "<edit_id>"}` の追記で表す。
  過去の行を消さないので、「AI が出したもの」と「人が直したもの」の差分が
  永久に残る — これが学習 (cc_core.learning) の唯一の材料になる。
- **fold は絶対に例外で止めない**: 取り消された add_edge に依存する relabel_edge の
  ような「宙に浮いた操作」は警告つき no-op にする。1 行の不整合で地図全体が
  開けなくなる方が損失が大きい。

layout_plan は派生物なので `rebuild_session()` で上書き保存してよい。
そのとき編集で島がシャッフルされないよう**コミュニティを凍結**し (§4.3)、
ユーザーが触ったノードは Top-K 選抜から落ちないよう**ピン留め**する (§4.1)。
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import unicodedata
from pathlib import Path
from typing import Any

from cc_core.detail import build_multilevel_plan
from cc_core.evaluation import EvaluationSession, EvaluationStore
from cc_core.gaps import detect_gaps
from cc_core.logging_util import get_logger
from cc_core.normalize import VALID_GLYPHS

logger = get_logger("cc_core.editing")

GRAPHS_DIR = "graphs"
EVAL_LOG = "logs/evaluation.jsonl"
KG_PREFIX = "kg_session_"
PLAN_PREFIX = "layout_plan_session_"
EDITS_PREFIX = "edits_session_"

NODE_OPS = ("rename_node", "delete_node")
EDGE_OPS = ("relabel_edge", "retype_edge", "reverse_edge", "delete_edge")
ADD_OPS = ("add_node", "add_edge")
EDIT_OPS = NODE_OPS + EDGE_OPS + ADD_OPS
ALL_OPS = EDIT_OPS + ("revert",)

# 編集で指定できる関係の種類。設計書 §2 の表は "line" を挙げているが、
# 同じ行が「既存 glyph 語彙のみ」とも定めており、"line" は cc_core.adapter の
# GLYPH_STYLES にも cc_core.normalize.VALID_GLYPHS にも存在しない (描画規則が
# 無いので描けない)。**実在する語彙**を正として採用する。
EDITABLE_GLYPHS = set(VALID_GLYPHS)

# 要素の出所 (§2 provenance)。無印は AI 生成とみなす。
ORIGIN_AI = "ai"
ORIGIN_EDITED = "user_edited"
ORIGIN_ADDED = "user_added"
USER_ORIGINS = (ORIGIN_EDITED, ORIGIN_ADDED)

# 編集 → 評価ログの操作名 (§6)。cc_core.evaluation.OPERATIONS の語彙に写す。
OP_TO_EVAL = {
    "rename_node": "edit_node",
    "add_node": "edit_node",
    "delete_node": "delete_element",
    "relabel_edge": "edit_edge",
    "retype_edge": "edit_edge",
    "reverse_edge": "edit_edge",
    "add_edge": "edit_edge",
    "delete_edge": "delete_element",
    "revert": "edit_revert",
}
# 関係が「誤り」であったことを含意する操作 (§6)
IMPLIES_INCORRECT = ("delete_edge", "retype_edge")


class EditError(ValueError):
    """不正な編集操作 (Web では 400)。"""


class EditTargetNotFound(EditError):
    """対象の概念・関係・セッションが存在しない (Web では 404)。"""


class EditConflict(EditError):
    """既に取り消し済みなど、状態の競合 (Web では 409)。"""


def is_user_origin(element: dict[str, Any]) -> bool:
    """ユーザーが触った要素か (KPI の分母除外・UI バッジの判定に使う)。"""
    return str(element.get("origin") or "").startswith("user")


def normalize_label(label: Any) -> str:
    """ラベル照合キー: NFKC + trim + casefold (§5.1)。

    id はセッションローカルなので、セッションを跨ぐ照合はすべてこの
    正規化ラベルで行う。
    """
    return unicodedata.normalize("NFKC", str(label if label is not None else "")).strip().casefold()


# ------------------------------------------------------------------ パス


def kg_file(session: str, *, graphs_dir: str | Path = GRAPHS_DIR) -> Path:
    return Path(graphs_dir) / f"{KG_PREFIX}{session}.json"


def plan_file(session: str, *, graphs_dir: str | Path = GRAPHS_DIR) -> Path:
    return Path(graphs_dir) / f"{PLAN_PREFIX}{session}.json"


def edits_file(session: str, *, graphs_dir: str | Path = GRAPHS_DIR) -> Path:
    return Path(graphs_dir) / f"{EDITS_PREFIX}{session}.jsonl"


def load_kg(session: str, *, graphs_dir: str | Path = GRAPHS_DIR) -> dict[str, Any]:
    """原本の knowledge_graph を読む (不変。編集では絶対に書き換えない)。"""
    path = kg_file(session, graphs_dir=graphs_dir)
    if not path.exists():
        raise EditTargetNotFound(
            f"原本の knowledge_graph がありません: {path.name} "
            "(このセッションは編集できません)")
    return json.loads(path.read_text(encoding="utf-8"))


def load_plan(session: str, *, graphs_dir: str | Path = GRAPHS_DIR) -> dict[str, Any] | None:
    path = plan_file(session, graphs_dir=graphs_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("plan unreadable: %s", path.name)
        return None


def load_edits(session: str, *, graphs_dir: str | Path = GRAPHS_DIR) -> list[dict[str, Any]]:
    """編集ログを追記順に読む (壊れた行は飛ばす — 1 行で全体を失わない)。"""
    path = edits_file(session, graphs_dir=graphs_dir)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("skip broken edit line in %s", path.name)
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def list_edited_sessions(*, graphs_dir: str | Path = GRAPHS_DIR) -> list[str]:
    """編集ログを持つセッション ID を昇順で返す (relearn の入力)。"""
    base = Path(graphs_dir)
    if not base.exists():
        return []
    return sorted(p.stem[len(EDITS_PREFIX):] for p in base.glob(f"{EDITS_PREFIX}*.jsonl"))


# ------------------------------------------------------------------ 検証


def _island_ids(kg: dict[str, Any], plan: dict[str, Any] | None) -> set[str]:
    """add_node で指定できる島の集合 = 現在の plan の島 + KG 側の島。"""
    ids: set[str] = {str(c.get("id")) for c in kg.get("communities", []) if c.get("id")}
    for n in kg.get("nodes", []):
        if n.get("community_id"):
            ids.add(str(n["community_id"]))
    if plan:
        for isl in plan.get("islands", []):
            ids.add(str(isl.get("community_id")))
        for level_plan in (plan.get("_level_plans") or {}).values():
            for isl in level_plan.get("islands", []):
                ids.add(str(isl.get("community_id")))
            for n in level_plan.get("nodes", []):
                if n.get("community_id"):
                    ids.add(str(n["community_id"]))
        for n in plan.get("nodes", []):
            if n.get("community_id"):
                ids.add(str(n["community_id"]))
    return {i for i in ids if i}


def validate_edit(op: dict[str, Any], kg: dict[str, Any],
                  plan: dict[str, Any] | None = None) -> None:
    """1 操作の妥当性を検査する (設計書 §2 の表)。不正なら EditError。"""
    name = op.get("op")
    if name not in ALL_OPS:
        raise EditError(f"未知の操作です: {name}")
    payload = op.get("payload") or {}
    if not isinstance(payload, dict):
        raise EditError("payload はオブジェクトである必要があります")
    target = op.get("target")
    nodes = {n["id"]: n for n in kg.get("nodes", [])}
    edges = {e["id"]: e for e in kg.get("edges", [])}

    if name == "revert":
        if not target:
            raise EditError("revert には取り消す edit_id が必要です")
        return

    if name in NODE_OPS and target not in nodes:
        raise EditTargetNotFound(f"概念が見つかりません: {target}")
    if name in EDGE_OPS and target not in edges:
        raise EditTargetNotFound(f"関係が見つかりません: {target}")

    if name == "rename_node":
        label = str(payload.get("label") or "").strip()
        if not label:
            raise EditError("ラベルが空です")
        key = normalize_label(label)
        for nid, node in nodes.items():
            if nid != target and normalize_label(node.get("label")) == key:
                raise EditError(
                    f"同じ名前の概念が既にあります: 「{node.get('label')}」 "
                    "(統合は R2 の merge_nodes で扱います)")

    elif name == "add_node":
        label = str(payload.get("label") or "").strip()
        if not label:
            raise EditError("ラベルが空です")
        key = normalize_label(label)
        for node in nodes.values():
            if normalize_label(node.get("label")) == key:
                raise EditError(f"同じ名前の概念が既にあります: 「{node.get('label')}」")
        if payload.get("id") and str(payload["id"]) in nodes:
            raise EditError(f"id が重複しています: {payload['id']}")
        if not payload.get("new_island"):
            cid = payload.get("community_id")
            if not cid:
                raise EditError("community_id か new_island: true のどちらかが必要です")
            if str(cid) not in _island_ids(kg, plan):
                raise EditTargetNotFound(f"島が見つかりません: {cid}")

    elif name == "relabel_edge":
        if "label" not in payload:
            raise EditError("label が必要です")

    elif name == "retype_edge":
        glyph = payload.get("glyph")
        if glyph not in EDITABLE_GLYPHS:
            raise EditError(
                f"関係の種類は {'/'.join(sorted(EDITABLE_GLYPHS))} のみです: {glyph}")

    elif name == "add_edge":
        src, dst = payload.get("from"), payload.get("to")
        if src not in nodes:
            raise EditTargetNotFound(f"始点の概念が見つかりません: {src}")
        if dst not in nodes:
            raise EditTargetNotFound(f"終点の概念が見つかりません: {dst}")
        if src == dst:
            raise EditError("同じ概念どうしは繋げません (自己ループ)")
        glyph = payload.get("glyph") or "wave"
        if glyph not in EDITABLE_GLYPHS:
            raise EditError(
                f"関係の種類は {'/'.join(sorted(EDITABLE_GLYPHS))} のみです: {glyph}")
        for edge in edges.values():
            if (edge.get("from") == src and edge.get("to") == dst
                    and edge.get("glyph") == glyph):
                raise EditError("同じ向き・同じ種類の関係が既にあります")
        if payload.get("id") and str(payload["id"]) in edges:
            raise EditError(f"id が重複しています: {payload['id']}")


# ------------------------------------------------------------------ fold


def reverted_ids(edits: list[dict[str, Any]]) -> set[str]:
    """revert 行が指す edit_id の集合。"""
    return {str(e.get("target")) for e in edits
            if e.get("op") == "revert" and e.get("target")}


def effective_edits(edits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """取り消されていない実効的な編集だけを追記順で返す。"""
    dead = reverted_ids(edits)
    return [e for e in edits
            if e.get("op") != "revert" and str(e.get("edit_id")) not in dead]


def _new_id(prefix: str, edit: dict[str, Any], taken: set[str]) -> str:
    """追加要素の id を edit_id から決定的に作る (§4.4 の決定性)。"""
    seed = str(edit.get("edit_id") or edit.get("ts") or "manual")
    seed = seed[2:] if seed.startswith("e-") else seed
    candidate = f"{prefix}-{seed}"
    n = 2
    while candidate in taken:
        candidate = f"{prefix}-{seed}-{n}"
        n += 1
    return candidate


def _next_user_island(kg: dict[str, Any]) -> str:
    taken = {str(c.get("id")) for c in kg.get("communities", [])}
    taken |= {str(n.get("community_id")) for n in kg.get("nodes", [])}
    n = 1
    while f"comm_user_{n}" in taken:
        n += 1
    return f"comm_user_{n}"


def _apply_one(kg: dict[str, Any], edit: dict[str, Any]) -> None:
    """1 操作を現在の KG へ適用する (対象が無ければ EditTargetNotFound)。"""
    name = edit.get("op")
    target = edit.get("target")
    payload = edit.get("payload") or {}
    nodes = {n["id"]: n for n in kg["nodes"]}
    edges = {e["id"]: e for e in kg["edges"]}

    if name == "rename_node":
        node = nodes.get(target)
        if node is None:
            raise EditTargetNotFound(f"概念が見つかりません: {target}")
        node["label"] = str(payload.get("label", node.get("label")))
        node["origin"] = ORIGIN_EDITED

    elif name == "delete_node":
        if target not in nodes:
            raise EditTargetNotFound(f"概念が見つかりません: {target}")
        kg["nodes"] = [n for n in kg["nodes"] if n["id"] != target]
        kg["edges"] = [e for e in kg["edges"]
                       if e.get("from") != target and e.get("to") != target]

    elif name == "add_node":
        nid = str(payload.get("id") or _new_id("un", edit, set(nodes)))
        if nid in nodes:
            raise EditError(f"id が重複しています: {nid}")
        cid = str(payload.get("community_id") or "")
        if payload.get("new_island") or not cid:
            cid = _next_user_island(kg)
            kg.setdefault("communities", []).append(
                {"id": cid, "name": str(payload.get("label") or cid), "is_gap": False})
        kg["nodes"].append({
            "id": nid,
            "label": str(payload.get("label") or nid),
            "community_id": cid,
            "origin": ORIGIN_ADDED,
        })

    elif name in EDGE_OPS:
        edge = edges.get(target)
        if edge is None:
            raise EditTargetNotFound(f"関係が見つかりません: {target}")
        if name == "relabel_edge":
            edge["label"] = str(payload.get("label", ""))
            edge["origin"] = ORIGIN_EDITED
        elif name == "retype_edge":
            edge["glyph"] = str(payload.get("glyph"))
            edge["origin"] = ORIGIN_EDITED
            # 種類を人が決めた以上、機械の 3 点セット判定は上書きされる
            # (§2「人間が最終権威」)。判定記録もその事実に差し替える。
            edge["causal_check"] = {
                "verifier_verdict": "skipped",
                "reason": "ユーザーが関係の種類を指定 (人間が最終権威)",
            }
        elif name == "reverse_edge":
            edge["from"], edge["to"] = edge["to"], edge["from"]
            edge["origin"] = ORIGIN_EDITED
        elif name == "delete_edge":
            kg["edges"] = [e for e in kg["edges"] if e["id"] != target]

    elif name == "add_edge":
        src, dst = str(payload.get("from")), str(payload.get("to"))
        if src not in nodes:
            raise EditTargetNotFound(f"始点の概念が見つかりません: {src}")
        if dst not in nodes:
            raise EditTargetNotFound(f"終点の概念が見つかりません: {dst}")
        eid = str(payload.get("id") or _new_id("ue", edit, set(edges)))
        if eid in edges:
            raise EditError(f"id が重複しています: {eid}")
        kg["edges"].append({
            "id": eid,
            "from": src,
            "to": dst,
            "label": str(payload.get("label") or ""),
            "glyph": str(payload.get("glyph") or "wave"),
            "evidence_span": [],
            "origin": ORIGIN_ADDED,
        })

    else:
        raise EditError(f"未知の操作です: {name}")


def apply_edits(base_kg: dict[str, Any],
                edits: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """base_kg に編集ログを畳み込んで「現在の KG」を作る (§4)。

    戻り値は (現在 KG, 警告リスト)。**例外は投げない** — 取り消しで宙に浮いた
    操作は警告つき no-op にして先へ進む。
    """
    kg = copy.deepcopy(base_kg)
    kg.setdefault("nodes", [])
    kg.setdefault("edges", [])
    kg.setdefault("communities", [])
    warnings: list[str] = []
    for edit in effective_edits(edits):
        try:
            _apply_one(kg, edit)
        except EditError as exc:
            warnings.append(
                f"{edit.get('edit_id', '?')} ({edit.get('op')}): {exc} — 無視しました")
    if warnings:
        logger.info("edits folded with %d warning(s)", len(warnings))
    return kg, warnings


def annotate_edits(edits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """一覧表示用に `reverted` / `reverted_by` を付けたコピーを返す。"""
    dead = reverted_ids(edits)
    by_target: dict[str, str] = {}
    for e in edits:
        if e.get("op") == "revert" and e.get("target"):
            by_target[str(e["target"])] = str(e.get("edit_id") or "")
    out: list[dict[str, Any]] = []
    for e in edits:
        row = dict(e)
        eid = str(e.get("edit_id") or "")
        row["reverted"] = eid in dead and e.get("op") != "revert"
        if row["reverted"]:
            row["reverted_by"] = by_target.get(eid, "")
        out.append(row)
    return out


# ------------------------------------------------------------------ 追記


def _next_edit_id(edits: list[dict[str, Any]], now: dt.datetime) -> str:
    prefix = f"e-{now:%Y%m%d}-"
    seq = 0
    for e in edits:
        eid = str(e.get("edit_id") or "")
        if eid.startswith(prefix):
            try:
                seq = max(seq, int(eid[len(prefix):]))
            except ValueError:
                continue
    return f"{prefix}{seq + 1:03d}"


def _with_endpoint_labels(edge: dict[str, Any],
                          nodes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """エッジのスナップショットに端点ラベルを添える。

    設計書 §5.1 の照合キーは正規化ラベルであり、id はセッションローカルで
    使えない。学習 (cc_core.learning) がラベル対を必要とするので、
    **編集ログだけで学習を再構成できる**よう、適用時点の端点ラベルを残す。
    """
    out = dict(edge)
    out["from_label"] = nodes.get(str(edge.get("from")), {}).get("label")
    out["to_label"] = nodes.get(str(edge.get("to")), {}).get("label")
    return out


def _snapshot(op: str, target: Any, payload: dict[str, Any],
              kg: dict[str, Any]) -> dict[str, Any]:
    """適用直前の対象スナップショット (undo / 学習用に必須。§2)。"""
    nodes = {n["id"]: n for n in kg.get("nodes", [])}
    edges = {e["id"]: e for e in kg.get("edges", [])}
    if op == "delete_node":
        return {
            "node": copy.deepcopy(nodes.get(target, {})),
            "edges": [_with_endpoint_labels(copy.deepcopy(e), nodes)
                      for e in kg.get("edges", [])
                      if e.get("from") == target or e.get("to") == target],
        }
    if op in NODE_OPS:
        return copy.deepcopy(nodes.get(target, {}))
    if op in EDGE_OPS:
        return _with_endpoint_labels(copy.deepcopy(edges.get(target, {})), nodes)
    if op == "add_edge":
        return {
            "from_label": nodes.get(str(payload.get("from")), {}).get("label"),
            "to_label": nodes.get(str(payload.get("to")), {}).get("label"),
        }
    return {}


def _record_evaluation(session: str, row: dict[str, Any],
                       eval_log: str | Path) -> None:
    """編集を評価 KPI へ自動追記する (§6)。

    編集は暗黙の関係評価でもある。delete_edge / retype_edge は「この関係は
    誤りだった」という判定そのものなので relation verdict に落とす。
    記録に失敗しても編集自体は成立させる (KPI は副産物)。
    """
    try:
        ev = EvaluationSession(map_id=session, user_id=str(row.get("user") or "local-user"))
        op = str(row.get("op"))
        if op in IMPLIES_INCORRECT and row.get("target"):
            ev.judge_relation(str(row["target"]), "incorrect")
        # log_operation の第 1 引数名が `op` なので、詳細キーは edit_op にする
        # (同名だと TypeError: multiple values for argument 'op')
        ev.log_operation(OP_TO_EVAL.get(op, "edit_node"),
                         edit_op=op, target=str(row.get("target") or ""),
                         edit_id=str(row.get("edit_id") or ""))
        EvaluationStore(eval_log).append(ev)
    except Exception as exc:  # pragma: no cover - 記録側の事故は編集に無関係
        logger.warning("evaluation auto-append skipped: %s", type(exc).__name__)


def append_edit(session: str, op: dict[str, Any], *,
                graphs_dir: str | Path = GRAPHS_DIR,
                user: str = "local-user",
                eval_log: str | Path | None = EVAL_LOG,
                now: dt.datetime | None = None) -> dict[str, Any]:
    """採番 + before 充填 + validate + jsonl 追記 (§4)。戻り値 = 確定した編集行。"""
    name = str(op.get("op") or "")
    payload = dict(op.get("payload") or {}) if isinstance(op.get("payload"), dict) else {}
    target = op.get("target")

    base = load_kg(session, graphs_dir=graphs_dir)
    edits = load_edits(session, graphs_dir=graphs_dir)
    current, _ = apply_edits(base, edits)
    plan = load_plan(session, graphs_dir=graphs_dir)

    if name == "revert":
        known = {str(e.get("edit_id")) for e in edits if e.get("op") != "revert"}
        if str(target) not in known:
            raise EditTargetNotFound(f"編集が見つかりません: {target}")
        if str(target) in reverted_ids(edits):
            raise EditConflict(f"既に取り消し済みです: {target}")
    validate_edit({"op": name, "target": target, "payload": payload}, current, plan)

    stamp = now or dt.datetime.now()
    row: dict[str, Any] = {
        "edit_id": _next_edit_id(edits, stamp),
        "ts": stamp.isoformat(timespec="seconds"),
        "user": str(op.get("user") or user),
        "op": name,
        "target": target,
        "payload": payload,
        "before": _snapshot(name, target, payload, current),
    }

    path = edits_file(session, graphs_dir=graphs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    logger.info("edit appended session=%s id=%s op=%s", session, row["edit_id"], name)

    if eval_log:
        _record_evaluation(session, row, eval_log)
    return row


def append_revert(session: str, edit_id: str, **kwargs: Any) -> dict[str, Any]:
    """取り消し行を追記する (二重 revert は EditConflict)。"""
    return append_edit(session, {"op": "revert", "target": edit_id}, **kwargs)


# ------------------------------------------------- 凍結マップ / ピン留め


def frozen_communities(plan: dict[str, Any] | None,
                       kg: dict[str, Any]) -> dict[str, str]:
    """現在の plan の nodes[].community_id を凍結マップとして取り出す (§4.3)。

    plan は初回生成時の Leiden 結果を刻んでいるので、これをキャリアに使えば
    新しいファイルを増やさずにコミュニティを固定できる。集約に畳まれて
    どのレベルにも現れないノードは aggregates[].member_node_ids から補う。
    """
    mapping: dict[str, str] = {}
    if plan:
        sources: list[dict[str, Any]] = []
        level_plans = plan.get("_level_plans") or {}
        for lv in ("detailed", "standard", "overview"):
            if isinstance(level_plans.get(lv), dict):
                sources.append(level_plans[lv])
        sources.append(plan)
        for src in sources:
            for node in src.get("nodes", []):
                if node.get("kind") == "aggregate" or not node.get("community_id"):
                    continue
                mapping.setdefault(str(node["id"]), str(node["community_id"]))
        for agg in plan.get("aggregates", []):
            cid = str(agg.get("community_id") or "")
            if not cid:
                continue
            for member in agg.get("member_node_ids", []):
                mapping.setdefault(str(member), cid)
    # 編集で足した概念は plan に無い。add_node が決めた所属を凍結マップへ登録する
    for node in kg.get("nodes", []):
        if node.get("origin") == ORIGIN_ADDED and node.get("community_id"):
            mapping.setdefault(str(node["id"]), str(node["community_id"]))
    alive = {str(n["id"]) for n in kg.get("nodes", [])}
    return {k: v for k, v in mapping.items() if k in alive}


def pinned_nodes(edits: list[dict[str, Any]], kg: dict[str, Any]) -> set[str]:
    """編集ログに現れたノード id (§4.1)。Top-K 選抜で必ず残す。

    「編集したのに Overview から消えた」という体験を防ぐためのもので、
    エッジ編集の両端も含める (片端が消えるとその関係自体が見えなくなる)。
    """
    alive = {str(n["id"]) for n in kg.get("nodes", [])}
    edge_index = {str(e["id"]): e for e in kg.get("edges", [])}
    ids: set[str] = set()
    for edit in effective_edits(edits):
        name = edit.get("op")
        target = edit.get("target")
        payload = edit.get("payload") or {}
        before = edit.get("before") or {}
        if name in NODE_OPS and target:
            ids.add(str(target))
        elif name == "add_node":
            ids.add(str(payload.get("id") or _new_id("un", edit, set())))
        elif name == "add_edge":
            ids.add(str(payload.get("from")))
            ids.add(str(payload.get("to")))
        elif name in EDGE_OPS and target:
            edge = edge_index.get(str(target)) or before
            for key in ("from", "to"):
                if edge.get(key):
                    ids.add(str(edge[key]))
    return {i for i in ids if i in alive}


def _plan_relation_index(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """plan からエッジ id → エッジの索引を作る (detailed を優先)。

    detailed レベルは縮約前の全量を持つので、まずそこを見る。旧形式で
    `_level_plans` が無い plan もあるため、最後に本体の edges で補う。
    """
    index: dict[str, dict[str, Any]] = {}
    levels = plan.get("_level_plans") or {}
    sources = [levels.get(lv, {}).get("edges") or []
               for lv in ("detailed", "standard", "overview")]
    sources.append(plan.get("edges") or [])
    for edges in sources:
        for edge in edges:
            eid = str(edge.get("id") or "")
            if eid and eid not in index:
                index[eid] = edge
    return index


def reconcile_relation_policy(kg: dict[str, Any],
                              plan: dict[str, Any] | None) -> list[str]:
    """前回 plan に記録された関係ポリシーの結果を、現在の KG へ戻す。

    R1.5 より前のセッションは、**関係ポリシー (裁定 7 の因果 3 点セット) を
    適用する前**の KG を原本として保存していた。降格の結果は plan にしか
    残っていないため、そのまま `fold(base_kg, edits)` すると降格済みの相関が
    生の因果矢印へ戻り、1 か所の編集で 3 点セットが黙って無効化される
    【実測: kg_session_20260807_010128 で arrow 5 本 + zigzag 1 本が復活】。

    そこで plan 側の glyph / causal_check を正として KG へ写す。ただし
    **ユーザーが触った関係は対象外** — 人の判断が最終権威であり (§5.2)、
    plan で上書きすると編集そのものが取り消されてしまう。

    戻り値は復元した内容の説明 (呼び出し側が provenance に残す。黙って直さない)。
    """
    if not plan:
        return []
    index = _plan_relation_index(plan)
    notes: list[str] = []
    for edge in kg.get("edges", []):
        if is_user_origin(edge):
            continue
        prev = index.get(str(edge.get("id") or ""))
        if not prev:
            continue
        old, new = edge.get("glyph"), prev.get("glyph")
        if new and old != new:
            edge["glyph"] = new
            if prev.get("label") is not None:
                edge["label"] = prev["label"]
            notes.append(f"{edge.get('id')}: {old} → {new}")
        if prev.get("causal_check") is not None and "causal_check" not in edge:
            edge["causal_check"] = copy.deepcopy(prev["causal_check"])
    if notes:
        logger.info("relation policy reconciled from plan: %s", ", ".join(notes))
    return notes


def _merge_gap_decisions(new_gaps: list[dict[str, Any]],
                         old_gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """再検出したギャップへ、既存の confirm/dismiss を引き継ぐ。

    編集のたびにギャップ確定が消えると、有用率 KPI の分母が編集で目減りし、
    ユーザーの確定作業もやり直しになる。gap_id が一致するものだけ移す。
    """
    decided = {str(g.get("gap_id")): g for g in old_gaps
               if g.get("status") in ("confirmed", "dismissed")}
    out: list[dict[str, Any]] = []
    for gap in new_gaps:
        old = decided.get(str(gap.get("gap_id")))
        if old:
            gap = dict(gap)
            gap["status"] = old["status"]
            for key in ("confirmed_by", "confirmed_at"):
                if old.get(key) is not None:
                    gap[key] = old[key]
        out.append(gap)
    return out


def rebuild_session(session: str, *, graphs_dir: str | Path = GRAPHS_DIR,
                    default_level: str | None = None,
                    save: bool = True) -> dict[str, Any]:
    """base_kg + edits → 現在 KG → 3 レベル plan を再構成して保存する (§4)。

    コミュニティは凍結し (§4.3)、編集で触れたノードはピン留めする (§4.1)。
    同じ base_kg + 同じ編集ログからは常に同じ plan が出る (§4.4)。
    """
    base = load_kg(session, graphs_dir=graphs_dir)
    edits = load_edits(session, graphs_dir=graphs_dir)
    kg, warnings = apply_edits(base, edits)
    old = load_plan(session, graphs_dir=graphs_dir) or {}
    # R1.5 以前の原本は関係ポリシー適用前なので、plan 側の判定を戻す
    reconciled = reconcile_relation_policy(kg, old)

    level = default_level or old.get("detail_level") or "standard"
    plan = build_multilevel_plan(
        kg,
        default_level=level,
        frozen_communities=frozen_communities(old, kg) or None,
        pinned=pinned_nodes(edits, kg) or None,
        language=(old.get("provenance") or {}).get("language"),
    )
    plan["gaps"] = _merge_gap_decisions(
        [g.to_dict() for g in detect_gaps(kg)], old.get("gaps") or [])

    prov = plan.setdefault("provenance", {})
    prov["edit_count"] = len(effective_edits(edits))
    prov["edits_logged"] = len(edits)
    if warnings:
        prov["edit_warnings"] = warnings
    if reconciled:
        prov["policy_reconciled"] = reconciled

    if save:
        path = plan_file(session, graphs_dir=graphs_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("session rebuilt session=%s edits=%d nodes=%d",
                session, prov["edit_count"], len(kg.get("nodes", [])))
    return plan


def current_kg(session: str, *, graphs_dir: str | Path = GRAPHS_DIR) -> dict[str, Any]:
    """fold 済みの「現在の KG」を返す (CLI / Web の共通入口)。

    rebuild_session と同じく関係ポリシーの整合も行うので、ここで得た KG は
    画面に出ている地図と一致する。
    """
    kg, _ = apply_edits(load_kg(session, graphs_dir=graphs_dir),
                        load_edits(session, graphs_dir=graphs_dir))
    reconcile_relation_policy(kg, load_plan(session, graphs_dir=graphs_dir))
    return kg
