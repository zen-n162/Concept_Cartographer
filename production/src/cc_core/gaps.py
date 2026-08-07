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

## R2a: ギャップ 3 型 (設計書 §9)

R1 の検出信号はグラフのトポロジーだけだった。R2a は層タグ・主張・検証結果が
kg に刻まれるので、**同じ地図から 3 種類のギャップ**を見分けられる:

  structural  L4-L6 のトポロジー。孤立・弱接続・コミュニティ間の断絶
              (R1 からある検出をそのまま写像した。挙動は変えていない)
  discourse   層 B。主張が紐づく概念なのに、手法 (Method/Experiment) の文に
              基づく関係が 1 本も無い = 「何をどう調べたか」が資料に無い
  causal      層 C。causes 候補として検証にかけたが裏付けが足りず、相関
              (correlates) 止まりになった関係 = 機序が未確立

`gap_type` は**検出信号の種類**で、`presumed_type` (data/extraction/true/
unknown) は**その原因の推定**。別の軸なので両方を持つ。

各候補は Toulmin の 2 項 (`toulmin.grounds` / `toulmin.warrant`) を持つ。
「何を見て」(grounds) 「どういう規則で」(warrant) ギャップと判断したかを
レコード自身に書かせる — ギャップは人間が確定するもの (裁定 8) なので、
判断材料を候補と同じ場所に置いておく必要がある。

**検出は kg だけで完結させる** (サイドカーを読まない)。層タグ・claim_refs・
validation はすべて ⑦meta までに kg へ刻まれているため、`detect_gaps(kg)` の
呼び出し規約 (editing.rebuild_session が使う) を変えずに 3 型が出せる。
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

# R2a §9 の gap_type (検出信号の種類)。presumed_type とは別の軸
GAP_KINDS = ("structural", "discourse", "causal")
KIND_STRUCTURAL, KIND_DISCOURSE, KIND_CAUSAL = GAP_KINDS

# detected_from_layer — どの層の情報から見つけたか。
# structural はグラフ構造 (層ではない) なので v4 の L 番号で書く
LAYER_TOPOLOGY = "L4-L6"
LAYER_DISCOURSE = "layer_B"
LAYER_CAUSAL = "layer_C"

# discourse ギャップの判定に使う層 B タグ (手法の語り口)
METHOD_TAG = "method_of"
# causal ギャップの判定に使う層 C タグ
CAUSES_TAG = "causes"


@dataclass
class GapCandidate:
    """ギャップ候補。v3 §4.6 の 4 点メタデータ + R2a の型情報を必ず持つ。"""

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
    # --- R2a §9 ---
    gap_type: str = KIND_STRUCTURAL       # structural / discourse / causal
    detected_from_layer: str = LAYER_TOPOLOGY
    detection_signal: str = ""            # 機械可読に近い検出信号の要約
    grounds: str = ""                     # Toulmin: 何を見たか
    warrant: str = ""                     # Toulmin: どの規則で判断したか

    def to_dict(self) -> dict[str, Any]:
        out = {
            "gap_id": self.gap_id,
            "status": self.status,
            "confidence": round(self.confidence, 3),
            "presumed_type": self.presumed_type,
            "gap_type": self.gap_type,
            "detected_from_layer": self.detected_from_layer,
            "detection_signal": self.detection_signal,
            "reason": self.reason,
            "toulmin": {"grounds": self.grounds, "warrant": self.warrant},
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


def _tags(element: dict[str, Any], layer: str) -> set[str]:
    """エッジの layer_tags から 1 層ぶんを取り出す (無ければ空集合)。"""
    tags = element.get("layer_tags")
    if not isinstance(tags, dict):
        return set()
    values = tags.get(layer)
    return set(values) if isinstance(values, (list, tuple)) else set()


def _detect_discourse(
    kg: dict[str, Any],
    node_meta: dict[str, Any],
    comm: dict[str, str],
) -> list[GapCandidate]:
    """言説ギャップ (§9): 主張はあるのに手法の文に基づく関係が無い概念。

    層 B の `method_of` は zone (Method / Experiment) から刻まれる
    (layer_assign.ZONE_TO_LAYER_B)。つまり「その概念にぶら下がる関係のうち、
    手法を語る文を根拠にしたものが 1 本も無い」= 主張の手続きが資料に無い。

    層タグが 1 つも無い世代 (R1.5 / layers=False) では claim_refs も付かない
    ので、この検出は自然にゼロ件になる — 旧セッションの挙動は変わらない。
    """
    edges = [e for e in kg.get("edges", []) or () if isinstance(e, dict)]
    incident: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        for key in ("from", "to"):
            nid = str(edge.get(key) or "")
            if nid:
                incident.setdefault(nid, []).append(edge)

    out: list[GapCandidate] = []
    for nid in sorted(node_meta):
        node = node_meta[nid]
        refs = node.get("claim_refs")
        refs = [r for r in refs if r] if isinstance(refs, (list, tuple)) else []
        if not refs:
            continue
        touching = incident.get(nid, [])
        if any(METHOD_TAG in _tags(edge, "layer_B") for edge in touching):
            continue
        label = node.get("label", nid)
        signal = f"claim_refs={len(refs)}, layer_B:{METHOD_TAG}=0/{len(touching)}"
        out.append(GapCandidate(
            gap_id=f"gap-discourse-{nid}",
            confidence=0.55,
            presumed_type="data",
            reason=(f"概念「{label}」には主張が {len(refs)} 件ぶら下がっていますが、"
                    "手法 (Method / Experiment) の文を根拠にした関係が 1 本も"
                    "ありません。主張を支える手続きが資料に書かれていない可能性があります。"),
            evidence_links=_evidence_of(node),
            related_node_ids=[nid],
            community_id=comm.get(nid),
            gap_type=KIND_DISCOURSE,
            detected_from_layer=LAYER_DISCOURSE,
            detection_signal=signal,
            grounds=(f"「{label}」に紐づく主張 {len(refs)} 件に対し、"
                     f"接続する関係 {len(touching)} 本のいずれにも層 B の "
                     f"{METHOD_TAG} が付いていない"),
            warrant=("主張が語られている概念には、その主張を得た手続き "
                     "(Method / Experiment の語り口) が資料内にあるはずだ、"
                     "という前提を置いている"),
        ))
    return out


def _detect_causal(
    kg: dict[str, Any],
    node_meta: dict[str, Any],
    comm: dict[str, str],
    *,
    rejection_log: str | None = None,
) -> list[GapCandidate]:
    """因果ギャップ (§9): causes 候補が裏付け不足で相関止まりになった関係。

    対象は「層 C に causes を持つのに glyph が矢印でない」エッジ。⑤validate の
    判定 (uncertain / rejected) が付いていればそれを信頼度と推定分類に使い、
    無ければ ④relate の降格記録 (causal_check.demoted_from) を見る。

    rejected になったものは rejection_log (§3.3) にも 1 行残っている。
    パスが渡されていれば出典として添える — 「なぜ矢印にならなかったか」の
    原文はそちらにあるため。
    """
    labels = {nid: str(node.get("label") or nid) for nid, node in node_meta.items()}
    out: list[GapCandidate] = []
    for edge in kg.get("edges", []) or ():
        if not isinstance(edge, dict):
            continue
        if CAUSES_TAG not in _tags(edge, "layer_C"):
            continue
        if str(edge.get("glyph") or "") == "arrow":
            continue                       # 因果として点灯済み = ギャップではない
        eid = str(edge.get("id") or "")
        src, dst = str(edge.get("from") or ""), str(edge.get("to") or "")
        validation = edge.get("validation") if isinstance(edge.get("validation"), dict) else {}
        status = str(validation.get("status") or "")
        combined = validation.get("combined")
        if status == "rejected":
            confidence, presumed = 0.6, "true"
            verdict = "⑤validate が裏付けなしと判定 (rejected)"
        elif status == "uncertain":
            confidence, presumed = 0.5, "unknown"
            verdict = "⑤validate が判断保留 (uncertain)"
        elif (edge.get("causal_check") or {}).get("demoted_from") == "arrow":
            confidence, presumed = 0.45, "unknown"
            verdict = "④relate が語彙証拠・独立検証で通さず降格"
        else:
            continue
        links = _evidence_of(node_meta.get(src, {"id": src}))
        if rejection_log and status == "rejected":
            links = links + [{"rejection_log": rejection_log, "target_id": eid}]
        signal = (f"layer_C:{CAUSES_TAG}, glyph={edge.get('glyph')},"
                  f" validation={status or 'none'}"
                  + (f", combined={combined}" if combined is not None else ""))
        out.append(GapCandidate(
            gap_id=f"gap-causal-{eid or src + '-' + dst}",
            confidence=confidence,
            presumed_type=presumed,
            reason=(f"「{labels.get(src, src)}」→「{labels.get(dst, dst)}」は因果の"
                    "候補でしたが裏付けが足りず、相関のままです。"
                    "機序を示す資料が無いか、まだ確かめられていない可能性があります。"),
            evidence_links=links,
            related_node_ids=[i for i in (src, dst) if i],
            community_id=comm.get(src),
            gap_type=KIND_CAUSAL,
            detected_from_layer=LAYER_CAUSAL,
            detection_signal=signal,
            grounds=(f"関係 {eid} は層 C に {CAUSES_TAG} を持つが glyph は "
                     f"{edge.get('glyph')} のまま。{verdict}"),
            warrant=("因果として抽出されながら裏付けを得られなかった対は、"
                     "機序の記述そのものが資料に欠けている、という規則で"
                     "ギャップ候補にしている (相関として地図には残す)"),
        ))
    return out


def detect_gaps(
    kg: dict[str, Any],
    communities: dict[str, str] | None = None,
    *,
    isolated_degree: int = 0,
    weak_degree: int = 1,
    rejection_log: str | None = None,
) -> list[GapCandidate]:
    """知識グラフからギャップ候補を検出する (R2a: 3 型)。

    ①〜④ は R1 からある構造ギャップ (L4-L6 のトポロジー)。⑤⑥ が R2a の
    言説・因果ギャップで、層タグが刻まれていない世代では自然にゼロ件になる
    (旧セッションの `detect_gaps(kg)` は R1 と同じ結果を返す)。
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
                detection_signal=f"degree={deg}",
                grounds=f"「{node.get('label', nid)}」の次数が {deg} (関係が無い)",
                warrant="資料で語られた概念は他の概念と何らかの関係を持つはずだ、"
                        "という前提を置いている",
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
                detection_signal=f"degree={deg} (<= {weak_degree})",
                grounds=f"「{node.get('label', nid)}」の次数が {deg} 本しかない",
                warrant="接続の薄い概念は、周辺の関係がまだ書かれていない領域を"
                        "指している可能性が高い、という規則で候補にしている",
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
                    detection_signal=f"crossing_edges({a},{b})=0",
                    grounds=f"テーマ {a} ({len(by_comm[a])} 概念) と {b} "
                            f"({len(by_comm[b])} 概念) をまたぐ関係が 0 本",
                    warrant="2 つ以上の概念を持つテーマどうしが全く繋がらないのは、"
                            "橋渡しの研究が未着手であることを示す、という規則",
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
                detection_signal="community.is_gap=true",
                grounds=f"抽出が「{c.get('name', c['id'])}」を is_gap として返した",
                warrant="抽出側が言及の薄さを認識したテーマは候補として提示する "
                        "(断定はしない)",
            ))

    # --- ⑤ 言説ギャップ / ⑥ 因果ギャップ (R2a §9) ---
    gaps.extend(_detect_discourse(kg, node_meta, comm))
    gaps.extend(_detect_causal(kg, node_meta, comm, rejection_log=rejection_log))

    gaps.sort(key=lambda x: (-x.confidence, x.gap_id))
    logger.info("gaps detected n=%d types=%s kinds=%s", len(gaps),
                {t: sum(1 for x in gaps if x.presumed_type == t) for t in GAP_TYPES},
                {k: sum(1 for x in gaps if x.gap_type == k) for k in GAP_KINDS})
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
