"""LLM が返した knowledge_graph を契約形へ正規化する。

エージェントは指示どおりの形を返すとは限らない。実際に起きた例 (2026-08-07):
  - evidence_span を **配列ではなく単一オブジェクト**で返した
    → dict を for で回すとキー (文字列) が出て 'str' has no attribute 'get'
  - char_start / char_end を null で返した
    → Work IQ の copilot_chat は文字オフセットを返さないため、そもそも
      エージェントには算出できない。要求する方が誤りだった

方針: **プロンプトで縛るだけに頼らず、受け取り側で必ず正規化する**。
形の揺れを吸収し、何を直したかをログに残す (黙って壊れるより、直した事実が
見えるほうが運用で追える)。正規化しても救えないもの (参照切れ等) は
warnings として返し、呼び出し側が判断する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cc_core.layers import POLARITY, normalize_layer_tags
from cc_core.logging_util import get_logger

logger = get_logger("cc_core.normalize")

# glyph 語彙の**一次定義**。layout.py / editing.py はここを import する
# (同じ集合を 2 か所に書くと必ず片方だけ増えるため。R2a 設計書 §2 の同期リスト)。
VALID_GLYPHS = {"arrow", "wave", "zigzag", "double", "hole", "tension",
                "isa", "partof", "precedes", "question"}
EPISTEMIC = {"asserted", "hedged", "hypothesized", "observed", "concluded"}


@dataclass
class NormalizeReport:
    repairs: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    dropped_edges: list[str] = field(default_factory=list)

    def note(self, key: str, n: int = 1) -> None:
        self.repairs[key] = self.repairs.get(key, 0) + n

    def to_dict(self) -> dict[str, Any]:
        return {"repairs": self.repairs, "warnings": self.warnings,
                "dropped_edges": self.dropped_edges}


def _as_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_evidence_span(raw: Any, report: NormalizeReport) -> list[dict[str, Any]]:
    """evidence_span をオブジェクトの配列へ揃える。

    受け付ける形:
      {...}                      -> [ {...} ]            (単一オブジェクト)
      [ {...}, ... ]             -> そのまま
      "原文の引用"                -> [ {"surface": "..."} ]
      [ "引用", {...} ]           -> 混在も可
    char_start / char_end は数値化できなければ落とす (null を残すと
    スキーマ違反になるうえ、trace back は document 粒度で成立するため)。
    """
    if raw is None:
        return []
    if isinstance(raw, dict):
        report.note("evidence_span: 単一オブジェクト -> 配列")
        raw = [raw]
    elif isinstance(raw, str):
        report.note("evidence_span: 文字列 -> 配列")
        raw = [{"surface": raw}]
    elif not isinstance(raw, list):
        report.note("evidence_span: 未知の型を破棄")
        return []

    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            report.note("evidence_span[]: 文字列 -> オブジェクト")
            item = {"surface": item}
        if not isinstance(item, dict):
            report.note("evidence_span[]: 未知の要素を破棄")
            continue
        span: dict[str, Any] = {}
        doc = item.get("document_id") or item.get("documentId") or item.get("source")
        if doc:
            span["document_id"] = str(doc)
        for key, alt in (("char_start", "charStart"), ("char_end", "charEnd")):
            v = _as_int_or_none(item.get(key, item.get(alt)))
            if v is not None:
                span[key] = v
        if item.get("surface"):
            span["surface"] = str(item["surface"])
        if ("char_start" in span) != ("char_end" in span):
            # 片方だけでは範囲にならないので両方落とす
            span.pop("char_start", None)
            span.pop("char_end", None)
            report.note("evidence_span[]: 片側だけの char 範囲を破棄")
        if span:
            out.append(span)
    return out


def normalize_claim_refs(raw: Any, report: NormalizeReport, where: str) -> list[str]:
    """claim_refs (nanopub id の配列) を型検査する (R2a 設計書 §3.1)。

    中身が実在する主張を指すかはここでは見ない — layers サイドカーは
    まだ書かれていない段階で通ることがあるため、型だけを保証する。
    """
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        report.note(f"{where}: claim_refs が配列でないので破棄")
        return []
    out = [str(v) for v in raw if isinstance(v, (str, int)) and str(v).strip()]
    if len(out) != len(list(raw)):
        report.note(f"{where}: claim_refs の非文字列要素を破棄")
    return out


def normalize_kg(kg: Any) -> tuple[dict[str, Any], NormalizeReport]:
    """LLM 出力の knowledge_graph を契約形へ揃える。

    - nodes / edges / communities が無い・型違いなら空配列に寄せる
    - id / label の欠落は補完 (連番・id 流用)
    - 参照切れエッジ・自己ループは落とす (レイアウトが壊れるため)
    - glyph / epistemic_status の未知値は既定へ丸める
    - evidence_span を配列へ正規化
    """
    report = NormalizeReport()
    if not isinstance(kg, dict):
        raise TypeError(f"knowledge_graph が dict ではありません: {type(kg).__name__}")

    out: dict[str, Any] = {
        "graph_version": str(kg.get("graph_version") or "kg_unknown"),
    }
    # layer_model は R2a の世代印。再取り込み (kg_file 経由) で消さない
    for passthrough in ("source_files", "generated_for", "layer_model"):
        if kg.get(passthrough) is not None:
            out[passthrough] = kg[passthrough]

    # --- nodes ---
    raw_nodes = kg.get("nodes")
    if not isinstance(raw_nodes, list):
        report.warnings.append("nodes が配列でない")
        raw_nodes = []
    nodes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, n in enumerate(raw_nodes):
        if isinstance(n, str):          # ラベルだけ返された場合
            n = {"id": f"c{i + 1:03d}", "label": n}
            report.note("node: 文字列 -> オブジェクト")
        if not isinstance(n, dict):
            report.note("node: 未知の要素を破棄")
            continue
        nid = str(n.get("id") or f"c{i + 1:03d}")
        if nid in seen_ids:
            report.note("node: id 重複を改番")
            nid = f"{nid}-{i}"
        seen_ids.add(nid)
        node = {k: v for k, v in n.items()
                if k not in ("id", "label", "community_id", "evidence_span",
                             "onto_class", "claim_refs")}
        node["id"] = nid
        node["label"] = str(n.get("label") or nid)
        node["community_id"] = str(n.get("community_id") or "comm_000")
        ev = normalize_evidence_span(n.get("evidence_span"), report)
        if ev:
            node["evidence_span"] = ev
        # R2a: onto_class / claim_refs は型検査のみ (値の妥当性は M4/M5 の検証器)
        if n.get("onto_class") is not None:
            if isinstance(n["onto_class"], str) and n["onto_class"].strip():
                node["onto_class"] = n["onto_class"].strip()
            else:
                report.note("node: onto_class が文字列でないので破棄")
        if n.get("claim_refs") is not None:
            refs = normalize_claim_refs(n["claim_refs"], report, "node")
            if refs:
                node["claim_refs"] = refs
        nodes.append(node)
    out["nodes"] = nodes

    # --- edges ---
    raw_edges = kg.get("edges")
    if not isinstance(raw_edges, list):
        report.warnings.append("edges が配列でない")
        raw_edges = []
    edges: list[dict[str, Any]] = []
    seen_edge_ids: set[str] = set()
    for i, e in enumerate(raw_edges):
        if not isinstance(e, dict):
            report.note("edge: 未知の要素を破棄")
            continue
        src, dst = e.get("from"), e.get("to")
        if src not in seen_ids or dst not in seen_ids:
            report.dropped_edges.append(str(e.get("id") or f"r{i + 1:03d}"))
            report.note("edge: 参照切れを破棄")
            continue
        if src == dst:
            report.dropped_edges.append(str(e.get("id") or f"r{i + 1:03d}"))
            report.note("edge: 自己ループを破棄")
            continue
        eid = str(e.get("id") or f"r{i + 1:03d}")
        if eid in seen_edge_ids:
            report.note("edge: id 重複を改番")
            eid = f"{eid}-{i}"
        seen_edge_ids.add(eid)

        edge = {k: v for k, v in e.items()
                if k not in ("id", "from", "to", "label", "glyph",
                             "evidence_span", "epistemic_status", "confidence",
                             "layer_tags", "polarity", "claim_refs")}
        edge.update({"id": eid, "from": str(src), "to": str(dst),
                     "label": str(e.get("label") or "")})
        glyph = e.get("glyph")
        if glyph not in VALID_GLYPHS:
            if glyph is not None:
                report.note(f"edge: 未知の glyph '{glyph}' -> wave")
            edge["glyph"] = "wave"   # 不明なら因果ではなく相関に倒す (安全側)
        else:
            edge["glyph"] = glyph

        ev = normalize_evidence_span(e.get("evidence_span"), report)
        if ev:
            edge["evidence_span"] = ev
        status = e.get("epistemic_status")
        if status in EPISTEMIC:
            edge["epistemic_status"] = status
        elif status is not None:
            report.note("edge: 未知の epistemic_status を破棄")
        conf = e.get("confidence")
        try:
            if conf is not None:
                edge["confidence"] = max(0.0, min(1.0, float(conf)))
        except (TypeError, ValueError):
            report.note("edge: confidence を数値化できず破棄")

        # --- R2a: 層タグ / polarity / claim_refs ---
        # いずれも**無い場合は足さない**。R1.5 世代のエッジに勝手なキーを
        # 生やすと、旧セッションとの差分が読めなくなるため。polarity の充填は
        # ⑦meta (生成パイプライン) の仕事で、正規化はしない。
        if e.get("layer_tags") is not None:
            tags, dropped = normalize_layer_tags(e["layer_tags"])
            edge["layer_tags"] = tags
            for note in dropped:
                report.note(f"edge: layer_tags {note}")
        polarity = e.get("polarity")
        if polarity is not None:
            if polarity in POLARITY:
                edge["polarity"] = polarity
            else:
                report.note(f"edge: 未知の polarity '{polarity}' を破棄")
        if e.get("claim_refs") is not None:
            refs = normalize_claim_refs(e["claim_refs"], report, "edge")
            if refs:
                edge["claim_refs"] = refs
        edges.append(edge)
    out["edges"] = edges

    # --- communities ---
    raw_comms = kg.get("communities")
    if not isinstance(raw_comms, list):
        raw_comms = []
    comms: list[dict[str, Any]] = []
    for i, c in enumerate(raw_comms):
        if isinstance(c, str):
            c = {"id": f"comm_{i:03d}", "name": c}
            report.note("community: 文字列 -> オブジェクト")
        if not isinstance(c, dict):
            continue
        comms.append({
            "id": str(c.get("id") or f"comm_{i:03d}"),
            "name": str(c.get("name") or c.get("id") or f"comm_{i:03d}"),
            "is_gap": bool(c.get("is_gap", False)),
        })
    # ノードが参照するコミュニティで定義が無いものを補う
    defined = {c["id"] for c in comms}
    for n in nodes:
        cid = n["community_id"]
        if cid not in defined:
            comms.append({"id": cid, "name": cid, "is_gap": False})
            defined.add(cid)
            report.note("community: 未定義を補完")
    out["communities"] = comms

    if report.repairs or report.warnings:
        logger.info("kg normalized repairs=%s warnings=%s dropped=%d",
                    report.repairs, report.warnings, len(report.dropped_edges))
    return out, report
