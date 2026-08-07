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

import os
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


# ------------------------------------------- 深掘り断片の統合 (裁定 AE)
#
# 抽出が薄かったときだけ走る 1 call ぶんの追加抽出を、既存の KG へ畳み込む。
# 形の修復と同族の処理なのでここに置く (「LLM が返したものを受け取り側で
# 揃える」責務が 2 ファイルに散らない)。
#
# 断片は**別の呼び出し**なので、id 空間は既存 KG と無関係に採番されている。
# 同じ概念を別 id で返すことも、既存概念を**ラベルで**参照することもある。
# したがって統合の軸は id ではなく **正規化ラベル**にする (セッションを跨ぐ
# 照合はすべて正規化ラベルで行う、という editing.py §5.1 と同じ流儀)。

EXTRACT_MAX_DEFAULT = 100
ENV_EXTRACT_MAX = "CC_EXTRACT_MAX"


def extract_max(override: int | None = None) -> int:
    """統合後のノード上限 (`CC_EXTRACT_MAX`、既定 100)。

    detailed の帯上限 (community.LEVEL_BANDS) と同じ 100。これを超えたノードは
    どの詳細度でも描かれないので、運んでも保存とレイアウト計算が重くなるだけ。

    **呼び出しのたびに環境変数を読む** — Web は常駐プロセスなので、import 時に
    固定すると再起動なしに変えられない。読めない値は既定へ倒して警告を出す
    (黙って 0 にすると全ノードが消える)。
    """
    if override is not None:
        return max(1, int(override))
    raw = os.environ.get(ENV_EXTRACT_MAX, "").strip()
    if not raw:
        return EXTRACT_MAX_DEFAULT
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("%s=%r を整数として読めません: 既定 %d を使います",
                       ENV_EXTRACT_MAX, raw, EXTRACT_MAX_DEFAULT)
        return EXTRACT_MAX_DEFAULT


@dataclass
class MergeReport:
    """深掘り断片を統合した内訳 (summary["extraction"]["merge"])。

    「何個増えたか」だけでなく「何を捨てたか」も残す。捨てた事実が見えないと、
    深掘りが効いていないのか、効いたが統合で落ちたのかを後から切り分けられない。
    """

    added_nodes: int = 0        # 新しい概念として採用した断片ノード
    added_edges: int = 0        # 採用した断片エッジ
    duplicate_nodes: int = 0    # 正規化ラベルが既出だったので捨てた断片ノード
    label_resolved: int = 0     # 端点をラベルで既存ノードへ結び直した回数
    dropped_edges: list[str] = field(default_factory=list)   # 端点を解決できず破棄
    capped_nodes: int = 0       # CC_EXTRACT_MAX 超過で後着順に切ったノード
    capped_edges: int = 0       # 切られたノードに繋がっていたエッジ
    notes: dict[str, int] = field(default_factory=dict)

    def note(self, key: str, n: int = 1) -> None:
        self.notes[key] = self.notes.get(key, 0) + n

    def to_dict(self) -> dict[str, Any]:
        return {"added_nodes": self.added_nodes, "added_edges": self.added_edges,
                "duplicate_nodes": self.duplicate_nodes,
                "label_resolved": self.label_resolved,
                "dropped_edges": self.dropped_edges,
                "capped_nodes": self.capped_nodes,
                "capped_edges": self.capped_edges,
                "notes": self.notes}


def _id_allocator(prefix: str, width: int, used: set[str]):
    """`prefix + 連番` の id を、使用済みを避けて配る。"""
    counter = 0

    def alloc() -> str:
        nonlocal counter
        while True:
            counter += 1
            candidate = f"{prefix}{counter:0{width}d}"
            if candidate not in used:
                used.add(candidate)
                return candidate
    return alloc


def merge_extraction(
    kg: Any, fragment: Any, *, max_nodes: int | None = None,
) -> tuple[dict[str, Any], MergeReport]:
    """深掘り抽出の断片を既存 KG へ統合する (裁定 AE)。

    - **正規化ラベルで重複排除**: 既出の概念を別 id で返してきても増やさない
    - **id は振り直す**: 断片の c001 と既存の c001 は別物なので必ず採番し直す
    - **エッジ端点はラベルでも解決する**: 断片は既存概念を id ではなくラベルで
      指すことがある (指示でそう書かせている)。id → 既存 id → 正規化ラベル の
      順で引き、解決できないエッジは normalize と同じ流儀で**破棄 + 報告**
    - **上限は後着順で切る**: この段には重要度がまだ無いので、順位付けの根拠が
      無いまま importance 風の切り方をしない (後から来たものを落とす)

    戻り値は (統合後の KG, MergeReport)。統合後は必ず `normalize_kg` を通すので、
    断片側の形の揺れ (evidence_span が単一オブジェクト等) もここで吸収される。
    """
    if not isinstance(kg, dict):
        raise TypeError(f"knowledge_graph が dict ではありません: {type(kg).__name__}")
    if not isinstance(fragment, dict):
        raise TypeError(f"fragment が dict ではありません: {type(fragment).__name__}")

    # editing は VALID_GLYPHS のためにこのモジュールを import している。
    # module 直下で import し返すと循環になるので、使う場所で読む。
    # (ラベル照合キーの定義は 1 つに保つ — 2 か所に書くと必ず片方だけ変わる)
    from cc_core.editing import normalize_label

    report = MergeReport()
    limit = extract_max(max_nodes)

    nodes = [dict(n) for n in (kg.get("nodes") or []) if isinstance(n, dict)]
    edges = [dict(e) for e in (kg.get("edges") or []) if isinstance(e, dict)]
    comms = [dict(c) for c in (kg.get("communities") or []) if isinstance(c, dict)]

    used_nodes = {str(n.get("id")) for n in nodes}
    used_edges = {str(e.get("id")) for e in edges}
    used_comms = {str(c.get("id")) for c in comms}
    new_node_id = _id_allocator("c", 3, used_nodes)
    new_edge_id = _id_allocator("r", 3, used_edges)
    new_comm_id = _id_allocator("comm_", 3, used_comms)

    by_label: dict[str, str] = {}
    for n in nodes:
        by_label.setdefault(normalize_label(n.get("label")), str(n.get("id")))

    # --- communities: 名前が一致すれば既存の島へ寄せる ---
    # id は断片側の勝手な採番なので信用できない (断片の comm_001 と既存の
    # comm_001 が同じテーマとは限らない)。名前で照合し、無ければ島を足す。
    comm_by_name: dict[str, str] = {}
    for c in comms:
        comm_by_name.setdefault(normalize_label(c.get("name")), str(c.get("id")))
    comm_map: dict[str, str] = {}

    def map_community(cid: str, name: str | None = None,
                      is_gap: bool = False) -> str:
        """断片のコミュニティ id を統合後の id へ写す。

        **空の id には島を作らない** — 作ると community_id を書かなかった
        ノードが 1 個ずつ別の島になる。normalize_kg の既定 (comm_000) へ
        任せるのが正しい (「テーマ不明」を 1 か所に集める)。
        """
        if not cid and name is None:
            return ""
        if cid and cid in comm_map:
            return comm_map[cid]
        key = normalize_label(name if name is not None else cid)
        hit = comm_by_name.get(key)
        if hit:
            if cid:
                comm_map[cid] = hit
            return hit
        fresh = cid if (cid and cid not in used_comms) else new_comm_id()
        used_comms.add(fresh)
        # is_gap は**新しく作る島にだけ**効かせる。断片の判断で既存の島を
        # ギャップへ倒すと、元の抽出が下した判断が黙って上書きされる。
        comms.append({"id": fresh, "name": str(name or cid or fresh),
                      "is_gap": bool(is_gap)})
        comm_by_name.setdefault(key, fresh)
        if cid:
            comm_map[cid] = fresh
        return fresh

    for c in fragment.get("communities") or ():
        if isinstance(c, str):
            c = {"id": c, "name": c}
        if not isinstance(c, dict):
            report.note("断片コミュニティ: 未知の要素を破棄")
            continue
        cid = str(c.get("id") or "")
        map_community(cid, str(c.get("name") or cid), bool(c.get("is_gap")))

    # --- nodes: 正規化ラベルで重複排除しつつ採番し直す ---
    id_map: dict[str, str] = {}          # 断片 id -> 統合後 id
    for raw in fragment.get("nodes") or ():
        if isinstance(raw, str):
            raw = {"label": raw}
        if not isinstance(raw, dict):
            report.note("断片ノード: 未知の要素を破棄")
            continue
        label = str(raw.get("label") or "").strip()
        old_id = str(raw.get("id") or "")
        if not label:
            report.note("断片ノード: ラベルが無いので破棄")
            continue
        key = normalize_label(label)
        if key in by_label:
            if old_id:
                id_map[old_id] = by_label[key]     # 既存概念への参照として使う
            report.duplicate_nodes += 1
            continue
        node = {k: v for k, v in raw.items() if k not in ("id", "community_id")}
        node["id"] = new_node_id()
        node["label"] = label
        community = map_community(str(raw.get("community_id") or ""))
        if community:      # 空なら normalize_kg の既定 (comm_000) へ任せる
            node["community_id"] = community
        nodes.append(node)
        by_label[key] = node["id"]
        if old_id:
            id_map[old_id] = node["id"]
        report.added_nodes += 1

    # --- 上限 (後着順で切る) ---
    if len(nodes) > limit:
        report.capped_nodes = len(nodes) - limit
        nodes = nodes[:limit]
        logger.info("merge_extraction capped nodes to %d (dropped %d)",
                    limit, report.capped_nodes)
    kept = {str(n["id"]) for n in nodes}

    def resolve(ref: Any) -> tuple[str | None, str]:
        """端点を統合後の node id へ解決する。戻りは (id, 解決の仕方)。"""
        s = "" if ref is None else str(ref).strip()
        if not s:
            return None, ""
        for candidate, how in ((id_map.get(s), "id"),
                               (s if s in used_nodes else None, "id"),
                               (by_label.get(normalize_label(s)), "label")):
            if candidate:
                return (candidate, how) if candidate in kept else (None, "capped")
        return None, ""

    if report.capped_nodes:
        before = len(edges)
        edges = [e for e in edges
                 if str(e.get("from")) in kept and str(e.get("to")) in kept]
        report.capped_edges += before - len(edges)

    for e in fragment.get("edges") or ():
        if not isinstance(e, dict):
            report.note("断片エッジ: 未知の要素を破棄")
            continue
        src, how_src = resolve(e.get("from"))
        dst, how_dst = resolve(e.get("to"))
        if src is None or dst is None:
            if "capped" in (how_src, how_dst):
                report.capped_edges += 1
            else:
                report.dropped_edges.append(str(e.get("id") or "?"))
                report.note("断片エッジ: 端点を解決できず破棄")
            continue
        report.label_resolved += (how_src == "label") + (how_dst == "label")
        edge = {k: v for k, v in e.items() if k not in ("id", "from", "to")}
        edge["id"] = new_edge_id()       # 断片の id は必ず振り直す (衝突回避)
        edge["from"], edge["to"] = src, dst
        edges.append(edge)
        report.added_edges += 1

    merged = {k: v for k, v in kg.items()
              if k not in ("nodes", "edges", "communities")}
    files = list(kg.get("source_files") or ())
    for f in fragment.get("source_files") or ():
        if f not in files:
            files.append(f)
    if files:
        merged["source_files"] = files
    merged["nodes"], merged["edges"], merged["communities"] = nodes, edges, comms

    out, norm = normalize_kg(merged)
    for key, n in norm.repairs.items():
        report.note(f"normalize: {key}", n)
    report.dropped_edges.extend(norm.dropped_edges)
    # 自己ループ等で normalize に落とされたぶんを「足した」から引く
    # (報告の数字と実物がずれると、次に読む人が実装を疑うことになる)
    lost = len(edges) - len(out["edges"])
    if lost > 0:
        report.added_edges = max(0, report.added_edges - lost)
    logger.info("merge_extraction: +%d nodes (dup %d) +%d edges (dropped %d)",
                report.added_nodes, report.duplicate_nodes,
                report.added_edges, len(report.dropped_edges))
    return out, report
