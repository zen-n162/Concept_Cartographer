"""オンライン評価の収集と KPI 集計 (実運用計画 §9 / v3 §7.2.1)。

R1 の必須機能。これを欠くと R2 以降のゲート判定 (関係正答率・ギャップ有用率)
が測定不能になり、評価駆動のリリース計画そのものが成立しない。

ラベル体系は v3 §7.2.1 の 2 系統をそのまま採用する (独自ラベルにすると
以降のゲート判定の互換性が失われるため):

  関係    正しい / 誤り / 判断不能        -> RelationVerdict
  ギャップ 有用 / 無意味 / 誤検知          -> ギャップ側は cc_core.gaps が管理

加えて 5 段階満足度と操作ログ (詳細度切替・編集・展開) を記録する。
本文・個人情報は記録しない (ラベルとIDのみ。cc_core.logging_util と同じ方針)。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from cc_core.layers import CAUSAL_GLYPH
from cc_core.logging_util import get_logger

logger = get_logger("cc_core.evaluation")

RELATION_VERDICTS = ("correct", "incorrect", "undecidable")  # v3 §7.2.1
# edit_revert は「編集の取り消し」。correction_rate の分子には**入れない**
# (取り消しは修正の追加ではなく撤回なので、修正率を二重に押し上げてしまう)。
OPERATIONS = ("level_switch", "expand_aggregate", "edit_node", "edit_edge",
              "delete_element", "export", "view_evidence", "edit_revert")


@dataclass
class EvaluationSession:
    """1 回の地図生成に対する評価記録。

    map_id で layout_plan と紐づく。ラベル本文は保持しない。
    """

    map_id: str
    user_id: str
    created_at: str = field(
        default_factory=lambda: dt.datetime.now().isoformat(timespec="seconds"))
    satisfaction: int | None = None                    # 1-5
    relation_verdicts: dict[str, str] = field(default_factory=dict)  # edge_id -> verdict
    operations: list[dict[str, Any]] = field(default_factory=list)
    detail_level_at_start: str = "standard"
    evidence_views: int = 0
    notes_redacted: bool = True

    # ---- 記録 ----
    def rate(self, score: int) -> None:
        if not 1 <= score <= 5:
            raise ValueError("満足度は 1-5")
        self.satisfaction = score

    def judge_relation(self, edge_id: str, verdict: str) -> None:
        if verdict not in RELATION_VERDICTS:
            raise ValueError(f"関係の評価は {RELATION_VERDICTS} のみ: {verdict}")
        self.relation_verdicts[edge_id] = verdict

    def log_operation(self, op: str, **detail: Any) -> None:
        if op not in OPERATIONS:
            raise ValueError(f"未知の操作: {op}")
        # detail に本文が混ざらないよう、値はスカラーのみ許可する
        safe = {k: v for k, v in detail.items()
                if isinstance(v, (str, int, float, bool)) and len(str(v)) <= 64}
        self.operations.append({
            "op": op,
            "at": dt.datetime.now().isoformat(timespec="seconds"),
            **safe,
        })
        if op == "view_evidence":
            self.evidence_views += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "map_id": self.map_id,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "satisfaction": self.satisfaction,
            "relation_verdicts": self.relation_verdicts,
            "operations": self.operations,
            "detail_level_at_start": self.detail_level_at_start,
            "evidence_views": self.evidence_views,
        }


class EvaluationStore:
    """評価記録の追記型ストア (JSONL)。R2 で DB へ移行しても形式は保つ。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, session: EvaluationSession) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(session.to_dict(), ensure_ascii=False) + "\n")
        logger.info("evaluation recorded map=%s judgements=%d",
                    session.map_id, len(session.relation_verdicts))

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in
                self.path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ------------------------------------------------------------- KPI 集計


def relation_error_rate(sessions: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """R1 出口の代理指標「オンライン『誤り』率 ≤30%」(計画 §9)。

    正解セットが無い R1 では、判断不能を除いた判定済みのうち誤りの割合を使う。
    正式な関係正答率 (正解セット測定) は R2。
    """
    counts = {v: 0 for v in RELATION_VERDICTS}
    for s in sessions:
        for verdict in s.get("relation_verdicts", {}).values():
            if verdict in counts:
                counts[verdict] += 1
    judged = counts["correct"] + counts["incorrect"]
    return {
        **counts,
        "judged": judged,
        "error_rate": round(counts["incorrect"] / judged, 3) if judged else None,
        "target": 0.30,
        "meets_target": (counts["incorrect"] / judged <= 0.30) if judged else None,
    }


def satisfaction_rate(sessions: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """R1 出口「満足度 5 段階で 4 以上が 60% 以上」(計画 §9)。"""
    scores = [s["satisfaction"] for s in sessions if s.get("satisfaction")]
    if not scores:
        return {"n": 0, "high_rate": None, "mean": None, "target": 0.60}
    high = sum(1 for x in scores if x >= 4)
    return {
        "n": len(scores),
        "mean": round(sum(scores) / len(scores), 2),
        "high_rate": round(high / len(scores), 3),
        "target": 0.60,
        "meets_target": high / len(scores) >= 0.60,
    }


def correction_rate(sessions: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """修正率 (v3 §7.3)。1 セッションあたりの編集・削除操作の平均。

    減少トレンドの監視が目的なので、絶対値より時系列の比較に使う。
    """
    sessions = list(sessions)
    if not sessions:
        return {"n": 0, "corrections_per_map": None}
    edits = sum(
        1 for s in sessions for op in s.get("operations", [])
        if op["op"] in ("edit_node", "edit_edge", "delete_element")
    )
    return {
        "n": len(sessions),
        "corrections": edits,
        "corrections_per_map": round(edits / len(sessions), 2),
    }


def evidence_display_rate(plan: dict[str, Any]) -> dict[str, Any]:
    """evidence 表示率 (R1 出口 ≥95%, v3 §7.4)。

    「根拠スパンを持つエッジの割合」= UI で出典に辿れるエッジの割合。
    v4核§7 A3「下位に根拠が辿れないエッジは無効」の実装上の測定値。
    """
    edges = plan.get("edges", [])
    if not edges:
        return {"edges": 0, "with_evidence": 0, "rate": None, "target": 0.95}
    with_ev = sum(1 for e in edges if e.get("evidence_span"))
    rate = with_ev / len(edges)
    return {
        "edges": len(edges),
        "with_evidence": with_ev,
        "rate": round(rate, 3),
        "target": 0.95,
        "meets_target": rate >= 0.95,
    }


def causal_precision_log(plan: dict[str, Any]) -> dict[str, Any]:
    """因果ラベル精度の計測開始 (R1) — 3 点セットの通過状況を集計する。

    R1 は「計測開始」が出口条件 (計画 §9)。R2 で正解セットと突き合わせる。

    **ユーザーが編集・追加した関係は分母から除外する** (編集/学習設計書 §2)。
    この KPI は AI の抽出性能を測るものなので、人が手で直したものを混ぜると
    「直せば直すほど精度が上がる」誤った読みになる。
    """
    edges = [e for e in plan.get("edges", [])
             if not str(e.get("origin") or "").startswith("user")]
    user_edges = sum(1 for e in plan.get("edges", [])
                     if str(e.get("origin") or "").startswith("user"))
    checked = [e for e in edges if e.get("causal_check")]
    passed = [e for e in checked if e.get("glyph") == CAUSAL_GLYPH]
    demoted = [e for e in checked
               if e["causal_check"].get("demoted_from") == CAUSAL_GLYPH]
    verified = [e for e in passed
                if e["causal_check"].get("verifier_verdict") == "pass"]
    return {
        "causal_candidates": len(checked),
        "kept_as_causal": len(passed),
        "demoted_to_correlation": len(demoted),
        "independently_verified": len(verified),
        "verification_coverage": (
            round(len(verified) / len(passed), 3) if passed else None),
        "user_edges_excluded": user_edges,
    }


def summarize(plan: dict[str, Any], sessions: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """R1 KPI ダッシュボードの 1 回分スナップショット。"""
    from cc_core.gaps import usefulness_rate

    sessions = list(sessions)
    return {
        "satisfaction": satisfaction_rate(sessions),
        "relation_error": relation_error_rate(sessions),
        "gap_usefulness": usefulness_rate(plan),
        "evidence_display": evidence_display_rate(plan),
        "causal": causal_precision_log(plan),
        "corrections": correction_rate(sessions),
    }
