"""3 レベル同梱 layout_plan の生成とレベル抽出 (実運用計画 §4)。

生成は Detailed 粒度で 1 回だけ行い、overview / standard / detailed の 3 レベルを
単一の layout_plan.json に同梱する。切替は `project(plan, level)` による
機械的な絞り込みだけで済み、LLM 呼び出しも再レイアウトも発生しない
(v3 §2.4「再生成を伴わずクライアント側で完結することを基本とし」)。

【裁定 3】v3 §2.4 の「固定的に最大粒度を生成せず」との関係:
Detailed は 100 ノード上限で Top-K 選抜が働くため無制限生成ではない。
同節が同時に求める「切替時に再生成しない」を満たすための同節内トレードオフ解決。

レイアウトは**レベルごとに独立して計算**する。同じ座標のまま間引くと、
消えたノードの穴が空いて可読性 (エッジラベルの間隔設計) が崩れるため。
"""

from __future__ import annotations

import copy
from typing import Any, Iterable

from cc_core.community import (
    LEVEL_BANDS,
    LEVEL_ORDER,
    DetailAnalysis,
    analyze,
)
from cc_core.island_packing import anchors_from_plan
from cc_core.layout import compute_layout
from cc_core.logging_util import get_logger

logger = get_logger("cc_core.detail")

# レイアウト v3 §3a: 島の方位を決める基準レベルと、そのための計算順。
# ANCHOR_ORDER は **LEVEL_ORDER の並べ替え** (基準の detailed を先頭に出すだけ)。
# 格納順は LEVEL_ORDER のまま — 既存の plan 互換を壊さない。
ANCHOR_LEVEL = "detailed"
ANCHOR_ORDER: tuple[str, ...] = ("detailed", "standard", "overview")

AGGREGATE_STYLE = {"rough": True, "backgroundColor": "#e7f5ff", "strokeColor": "#1971c2"}

# kg -> plan で運ぶ属性。**ここに書き忘れると plan に届かない**のが実測済みの
# 関門なので、新しいフィールドを足すときは必ずこの 2 つのリストも更新すること
# (schemas/layout_plan.schema.json / cc_web.sessions の FIELDS と 3 点セット)。
NODE_CARRY: tuple[str, ...] = ("origin", "onto_class", "claim_refs")
EDGE_CARRY: tuple[str, ...] = ("confidence", "evidence_span", "epistemic_status",
                               "polarity", "provenance", "causal_check", "origin",
                               "layer_tags", "claim_refs", "validation")


def _level_kg(
    kg: dict[str, Any],
    analysis: DetailAnalysis,
    level: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """指定レベルで描く knowledge_graph (可視ノード + 集約ノード) を組み立てる。"""
    visible = set(analysis.visible[level])
    aggregates = analysis.aggregates[level]
    node_meta = {n["id"]: n for n in kg.get("nodes", [])}

    nodes: list[dict[str, Any]] = []
    for nid in analysis.visible[level]:
        src = node_meta.get(nid, {})
        node: dict[str, Any] = {
            "id": nid,
            "label": src.get("label", nid),
            "community_id": analysis.communities.get(nid, "comm_000"),
        }
        # 出所 (編集/学習設計書 §2) は UI バッジと KPI の分母判定に要るので運ぶ。
        # onto_class / claim_refs (R2a 設計書 §3.1) も同様 — ここで落とすと
        # plan に届かず、クリック展開で層の情報が出せなくなる。
        for attr in NODE_CARRY:
            if src.get(attr):
                node[attr] = src[attr]
        # レイアウト v3 §4: ノードの大きさを重要度で変えるため、**レイアウトが
        # 読める場所**= 生成の入口で importance を渡す。NODE_CARRY には足さない
        # (kg 由来の属性ではなく analysis の産物なので出所が違う)。
        # plan 上の最終的な値と並び順は build_multilevel_plan が付け直す。
        imp = analysis.importance.get(nid)
        if imp:
            node["importance"] = imp.to_dict()
        nodes.append(node)

    # 集約ノードを「メンバーがいたコミュニティ」に置く
    agg_ids: dict[str, str] = {}
    for agg in aggregates:
        nodes.append({
            "id": agg.id,
            "label": agg.summary_label,
            "community_id": agg.community_id,
        })
        for m in agg.member_node_ids:
            agg_ids[m] = agg.id

    node_ids = {n["id"] for n in nodes}

    # --- エッジの付け替えと縮約 ---
    # 非表示ノードへ向かうエッジは、その集約ノードへ付け替える。
    # 同じ端点ペアに複数本できた場合は 1 本へ縮約し、元の id を保持する
    # (v4核§6.4「内部情報を失わない」)。
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for e in kg.get("edges", []):
        a, b = agg_ids.get(e["from"], e["from"]), agg_ids.get(e["to"], e["to"])
        if a not in node_ids or b not in node_ids or a == b:
            continue
        key = (a, b)
        if key in merged:
            m = merged[key]
            m["member_edge_ids"].append(e["id"])
            # 縮約後の代表 glyph は confidence が最も高いものを採用
            if float(e.get("confidence", 0.5) or 0.5) > m["_conf"]:
                m["_conf"] = float(e.get("confidence", 0.5) or 0.5)
                m["glyph"] = e.get("glyph", "arrow")
                m["label"] = e.get("label", "")
            continue
        merged[key] = {
            "id": e["id"], "from": a, "to": b,
            "label": e.get("label", ""), "glyph": e.get("glyph", "arrow"),
            "member_edge_ids": [e["id"]],
            "_conf": float(e.get("confidence", 0.5) or 0.5),
            "_src": e,
        }

    edges: list[dict[str, Any]] = []
    for m in merged.values():
        src = m.pop("_src")
        m.pop("_conf")
        if len(m["member_edge_ids"]) == 1:
            m.pop("member_edge_ids")
        # v4核§6.3 の属性を引き継ぐ (縮約時は代表エッジのもの)
        for attr in EDGE_CARRY:
            if src.get(attr) is not None:
                m[attr] = src[attr]
        edges.append(m)

    communities = []
    seen: set[str] = set()
    src_names = {c["id"]: c.get("name", c["id"]) for c in kg.get("communities", [])}
    node_src = {n["id"]: n.get("community_id") for n in kg.get("nodes", [])}
    for n in nodes:
        cid = n["community_id"]
        if cid in seen:
            continue
        seen.add(cid)
        # コミュニティ名は元 KG の名前を引き継ぐ (Leiden は id しか持たない)
        members = [k for k, v in analysis.communities.items() if v == cid]
        origin = next((src_names[node_src[m]] for m in members
                       if node_src.get(m) in src_names), cid)
        is_gap = any(
            c.get("is_gap") for c in kg.get("communities", [])
            if c["id"] in {node_src.get(m) for m in members}
        )
        communities.append({"id": cid, "name": origin, "is_gap": bool(is_gap)})

    return ({"graph_version": kg.get("graph_version", "kg"),
             "nodes": nodes, "edges": edges, "communities": communities},
            [a.to_dict() for a in aggregates])


def build_multilevel_plan(
    kg: dict[str, Any],
    *,
    default_level: str = "standard",
    weights: dict[str, float] | None = None,
    community_names: dict[str, str] | None = None,
    language: str | None = None,
    frozen_communities: dict[str, str] | None = None,
    pinned: Iterable[str] | None = None,
) -> dict[str, Any]:
    """3 レベルを同梱した layout_plan を作る。

    plan["nodes"] / ["edges"] は **detailed の全量**を持ち、各要素の
    `visible_at` でどのレベルに出るかを示す。overview/standard の座標は
    `plan["_levels_layout"]` ではなく `project()` で都度取り出す設計にすると
    切替のたびに再レイアウトが要るため、**各レベルの完成 plan を同梱**する。

    frozen_communities / pinned は編集後の再構成用のパススルー
    (編集/学習設計書 §4.3 / §4.1)。省略時は従来どおり Leiden を再実行する。
    """
    if default_level not in LEVEL_ORDER:
        raise ValueError(f"unknown detail level: {default_level}")

    analysis = analyze(kg, weights=weights, community_names=community_names,
                       frozen_communities=frozen_communities, pinned=pinned)

    level_plans: dict[str, dict[str, Any]] = {}
    level_stats: dict[str, dict[str, int]] = {}
    all_aggregates: dict[str, dict[str, Any]] = {}

    # レイアウト v3 §3a: **detailed を最初に**計算し、その島配置を他レベルの
    # 方位 (アンカー) にする。こうしないとレベルを切り替えたときに島が飛ぶ。
    # `_level_plans` / `levels` への格納は下で LEVEL_ORDER に並べ直すので、
    # 計算順を変えても plan の JSON は 1 バイトも変わらない (grid も semantic も)。
    anchors: dict[str, Any] | None = None
    for level in ANCHOR_ORDER:
        level_kg, aggs = _level_kg(kg, analysis, level)
        plan = compute_layout(level_kg, detail_level=level, anchors=anchors)
        if level == ANCHOR_LEVEL:
            anchors = anchors_from_plan(plan)

        # 集約ノードは見た目を変えて「畳まれている」ことを示す
        agg_lookup = {a["id"]: a for a in aggs}
        for node in plan["nodes"]:
            if node["id"] in agg_lookup:
                node["kind"] = "aggregate"
                node["aggregate_id"] = node["id"]
                node["style"] = dict(AGGREGATE_STYLE)
            else:
                node["kind"] = "concept"
                imp = analysis.importance.get(node["id"])
                # _level_kg が先に載せた importance をいったん外してから付け直す。
                # 値は同じだが、こうしないとキーの並びが変わって plan の JSON が
                # 従来とバイト単位で一致しなくなる (v3 の既定は grid 完全互換)。
                node.pop("importance", None)
                if imp:
                    node["importance"] = imp.to_dict()
                node["visible_at"] = analysis.visible_at(node["id"])

        level_plans[level] = plan
        level_stats[level] = {
            "nodes": len(plan["nodes"]),
            "edges": len(plan["edges"]),
            "aggregates": len(aggs),
        }
        if analysis.pinned:
            level_stats[level]["pinned"] = sum(
                1 for n in analysis.visible[level] if n in analysis.pinned)
        for a in aggs:
            all_aggregates[a["id"]] = a

    # 計算順 (§3a) に関わらず、格納順は LEVEL_ORDER で固定する
    level_plans = {lv: level_plans[lv] for lv in LEVEL_ORDER}
    level_stats = {lv: level_stats[lv] for lv in LEVEL_ORDER}

    # 既定レベルの plan を本体とし、他レベルを同梱する
    base = copy.deepcopy(level_plans[default_level])
    base["levels"] = level_stats
    base["aggregates"] = [all_aggregates[k] for k in sorted(all_aggregates)]
    base["_level_plans"] = {lv: level_plans[lv] for lv in LEVEL_ORDER}
    base["provenance"]["layout_engine"] = "cc_core.detail/1.0 multilevel"
    if language:
        base["provenance"]["language"] = language

    logger.info("multilevel plan built default=%s levels=%s",
                default_level, level_stats)
    return base


def project(plan: dict[str, Any], level: str) -> dict[str, Any]:
    """同梱された plan から指定レベルを取り出す (LLM 呼び出しゼロ)。

    v3 §2.4 の「詳細度の切替は再生成を伴わずクライアント側で完結」を実現する
    唯一の入口。再計算が必要 (同梱が無い) 場合は例外にせず、その旨を示す
    フラグ付きで既定レベルを返す — UI 側で進捗表示に切り替えるため。
    """
    if level not in LEVEL_ORDER:
        raise ValueError(f"unknown detail level: {level}")
    bundled = plan.get("_level_plans", {})
    if level not in bundled:
        logger.warning("level %s not bundled; recomputation required", level)
        out = copy.deepcopy(plan)
        out["_needs_recompute"] = True
        return out
    out = copy.deepcopy(bundled[level])
    out["levels"] = plan.get("levels", {})
    out["aggregates"] = [a for a in plan.get("aggregates", [])
                         if any(n["id"] == a["id"] for n in out["nodes"])]
    # detail_note は裁定 AO の注記 (「これ以上は増やせない」)。plan 全体に
    # 掛かる事実なので、どのレベルを取り出しても同じものが付いてくる。
    for key in ("gaps", "detail_note"):
        if key in plan:
            out[key] = plan[key]
    return out


PINNED_OVERFLOW_PREFIX = "user_pinned_overflow"


def check_level_bands(plan: dict[str, Any]) -> list[str]:
    """各レベルのノード数が v3 §2.4 の帯に収まっているか検査する。

    元のグラフが小さくて帯の下限に届かない場合は違反としない
    (「10 概念しかない研究に 10-20 を強制する」のは無意味なため)。

    ユーザーのピン留め (編集/学習設計書 §4.1) で上限を超えた場合は
    `user_pinned_overflow:` 接頭辞を付けて**区別して**報告する。これは
    仕様どおりの挙動であってレイアウトの不具合ではないため、エラーではない。
    """
    problems: list[str] = []
    levels = plan.get("levels", {})
    detailed_total = levels.get("detailed", {}).get("nodes", 0)
    for level in LEVEL_ORDER:
        stats = levels.get(level)
        if not stats:
            continue
        lo, hi = LEVEL_BANDS[level]
        n = stats["nodes"]
        pinned = stats.get("pinned", 0)
        if n > hi:
            if pinned:
                problems.append(
                    f"{PINNED_OVERFLOW_PREFIX}: {level}: {n} ノード (上限 {hi}) — "
                    f"編集でピン留めした {pinned} 件を優先しました")
            else:
                problems.append(f"{level}: {n} ノードは上限 {hi} を超過")
        if n < lo and detailed_total > lo:
            problems.append(f"{level}: {n} ノードは下限 {lo} 未満 (全体 {detailed_total})")
    return problems


def band_problems_are_pins_only(problems: list[str]) -> bool:
    """帯の逸脱がピン留めによるものだけか (受け入れ判定に使う)。"""
    return bool(problems) and all(
        p.startswith(PINNED_OVERFLOW_PREFIX) for p in problems)
