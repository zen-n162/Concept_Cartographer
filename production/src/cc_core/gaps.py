"""ギャップ候補の生成と人間による確定ワークフロー (実運用計画 裁定 8 / G4)。

v3 §4.6 (G4) は「ギャップを提示するだけでなく、ユーザー検証を必須にする」と
定める。区別せずに提示すると誤った研究判断 (自動化バイアス) を誘発するため。
v4核§8 も「確定は人間が行う」を設計原則とする。

R1 で提供するもの:
  - ギャップ候補の 4 点メタデータ (v3 §4.6): 信頼度 / 推定分類 / 提示理由 / 出典リンク
  - confirm / dismiss の 2 値確定操作 (v4実§7.4 の status 遷移)
  - 確定操作はギャップ有用率 KPI の分母定義でもある (計画 §9)

三分類 (Data / Extraction / True) の推定は R1 では**候補提示のみ**で、
5 型 (v4実§6) への精緻化は R2〜R3。

検出信号 (R1 = L4-L6 のトポロジーのみ, v4実§6 の構造ギャップ相当):
  - 孤立ノード / 次数が極端に低いノード
  - コミュニティ間の接続が無い、または極端に弱い
  - LLM が is_gap=true と付けたコミュニティ (抽出側の明示的ギャップ)
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from cc_core.community import build_graph
from cc_core.logging_util import get_logger

logger = get_logger("cc_core.gaps")

GAP_STATUS = ("candidate", "confirmed", "dismissed")
GAP_TYPES = ("data", "extraction", "true", "unknown")


@dataclass
class GapCandidate:
    """ギャップ候補。v3 §4.6 の 4 点メタデータを必ず持つ。"""

    gap_id: str
    confidence: float            # ①信頼度スコア
    presumed_type: str           # ②推定分類 (data/extraction/true/unknown)
    reason: str                  # ③提示理由 (なぜギャップと判断したか)
    evidence_links: list[dict[str, Any]] = field(default_factory=list)  # ④出典
    related_node_ids: list[str] = field(default_factory=list)
    community_id: str | None = None
    status: str = "candidate"
    confirmed_by: str | None = None
    confirmed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "gap_id": self.gap_id,
            "status": self.status,
            "confidence": round(self.confidence, 3),
            "presumed_type": self.presumed_type,
            "reason": self.reason,
            "evidence_links": self.evidence_links,
            "related_node_ids": self.related_node_ids,
        }
        if self.community_id:
            out["community_id"] = self.community_id
        if self.confirmed_by is not None:
            out["confirmed_by"] = self.confirmed_by
        if self.confirmed_at is not None:
            out["confirmed_at"] = self.confirmed_at
        return out


def _evidence_of(node: dict[str, Any]) -> list[dict[str, Any]]:
    spans = node.get("evidence_span") or []
    return [{"node_id": node["id"], "span": s} for s in spans[:2]]


def detect_gaps(
    kg: dict[str, Any],
    communities: dict[str, str] | None = None,
    *,
    isolated_degree: int = 0,
    weak_degree: int = 1,
) -> list[GapCandidate]:
    """知識グラフのトポロジーからギャップ候補を検出する。

    R1 の検出信号は L4-L6 のグラフ構造に限る (v4実§6 の構造ギャップ相当)。
    言説・因果・認識論・オントロジーの各ギャップは R2 以降。
    """
    g = build_graph(kg)
    node_meta = {n["id"]: n for n in kg.get("nodes", [])}
    comm = communities or {n["id"]: n.get("community_id", "comm_000")
                           for n in kg.get("nodes", [])}
    gaps: list[GapCandidate] = []

    # --- ① 孤立ノード: 関係が 1 本も無い概念 ---
    for nid in sorted(g.nodes()):
        deg = g.degree(nid)
        if deg <= isolated_degree:
            node = node_meta.get(nid, {"id": nid})
            gaps.append(GapCandidate(
                gap_id=f"gap-isolated-{nid}",
                confidence=0.75,
                presumed_type="data",
                reason=(f"概念「{node.get('label', nid)}」に他概念との関係が抽出されて"
                        "いません。資料内の記述が不足しているか、関係抽出が漏れた可能性があります。"),
                evidence_links=_evidence_of(node),
                related_node_ids=[nid],
                community_id=comm.get(nid),
            ))

    # --- ② 弱接続ノード: 次数が極端に低い (橋渡しが 1 本しかない) ---
    for nid in sorted(g.nodes()):
        deg = g.degree(nid)
        if isolated_degree < deg <= weak_degree:
            node = node_meta.get(nid, {"id": nid})
            gaps.append(GapCandidate(
                gap_id=f"gap-weak-{nid}",
                confidence=0.45,
                presumed_type="unknown",
                reason=(f"概念「{node.get('label', nid)}」の関係が {deg} 本のみで、"
                        "他テーマとの接続が薄い領域です。"),
                evidence_links=_evidence_of(node),
                related_node_ids=[nid],
                community_id=comm.get(nid),
            ))

    # --- ③ コミュニティ間の未接続: 島同士が繋がっていない ---
    by_comm: dict[str, set[str]] = {}
    for nid, cid in comm.items():
        if nid in g:
            by_comm.setdefault(cid, set()).add(nid)
    cids = sorted(by_comm)
    for i in range(len(cids)):
        for j in range(i + 1, len(cids)):
            a, b = cids[i], cids[j]
            crossing = sum(1 for u, v in g.edges()
                           if (u in by_comm[a] and v in by_comm[b])
                           or (u in by_comm[b] and v in by_comm[a]))
            if crossing == 0 and len(by_comm[a]) >= 2 and len(by_comm[b]) >= 2:
                gaps.append(GapCandidate(
                    gap_id=f"gap-bridge-{a}-{b}",
                    confidence=0.5,
                    presumed_type="true",
                    reason=(f"テーマ {a} と {b} の間に関係が 1 本もありません。"
                            "両者を橋渡しする研究が未着手か、資料が不足している可能性があります。"),
                    evidence_links=[],
                    related_node_ids=sorted(by_comm[a])[:2] + sorted(by_comm[b])[:2],
                    community_id=None,
                ))

    # --- ④ 抽出側が明示したギャップコミュニティ ---
    for c in kg.get("communities", []):
        if c.get("is_gap"):
            members = [n["id"] for n in kg.get("nodes", [])
                       if n.get("community_id") == c["id"]]
            gaps.append(GapCandidate(
                gap_id=f"gap-declared-{c['id']}",
                confidence=0.6,
                presumed_type="true",
                reason=(f"テーマ「{c.get('name', c['id'])}」は資料中の言及が薄く、"
                        "抽出時にギャップ候補として識別されました。"),
                evidence_links=[e for m in members[:2]
                                for e in _evidence_of(node_meta.get(m, {"id": m}))],
                related_node_ids=members,
                community_id=c["id"],
            ))

    gaps.sort(key=lambda x: (-x.confidence, x.gap_id))
    logger.info("gaps detected n=%d types=%s", len(gaps),
                {t: sum(1 for x in gaps if x.presumed_type == t) for t in GAP_TYPES})
    return gaps


# ------------------------------------------------------------- 確定操作


class GapDecisionError(ValueError):
    """不正な確定操作 (未知の gap_id・不正な status 遷移)。"""


def apply_decision(
    plan: dict[str, Any],
    gap_id: str,
    decision: str,
    *,
    user_id: str,
    now: str | None = None,
) -> dict[str, Any]:
    """ギャップ候補に confirm / dismiss を適用する (v4実§7.4 の status 遷移)。

    確定済みのものを再度確定しようとした場合は上書きせずエラーにする。
    「誰が・いつ」を必ず記録する (監査と KPI 集計の根拠)。
    """
    if decision not in ("confirm", "dismiss"):
        raise GapDecisionError(f"decision は confirm / dismiss のみ: {decision}")

    gaps = plan.get("gaps", [])
    target = next((g for g in gaps if g["gap_id"] == gap_id), None)
    if target is None:
        raise GapDecisionError(f"gap_id が見つかりません: {gap_id}")
    if target["status"] != "candidate":
        raise GapDecisionError(
            f"既に {target['status']} です (再確定は不可): {gap_id}")

    target["status"] = "confirmed" if decision == "confirm" else "dismissed"
    target["confirmed_by"] = user_id
    target["confirmed_at"] = now or dt.datetime.now().isoformat(timespec="seconds")
    logger.info("gap decision gap=%s -> %s", gap_id, target["status"])
    return target


def usefulness_rate(plan: dict[str, Any]) -> dict[str, Any]:
    """ギャップ有用率 (KPI)。分母は**確定操作が行われた候補**のみ。

    未確定を分母に入れると「まだ見ていない」が「無用」と混ざるため、
    計画 §9 のとおり confirm/dismiss を分母定義とする。
    """
    gaps = plan.get("gaps", [])
    confirmed = sum(1 for g in gaps if g["status"] == "confirmed")
    dismissed = sum(1 for g in gaps if g["status"] == "dismissed")
    decided = confirmed + dismissed
    return {
        "total_candidates": len(gaps),
        "decided": decided,
        "confirmed": confirmed,
        "dismissed": dismissed,
        "usefulness_rate": round(confirmed / decided, 3) if decided else None,
    }
