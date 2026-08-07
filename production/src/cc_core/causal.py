"""因果ラベルの検証と矛盾の非断定化 (実運用計画 裁定 7)。

背景 (v4核§4.2): 一般の LLM は「相関 vs 因果」を安定して区別しない。
LLM の判定信頼度だけで causes を付けると、相関が因果へ過剰昇格して
研究判断を誤らせる。v4実§3.6 はこれを 3 点セットで防ぐ:

  (1) LLM 候補抽出 → (2) causal cue lexicon フィルタ → (3) 独立検証器

R1 はこの簡易版を実装する:
  (2) は Pearl の Ladder of Causation 第 2〜3 段 (介入・反事実) と機序記述の
      語彙証拠を根拠スパンから探す。相関表現しか無ければ通さない。
  (3) は描画検証と同じ「別モデル判定」パターン (PoC で実証済み) を使う。
      検証器が無い環境では語彙証拠のみで暫定通過とし、記録に残す。
完全な 3 段バリデーション (DeBERTa 系 NLI + オントロジー整合性) は R2。

矛盾 (zigzag/⚡) の扱い:
矛盾検出は L8 Rhetorical Layer でのみ行うのが v4 の原則 (v4実§3.6/§3.8)。
L8 が無い R1 では矛盾を断定せず、「テンション候補」として非断定スタイル
(灰・破線・? 接頭) に降格する。R2 で L8 導入後に ⚡ を有効化する。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from cc_core.logging_util import get_logger

logger = get_logger("cc_core.causal")

# --- causal cue lexicon (v4実§3.6) ---
# Ladder 第2段=介入 / 第3段=反事実 / 機序記述 のいずれかに該当する語彙。
# 相関表現 (correlates with / 関連する 等) は**意図的に含めない**。
CAUSAL_CUES_JA: dict[str, list[str]] = {
    "mechanism": [
        "機序", "メカニズム", "を介して", "を通じて", "経路により", "作用機構",
        "媒介して", "仕組みにより", "原理により",
    ],
    "intervention": [
        "介入", "操作すると", "変化させると", "投与", "印加", "制御することで",
        "を加えると", "を除去すると", "阻害すると", "処理により",
    ],
    "counterfactual": [
        "なければ", "しなければ", "でなかったら", "仮に", "反事実",
        "生じなかった", "起こらなかった",
    ],
    "causal_verb": [
        "引き起こす", "もたらす", "誘発", "生じさせる", "招く", "起因",
        "によって決ま", "を決定づけ", "支配する",
    ],
}
CAUSAL_CUES_EN: dict[str, list[str]] = {
    "mechanism": ["mechanism by which", "mediates", "pathway through", "via the",
                  "through which"],
    "intervention": ["intervening with", "when we intervene", "administering",
                     "knockout", "ablation", "upon applying", "by removing"],
    "counterfactual": ["had x not", "would not have", "counterfactual", "if not for"],
    "causal_verb": ["causes", "induces", "leads to", "triggers", "results in",
                    "gives rise to", "drives"],
}

# 相関しか示さない表現 (これだけなら因果に昇格させない)
CORRELATION_ONLY = [
    "相関", "関連", "対応関係", "同時に", "併存", "傾向がある",
    "correlat", "associated with", "co-occur", "linked to",
]


@dataclass
class CausalCheck:
    """因果判定の記録。UI とログの両方で「なぜ因果と認めたか」を説明する。"""

    passed: bool
    lexicon_hit: list[str] = field(default_factory=list)
    verifier_verdict: str = "skipped"   # pass | fail | skipped
    demoted_from: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "lexicon_hit": self.lexicon_hit,
            "verifier_verdict": self.verifier_verdict,
        }
        if self.demoted_from:
            out["demoted_from"] = self.demoted_from
        if self.reason:
            out["reason"] = self.reason
        return out


def find_causal_cues(text: str) -> list[str]:
    """根拠テキストから Ladder 第2〜3段・機序の語彙証拠を探す。

    戻り値は "category:語" の形 (例: "mechanism:機序")。
    """
    if not text:
        return []
    lowered = text.lower()
    hits: list[str] = []
    for lexicon in (CAUSAL_CUES_JA, CAUSAL_CUES_EN):
        for category, words in lexicon.items():
            for w in words:
                if w in lowered or w in text:
                    hits.append(f"{category}:{w}")
    return sorted(set(hits))


def has_only_correlation_language(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(w in lowered or w in text for w in CORRELATION_ONLY)


def _edge_text(edge: dict[str, Any]) -> str:
    """語彙検査の対象テキスト: 根拠スパンの surface + ラベル。

    v4実§3.6 は「根拠スパンから検出できる場合のみ」と定めるため、
    根拠スパンを一次情報とし、ラベルは補助に留める。
    """
    parts: list[str] = []
    spans = edge.get("evidence_span") or []
    # 正規化前のデータが渡ってきても壊れないようにする (単一オブジェクト・
    # 文字列で返してくるエージェントが実在した。cc_core.normalize も参照)
    if isinstance(spans, dict):
        spans = [spans]
    elif isinstance(spans, str):
        spans = [{"surface": spans}]
    elif not isinstance(spans, (list, tuple)):
        spans = []
    for span in spans:
        if isinstance(span, str):
            parts.append(span)
        elif isinstance(span, dict) and span.get("surface"):
            parts.append(str(span["surface"]))
    if edge.get("label"):
        parts.append(str(edge["label"]))
    return " ".join(parts)


VerifierFn = Callable[[dict[str, Any], str], bool]
"""独立検証器: (edge, evidence_text) -> 因果として妥当か。

cc_orchestrator 側で別モデルのエージェント判定を注入する。
None の場合は語彙証拠のみで暫定通過し、記録に skipped を残す。
"""


def validate_causal_edge(
    edge: dict[str, Any],
    *,
    verifier: VerifierFn | None = None,
    require_verifier: bool = False,
) -> CausalCheck:
    """1 本の因果エッジに 3 点セット (簡易版) を適用する。"""
    text = _edge_text(edge)
    hits = find_causal_cues(text)

    if not hits:
        reason = ("根拠スパンに機序・介入・反事実の語彙証拠が無い"
                  if text else "根拠スパンが無い")
        if has_only_correlation_language(text):
            reason = "相関表現のみで因果の語彙証拠が無い"
        return CausalCheck(False, [], "skipped", "arrow", reason)

    if verifier is None:
        if require_verifier:
            return CausalCheck(False, hits, "skipped", "arrow",
                               "独立検証器が利用できないため因果を保留")
        return CausalCheck(True, hits, "skipped", None,
                           "語彙証拠あり (独立検証は未実施)")

    try:
        ok = bool(verifier(edge, text))
    except Exception as exc:  # 検証器の失敗で因果を通さない (安全側)
        logger.warning("causal verifier failed: %s", type(exc).__name__)
        return CausalCheck(False, hits, "fail", "arrow",
                           f"独立検証器エラー ({type(exc).__name__})")

    if ok:
        return CausalCheck(True, hits, "pass", None, "語彙証拠 + 独立検証を通過")
    return CausalCheck(False, hits, "fail", "arrow", "独立検証器が因果を否定")


def apply_relation_policy(
    kg: dict[str, Any],
    *,
    verifier: VerifierFn | None = None,
    enable_contradiction: bool = False,
    require_verifier: bool = False,
) -> tuple[dict[str, Any], dict[str, int]]:
    """knowledge_graph 全体に R1 の関係表示ポリシーを適用する。

    - glyph=arrow (因果): 3 点セットを通過しなければ wave (相関) へ降格
    - glyph=zigzag (矛盾): enable_contradiction=False (R1 既定) なら
      tension (非断定) へ降格。L8 導入後に True にする
    戻り値は (適用後の kg, 集計)。元の kg は変更しない。
    """
    out = {**kg, "edges": []}
    stats = {"causal_kept": 0, "causal_demoted": 0,
             "contradiction_demoted": 0, "unchanged": 0,
             "override_allow": 0, "override_deny": 0}

    for edge in kg.get("edges", []):
        e = dict(edge)
        glyph = e.get("glyph", "arrow")
        override = e.get("causal_override")

        if glyph == "arrow" and override in ("allow", "deny"):
            # 過去の修正による確定 (編集/学習設計書 §5.3 の 3)。
            # ユーザーが一度判断した対に、毎回 LLM 検証をかけ直さない
            # (検証コストの削減 + 人間が最終権威)。
            hits = find_causal_cues(_edge_text(e))
            if override == "allow":
                e["causal_check"] = {
                    "lexicon_hit": hits, "verifier_verdict": "skipped",
                    "reason": "過去の修正で因果と確定 (ユーザー) — 独立検証をスキップ",
                }
                stats["causal_kept"] += 1
                stats["override_allow"] += 1
            else:
                e["glyph"] = "wave"
                e["label"] = _demote_label(e.get("label", ""), "user_override")
                e["causal_check"] = {
                    "lexicon_hit": hits, "verifier_verdict": "skipped",
                    "demoted_from": "arrow",
                    "reason": "過去の修正で因果を否定 (ユーザー) — 独立検証をスキップ",
                }
                stats["causal_demoted"] += 1
                stats["override_deny"] += 1

        elif glyph == "arrow":
            check = validate_causal_edge(
                e, verifier=verifier, require_verifier=require_verifier)
            e["causal_check"] = check.to_dict()
            if check.passed:
                stats["causal_kept"] += 1
            else:
                e["glyph"] = "wave"
                e["label"] = _demote_label(e.get("label", ""), check.reason)
                stats["causal_demoted"] += 1
        elif glyph == "zigzag" and not enable_contradiction:
            e["glyph"] = "tension"
            e["causal_check"] = {
                "verifier_verdict": "skipped",
                "demoted_from": "zigzag",
                "reason": "矛盾判定は L8 (R2) で行う。R1 では候補として非断定表示",
            }
            stats["contradiction_demoted"] += 1
        else:
            stats["unchanged"] += 1

        out["edges"].append(e)

    logger.info("relation policy applied %s", stats)
    return out, stats


def _demote_label(label: str, reason: str) -> str:
    """降格したエッジのラベルに、断定を弱める印を残す。"""
    label = re.sub(r"^[〜⚡⇒?]\s*", "", label or "").strip()
    if not label:
        return "関連"
    return label
