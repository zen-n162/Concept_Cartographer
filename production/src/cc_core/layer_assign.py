"""層タグの刻印 — glyph / zone / L5 出力から layer_tags を組み立てる (R2a 設計書 §8)。

設計書 §8 は `assign_layer_tags` を causal.py へ置くと書いているが、
`apply_relation_policy` (裁定 7 の 3 点セット) と同じファイルに入れると、
R1.5 から凍結されている因果ポリシーの回帰面が広がる。**因果ポリシーには一切
触れない**という本バッチの制約を機械的に守るため、別モジュールに分けた
(呼び出し順は設計どおり: ④relate の後、⑦meta の前)。

このモジュールは **LLM を呼ばない**。材料は 3 つとも既に手元にある:

  (1) 現行 glyph          -> layer_C / layer_D の初期タグ
  (3) zone ラベル          -> layer_B (v4実§4.7 経路B: 新規 LLM 呼び出しなし)
  (4) L5 の関係候補        -> layer_A (is_a / part_of)。**新規エッジは作らない**

§8 の (2) apply_relation_policy はパイプラインの ④relate が既に実行済み、
(5) 検証結果の反映は M5/M6 の担当。

**(1) が要る理由**: 層タグが 1 つでも付くと ⑦meta の投影が走り、規則⑪ の
既定 (wave) に落ちる。zone 由来の layer_B だけを刻むと、検証を通った因果の
矢印が相関へ化ける。glyph 由来の layer_C を先に置くことで、
「タグを刻んでも記号は動かない」(LLM が何も足さなければ挙動不変) が成り立つ。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from cc_core.editing import normalize_label
from cc_core.layers import LAYER_KEYS, normalize_layer_tags
from cc_core.logging_util import get_logger

logger = get_logger("cc_core.layer_assign")

# 層の語彙で表せない記号 (§2)。**タグを刻まない** —
# hole (ギャップ候補) と tension (非断定の対立候補) は投影の対象外にして
# R1 の非断定表示 (裁定 7) をそのまま残す
UNTAGGED_GLYPHS: frozenset[str] = frozenset({"hole", "tension"})

# (1) 現行 glyph -> 層タグの初期値 (§8(1))
GLYPH_TO_TAGS: dict[str, dict[str, list[str]]] = {
    "arrow": {"layer_C": ["causes"]},
    "wave": {"layer_C": ["correlates_with"]},
    "double": {"layer_D": ["corroborates"]},
    "zigzag": {"layer_D": ["refutes"]},
    "isa": {"layer_A": ["is_a"]},
    "partof": {"layer_A": ["part_of"]},
    "precedes": {"layer_C": ["precedes"]},
    "question": {"layer_D": ["questions"]},
}

# (3) zone -> layer_B の写像表 (§8 末尾)。表に無いラベル (Object / Model /
# CONTRAST 等) は**付けない** — 無理に当てるより空のほうが読み手に正直
ZONE_TO_LAYER_B: dict[str, str] = {
    "Result": "result_of",
    "Observation": "result_of",
    "Conclusion": "conclusion_of",
    "Method": "method_of",
    "Experiment": "method_of",
    "Hypothesis": "hypothesis_of",
    "Motivation": "motivation_of",
    "Goal": "motivation_of",
    "Background": "background_of",
    # AZ 7 種のうち写像が一意に決まるものだけ
    "BACKGROUND": "background_of",
    "AIM": "motivation_of",
}


# 部分一致で文を同定してよい最小の長さ。「はい。」のような短文は多くの
# 根拠スパンに含まれてしまい、無関係な zone ラベルを引き当てる
MIN_CONTAINMENT_CHARS = 8


def _norm_text(value: Any) -> str:
    """空白の揺れを潰した照合キー (改行・全角空白の差で突合を落とさない)。"""
    return " ".join(str(value or "").split())


def _similar(a: str, b: str) -> bool:
    """同じ文とみなしてよいか (完全一致、または十分に長い側の部分一致)。"""
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < MIN_CONTAINMENT_CHARS:
        return False
    return a in b or b in a


class ZoneIndex:
    """文 -> zone ラベルの索引。根拠スパンの surface と突き合わせる。

    LLM は根拠を「原文のまま」返す約束だが、実際には前後が欠けたり
    複数文がつながったりする。そこで完全一致で引けなければ部分一致へ落とす。
    部分一致は最初に当たったものを採る (zones の順 = 資料の出現順なので決定的)。
    """

    def __init__(self, zones: Iterable[dict[str, Any]] = ()) -> None:
        self.by_sentence: dict[str, dict[str, Any]] = {}
        self.exact: dict[str, str] = {}
        self.ordered: list[tuple[str, str]] = []
        for zone in zones or ():
            if not isinstance(zone, dict):
                continue
            sid = str(zone.get("sentence_id") or "")
            if sid:
                self.by_sentence[sid] = zone
            text = _norm_text(zone.get("text"))
            label = str(zone.get("zone_label") or "")
            if not text or not label:
                continue
            self.exact.setdefault(text, label)
            self.ordered.append((text, label))

    def label_of_text(self, surface: Any) -> str | None:
        key = _norm_text(surface)
        if not key:
            return None
        hit = self.exact.get(key)
        if hit:
            return hit
        for text, label in self.ordered:
            if _similar(text, key):
                return label
        return None

    def text_of(self, sentence_id: str) -> str:
        zone = self.by_sentence.get(str(sentence_id))
        return _norm_text(zone.get("text")) if zone else ""


def _evidence_surfaces(edge: dict[str, Any]) -> list[str]:
    return [str(s["surface"]) for s in edge.get("evidence_span") or ()
            if isinstance(s, dict) and s.get("surface")]


def _merge_tags(edge: dict[str, Any], additions: dict[str, list[str]]) -> bool:
    """既存の layer_tags に足す (消さない)。変化があれば True。

    正規化は `normalize_layer_tags` に任せる — 語彙表の順序へ整列させ、
    語彙外を落とすのを 1 か所に集めておくため。
    """
    current = edge.get("layer_tags") if isinstance(edge.get("layer_tags"), dict) else {}
    merged: dict[str, list[str]] = {}
    for key in LAYER_KEYS:
        values = list(current.get(key) or ())
        for tag in additions.get(key, ()):
            if tag not in values:
                values.append(tag)
        merged[key] = values
    tags, _dropped = normalize_layer_tags(merged)
    changed = tags != (current or {})
    edge["layer_tags"] = tags
    return changed


def _initial_tags(edge: dict[str, Any]) -> dict[str, list[str]]:
    """(1) 現行 glyph から層タグの初期値を作る。

    ④relate で降格されたエッジ (glyph=wave + causal_check.demoted_from=arrow)
    は「裏付けの足りない causes 候補」なので layer_C に causes を残す。
    こうしておくと投影の規則⑩ が同じ判断を再現し、demoted_from の記録も
    そのまま残る (KPI の連続性)。
    """
    glyph = str(edge.get("glyph") or "wave")
    if glyph == "wave" and (edge.get("causal_check") or {}).get("demoted_from") == "arrow":
        return {"layer_C": ["causes"]}
    return {k: list(v) for k, v in GLYPH_TO_TAGS.get(glyph, {}).items()}


def _add_ref(element: dict[str, Any], nanopub_id: str) -> bool:
    refs = element.get("claim_refs")
    refs = list(refs) if isinstance(refs, (list, tuple)) else []
    if nanopub_id in refs:
        return False
    refs.append(nanopub_id)
    element["claim_refs"] = refs
    return True


def assign_layer_tags(
    kg: dict[str, Any],
    *,
    zones: Sequence[dict[str, Any]] = (),
    claims: Sequence[dict[str, Any]] = (),
    ontology: dict[str, Any] | None = None,
) -> dict[str, int]:
    """kg へ層タグ・onto_class・claim_refs を刻む (§8 の (1)(3)(4))。

    kg は**その場で書き換える** (apply_meta と同じ流儀)。戻り値は集計で、
    summary["layers"] に載せて「機械が何を足したか」を追えるようにする。
    """
    ontology = ontology or {}
    index = ZoneIndex(zones)
    stats = {"edges_tagged": 0, "edges_untagged": 0, "layer_b_from_zones": 0,
             "layer_a_from_llm": 0, "relations_unmatched": 0,
             "onto_class_set": 0, "node_claim_refs": 0, "edge_claim_refs": 0}

    edges = [e for e in kg.get("edges", []) or () if isinstance(e, dict)]
    nodes = [n for n in kg.get("nodes", []) or () if isinstance(n, dict)]

    # --- (1) glyph -> 初期タグ / (3) zone -> layer_B ---
    for edge in edges:
        if str(edge.get("glyph") or "") in UNTAGGED_GLYPHS:
            stats["edges_untagged"] += 1
            continue
        additions = _initial_tags(edge)
        for surface in _evidence_surfaces(edge):
            label = index.label_of_text(surface)
            tag = ZONE_TO_LAYER_B.get(label or "")
            if not tag:
                continue
            layer_b = additions.setdefault("layer_B", [])
            if tag not in layer_b:
                layer_b.append(tag)
                stats["layer_b_from_zones"] += 1
        if _merge_tags(edge, additions):
            stats["edges_tagged"] += 1

    # --- (4) L5: onto_class (ノード) と is_a / part_of (既存エッジへの刻印) ---
    by_label: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        by_label.setdefault(normalize_label(node.get("label")), []).append(node)

    for concept in ontology.get("concepts") or ():
        if not isinstance(concept, dict):
            continue
        for node in by_label.get(normalize_label(concept.get("label")), ()):
            onto = str(concept.get("onto_class") or "").strip()
            if onto and node.get("onto_class") != onto:
                node["onto_class"] = onto
                stats["onto_class_set"] += 1

    node_label = {n.get("id"): normalize_label(n.get("label")) for n in nodes}
    for relation in ontology.get("relations") or ():
        if not isinstance(relation, dict):
            continue
        tag = str(relation.get("relation") or "")
        src, dst = normalize_label(relation.get("from")), normalize_label(relation.get("to"))
        hit = False
        for edge in edges:
            if str(edge.get("glyph") or "") in UNTAGGED_GLYPHS:
                continue
            # **向きが一致するときだけ**刻む。逆向きのエッジに is_a を付けると
            # ◇ が反対を向き、地図が嘘をつく (新規エッジも作らない)
            if node_label.get(edge.get("from")) == src and node_label.get(edge.get("to")) == dst:
                if _merge_tags(edge, {"layer_A": [tag]}):
                    stats["layer_a_from_llm"] += 1
                hit = True
        if not hit:
            stats["relations_unmatched"] += 1

    # --- claim_refs (§3.1): 主張 <-> ノード / エッジ ---
    for claim in claims or ():
        if not isinstance(claim, dict):
            continue
        nanopub = str(claim.get("nanopub_id") or "")
        if not nanopub:
            continue
        assertion = claim.get("assertion") or {}
        for label in assertion.get("related_concepts") or ():
            for node in by_label.get(normalize_label(label), ()):
                if _add_ref(node, nanopub):
                    stats["node_claim_refs"] += 1
        # 主張の根拠文と一致する根拠スパンを持つエッジに紐づける
        sources = [index.text_of(sid)
                   for sid in (claim.get("provenance") or {}).get("source_span") or ()]
        sources = [s for s in sources if s]
        if not sources:
            continue
        for edge in edges:
            surfaces = [_norm_text(s) for s in _evidence_surfaces(edge)]
            if any(_similar(src, surface) for src in sources for surface in surfaces):
                if _add_ref(edge, nanopub):
                    stats["edge_claim_refs"] += 1

    logger.info("layer tags assigned %s", stats)
    return stats
