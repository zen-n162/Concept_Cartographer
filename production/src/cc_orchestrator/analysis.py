"""cc-analysis の呼び出し側 — 文脈ラベル付けと主張抽出 (R2a 設計書 §6)。

このモジュールの本体は**プロンプト構築ではなく「形の修復」**にある。
normalize.py と同じ思想で、LLM 出力は指示どおりの形で返るとは限らない前提に
立ち、受け取り側で必ず検証・修復してから使う:

  - 入力に無い sentence_id が返る    -> 突合して捨てる (幻の文に主張がぶら下がるのを防ぐ)
  - 語彙表に無い zone_label が返る    -> 捨てて報告する (未知ラベルは層 B へ写像できない)
  - confidence が 1.5 や "high"      -> 0〜1 にクランプ、数値化できなければ既定値
  - nanopub_id を LLM が自称する      -> 無視してサーバ側で採番 (run 間で ID を安定させる)

上限は環境変数で絞れる (§6・受け入れ基準 5「LLM 呼び出しが 30 call/run 以下」):

  CC_ZONE_BATCH          1 call あたりの文数            (既定 50)
  CC_ZONE_MAX_SENTENCES  ゾーニングする文の総数上限     (既定 500)
  CC_CLAIMS_MAX          主張の件数上限                 (既定 40)
  CC_CLAIMS_BATCH        claims 1 call あたりの文数     (既定 60)
  CC_CLAIMS_MAX_CALLS    claims の call 数上限          (既定 3)
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from cc_core import layers_store
from cc_core.editing import normalize_label
from cc_core.logging_util import get_logger
from cc_core.mcp_client import extract_json
from cc_core.sentences import SentenceSpan, sentence_id, split_sentences

logger = get_logger("cc_orchestrator.analysis")

AGENT = "cc-analysis"
RunFn = Callable[[str], str]
"""プロンプトを投げて本文を受け取る関数。テストはここをモックに差し替える。"""

# --- zone の語彙 (§6) ---
# CoreSC 11 種が既定。AZ 7 種も受理する (資料によってはこちらで返る)
ZONE_LABELS_CORESC: tuple[str, ...] = (
    "Hypothesis", "Motivation", "Goal", "Object", "Method", "Experiment",
    "Model", "Observation", "Result", "Conclusion", "Background",
)
ZONE_LABELS_AZ: tuple[str, ...] = (
    "AIM", "TEXTUAL", "OWN", "BACKGROUND", "CONTRAST", "BASIS", "OTHER",
)
_ZONE_LOOKUP: dict[str, tuple[str, str]] = {
    **{label.lower(): (label, "CoreSC") for label in ZONE_LABELS_CORESC},
    **{label.lower(): (label, "AZ") for label in ZONE_LABELS_AZ},
}
# Background は CoreSC / AZ 双方にある。既定体系である CoreSC を優先する
_ZONE_LOOKUP["background"] = ("Background", "CoreSC")

# 主張抽出の対象になる zone (§6)
CLAIM_ZONES: frozenset[str] = frozenset(
    {"Result", "Conclusion", "Hypothesis", "Observation"})

# L5 最小: BFO 上位の語彙 (§3.1 の onto_class)
ONTO_CLASSES: tuple[str, ...] = (
    "MaterialEntity", "Process", "Quality", "Role", "InformationEntity",
)
ONTO_UNKNOWN = "UNKNOWN"
ONTO_PREFIX = "bfo:"
_ONTO_LOOKUP: dict[str, str] = {
    **{c.lower(): ONTO_PREFIX + c for c in ONTO_CLASSES},
    **{(ONTO_PREFIX + c).lower(): ONTO_PREFIX + c for c in ONTO_CLASSES},
    ONTO_UNKNOWN.lower(): ONTO_UNKNOWN,
}

# L5 最小で受け取る関係 (層 A のうち、資料に明示されやすい 2 種だけ)
ONTOLOGY_RELATIONS: tuple[str, ...] = ("is_a", "part_of")

MAX_CLAIM_CHARS = 300
DEFAULT_CONFIDENCE = 0.5
EXTRACTION_METHOD = "llm-fewshot"


def _knob(name: str, default: int) -> int:
    """環境変数の上限を読む。壊れた値は既定へ倒す (上限が外れるより安全)。"""
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning("%s の値を数値化できないので既定 %d を使う", name, default)
        return default
    return value if value > 0 else default


def zone_batch() -> int:
    return _knob("CC_ZONE_BATCH", 50)


def zone_max_sentences() -> int:
    return _knob("CC_ZONE_MAX_SENTENCES", 500)


def claims_max() -> int:
    return _knob("CC_CLAIMS_MAX", 40)


def claims_batch() -> int:
    return _knob("CC_CLAIMS_BATCH", 60)


def claims_max_calls() -> int:
    return _knob("CC_CLAIMS_MAX_CALLS", 3)


@dataclass
class AnalysisReport:
    """何を修復し、何を捨て、何 call 使ったか。summary へそのまま載る。"""

    repairs: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    llm_calls: int = 0
    sentence_source: str = "none"

    def note(self, key: str, n: int = 1) -> None:
        self.repairs[key] = self.repairs.get(key, 0) + n

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"llm_calls": self.llm_calls,
                               "sentence_source": self.sentence_source}
        if self.repairs:
            out["repairs"] = dict(self.repairs)
        if self.notes:
            out["notes"] = list(self.notes)
        if self.errors:
            out["errors"] = list(self.errors)
        return out


# ------------------------------------------------------------------ 文の収集


def _doc_fields(doc: Any) -> tuple[str, str]:
    """ingest.Doc / dict のどちらでも (名前, 本文) を取り出す。"""
    if isinstance(doc, dict):
        return str(doc.get("name") or "doc"), str(doc.get("text") or "")
    return str(getattr(doc, "name", "doc")), str(getattr(doc, "text", "") or "")


def _evidence_spans(kg: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for edge in kg.get("edges", []) or ():
        if not isinstance(edge, dict):
            continue
        for span in edge.get("evidence_span") or ():
            if isinstance(span, dict) and span.get("surface"):
                out.append(span)
    return out


def collect_sentences(
    docs: Sequence[Any] | None,
    kg: dict[str, Any] | None,
    *,
    max_sentences: int | None = None,
    report: AnalysisReport | None = None,
) -> list[SentenceSpan]:
    """ゾーニングの入力になる文を集める (§9 の制約つき)。

    第一の入力は **ingest の Doc.text** で、char offset が取れる唯一の場所。
    Work IQ 経由で読んだ資料は Foundry 側にしか本文が無いため、そのぶんは
    `evidence_span.surface` を**疑似文**として補う。疑似文は原文中の位置が
    分からないことがあり (char offset を返さない経路がある)、その場合
    char_start/char_end は surface 内の相対位置になる — この制約は
    report.notes に必ず残す (後から「なぜ offset がずれるのか」を追えるように)。
    """
    report = report or AnalysisReport()
    limit = max_sentences or zone_max_sentences()
    spans: list[SentenceSpan] = []
    seen: set[str] = set()
    counters: dict[str, int] = {}

    for doc in docs or ():
        name, text = _doc_fields(doc)
        for span in split_sentences(text, name):
            if span.text in seen:
                continue
            seen.add(span.text)
            counters[name] = counters.get(name, 0) + 1
            spans.append(span)
    from_documents = len(spans)

    for span in _evidence_spans(kg or {}):
        doc_id = str(span.get("document_id") or "evidence")
        base = span.get("char_start")
        base = int(base) if isinstance(base, int) else None
        for piece in split_sentences(str(span["surface"]), doc_id):
            if piece.text in seen:
                continue
            seen.add(piece.text)
            idx = counters.get(doc_id, 0)
            counters[doc_id] = idx + 1
            start = piece.char_start + (base or 0)
            spans.append(SentenceSpan(
                sentence_id=sentence_id(doc_id, idx, piece.text),
                text=piece.text, char_start=start,
                char_end=start + (piece.char_end - piece.char_start),
                document_id=doc_id))
    from_evidence = len(spans) - from_documents

    if from_documents and from_evidence:
        report.sentence_source = "mixed"
    elif from_documents:
        report.sentence_source = "documents"
    elif from_evidence:
        report.sentence_source = "evidence_span"
    else:
        report.sentence_source = "none"

    if from_evidence:
        report.notes.append(
            f"ローカル本文の無い資料 {from_evidence} 文は evidence_span の "
            "surface を疑似文としてゾーニングした (char offset は原文基準ではない)")
    if len(spans) > limit:
        report.notes.append(
            f"文が {len(spans)} 件あるため先頭 {limit} 件のみを対象にした "
            f"(CC_ZONE_MAX_SENTENCES={limit})")
        spans = spans[:limit]
    return spans


# ------------------------------------------------------------------ zone


def _batched(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _payload(task: str, **body: Any) -> str:
    """cc-analysis へ渡す入力。JSON 1 個だけを送る (§6)。"""
    return ("次の JSON を処理し、JSON のみで応答してください。\n"
            + json.dumps({"task": task, **body}, ensure_ascii=False))


def _as_confidence(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONFIDENCE
    return max(0.0, min(1.0, value))


def _as_list(raw: Any, *keys: str) -> list[Any]:
    """{"labels": [...]} でも [...] でも受け取れるようにする。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in keys:
            if isinstance(raw.get(key), list):
                return raw[key]
    return []


def repair_zone_labels(
    raw: Any,
    batch: Sequence[SentenceSpan],
    report: AnalysisReport,
) -> list[dict[str, Any]]:
    """zone task の出力を修復して §3.2 の zones レコードにする。

    text / document_id / char offset は **LLM の返り値を使わない**。入力の
    SentenceSpan から引き直す — 原文の同一性を LLM に委ねないため。
    """
    known = {span.sentence_id: span for span in batch}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _as_list(raw, "labels", "zones", "results"):
        if not isinstance(item, dict):
            report.note("zone: 未知の要素を破棄")
            continue
        sid = str(item.get("sentence_id") or "")
        span = known.get(sid)
        if span is None:
            report.note("zone: 入力に無い sentence_id を破棄")
            continue
        if sid in seen:
            report.note("zone: 同じ文への重複ラベルを破棄")
            continue
        resolved = _ZONE_LOOKUP.get(str(item.get("zone_label") or "").strip().lower())
        if resolved is None:
            report.note(f"zone: 未知の zone_label '{item.get('zone_label')}' を破棄")
            continue
        label, default_system = resolved
        system = str(item.get("zone_system") or "").strip()
        if system not in ("CoreSC", "AZ"):
            if system:
                report.note("zone: 未知の zone_system を既定へ丸め")
            system = default_system
        seen.add(sid)
        out.append({"sentence_id": sid, "text": span.text, "zone_label": label,
                    "zone_system": system,
                    "confidence": _as_confidence(item.get("confidence")),
                    "document_id": span.document_id,
                    "char_start": span.char_start, "char_end": span.char_end})
    return out


def run_zone(
    run: RunFn,
    sentences: Sequence[SentenceSpan],
    *,
    report: AnalysisReport,
    batch_size: int | None = None,
) -> list[dict[str, Any]]:
    """文脈ラベル付け (§6 の task: zone)。50 文/call でバッチ処理する。

    1 バッチが失敗しても他のバッチは続ける — 資料の一部がラベル無しでも
    地図は作れるので、ここで全体を落とさない (失敗は report.errors に残る)。
    """
    zones: list[dict[str, Any]] = []
    size = batch_size or zone_batch()
    for batch in _batched(list(sentences), size):
        payload = _payload("zone", sentences=[
            {"sentence_id": s.sentence_id, "text": s.text} for s in batch])
        try:
            raw = extract_json(run(payload))
            report.llm_calls += 1
        except Exception as exc:
            report.errors.append(f"zone: {type(exc).__name__}")
            logger.warning("zone batch failed: %s", type(exc).__name__)
            continue
        zones.extend(repair_zone_labels(raw, batch, report))
    logger.info("zone done sentences=%d zoned=%d calls=%d",
                len(sentences), len(zones), report.llm_calls)
    return zones


# ------------------------------------------------------------------ claims


def _concept_index(kg: dict[str, Any]) -> dict[str, str]:
    """正規化ラベル -> 実在ノードの表示ラベル (§6 の related_concepts 照合)。"""
    index: dict[str, str] = {}
    for node in kg.get("nodes", []) or ():
        if isinstance(node, dict) and node.get("label"):
            index.setdefault(normalize_label(node["label"]), str(node["label"]))
    return index


def _match_concepts(raw: Any, index: dict[str, str],
                    report: AnalysisReport, where: str) -> list[str]:
    """LLM が挙げた概念名を**実在ノードのラベル**へ丸める (無いものは捨てる)。"""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for value in raw:
        label = index.get(normalize_label(value))
        if label is None:
            report.note(f"{where}: 実在しない概念名を破棄")
            continue
        if label not in out:
            out.append(label)
    return out


def repair_claims(
    raw: Any,
    batch: Sequence[dict[str, Any]],
    concepts: dict[str, str],
    report: AnalysisReport,
    *,
    timestamp: str,
) -> list[dict[str, Any]]:
    """claims task の出力を §3.2 の claims レコードへ修復する。

    - 根拠文が 1 つも突合できない主張は**捨てる** (出所の無い主張は
      nanopub にできず、後段の検証も掛けられないため)
    - nanopub_id はサーバ側採番。source_span はソートしてから畳む —
      LLM が返す順序に ID が依存すると run 間で不安定になる
    - validation は書かない。⑤validate (M5) が走っていない run で
      検証結果の枠だけ作ると「検証済み」と誤読される (validator_ids と同じ思想)
    """
    known = {str(s["sentence_id"]): s for s in batch}
    out: list[dict[str, Any]] = []
    for item in _as_list(raw, "claims", "results"):
        if not isinstance(item, dict):
            report.note("claims: 未知の要素を破棄")
            continue
        text = str(item.get("claim_text") or "").strip()
        if not text:
            report.note("claims: claim_text が空の主張を破棄")
            continue
        if len(text) > MAX_CLAIM_CHARS:
            report.note("claims: 長すぎる claim_text を切り詰め")
            text = text[:MAX_CLAIM_CHARS]
        source = item.get("source_sentence_ids")
        if isinstance(source, str):
            source = [source]
        span_ids = sorted({str(s) for s in (source or []) if str(s) in known})
        if not span_ids:
            report.note("claims: 根拠文が突合できない主張を破棄")
            continue
        doc_ids = [known[s].get("document_id", "") for s in span_ids]
        out.append({
            "nanopub_id": layers_store.nanopub_id(text, span_ids),
            "assertion": {
                "claim_id": "",                       # 採番は呼び出し側 (通し番号)
                "claim_text": text,
                "is_underspecified": bool(item.get("is_underspecified")),
                "related_concepts": _match_concepts(
                    item.get("related_concepts"), concepts, report, "claims"),
            },
            "provenance": {"source_span": span_ids, "extractor_id": AGENT,
                           "extraction_timestamp": timestamp,
                           "extraction_method": EXTRACTION_METHOD},
            "pub_info": {"document_id": next((d for d in doc_ids if d), "")},
        })
    return out


def repair_ontology(
    raw: Any,
    concepts: dict[str, str],
    report: AnalysisReport,
) -> dict[str, list[dict[str, Any]]]:
    """L5 最小: onto_class と is_a / part_of 候補を修復する (§8(4))。

    **新しいエッジは作らない** — 関係の抽出は ③concept/④relate の仕事で、
    ここで得るのは「既存エッジに層 A のタグを刻むための候補」だけ。
    実在しないノードを指す候補は捨てる。
    """
    classes: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    for item in _as_list(raw, "concepts", "onto_classes"):
        if not isinstance(item, dict):
            continue
        label = concepts.get(normalize_label(item.get("label")))
        if label is None:
            report.note("ontology: 実在しない概念名を破棄")
            continue
        onto = _ONTO_LOOKUP.get(str(item.get("onto_class") or "").strip().lower())
        if onto is None:
            report.note(f"ontology: 未知の onto_class '{item.get('onto_class')}' を破棄")
            continue
        if label in seen_labels:
            continue
        seen_labels.add(label)
        classes.append({"label": label, "onto_class": onto})

    relations: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str, str]] = set()
    for item in _as_list(raw, "relations", "ontology_relations"):
        if not isinstance(item, dict):
            continue
        src = concepts.get(normalize_label(item.get("from")))
        dst = concepts.get(normalize_label(item.get("to")))
        relation = str(item.get("relation") or "").strip().replace("-", "_").lower()
        if src is None or dst is None:
            report.note("ontology: 実在しないノードを指す関係候補を破棄")
            continue
        if relation not in ONTOLOGY_RELATIONS:
            report.note(f"ontology: 未知の relation '{item.get('relation')}' を破棄")
            continue
        if src == dst:
            report.note("ontology: 自己参照の関係候補を破棄")
            continue
        key = (src, dst, relation)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        relations.append({"from": src, "to": dst, "relation": relation})
    return {"concepts": classes, "relations": relations}


def run_claims(
    run: RunFn,
    zones: Sequence[dict[str, Any]],
    kg: dict[str, Any],
    *,
    report: AnalysisReport,
    timestamp: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """主張抽出 + L5 最小 (§6 の task: claims)。

    対象は zone_label ∈ {Result, Conclusion, Hypothesis, Observation} の文だけ
    (§6)。手順や背景の文から主張を作らせない。onto_class と is_a/part_of は
    **同じ call で**受け取る (LLM 呼び出しを増やさないため / §8(4))。
    """
    ts = timestamp or dt.datetime.now().isoformat(timespec="seconds")
    concepts = _concept_index(kg)
    targets = [z for z in zones if z.get("zone_label") in CLAIM_ZONES]
    empty: dict[str, list[dict[str, Any]]] = {"concepts": [], "relations": []}
    if not targets:
        report.notes.append(
            "主張抽出の対象になる文 (Result/Conclusion/Hypothesis/Observation) が無い")
        return [], empty

    claims: list[dict[str, Any]] = []
    ontology: dict[str, list[dict[str, Any]]] = {"concepts": [], "relations": []}
    seen_ids: set[str] = set()
    concept_labels = list(concepts.values())
    max_calls = claims_max_calls()

    for call_no, batch in enumerate(_batched(targets, claims_batch())):
        if call_no >= max_calls:
            report.notes.append(
                f"claims の call 上限 {max_calls} に達したため残りの文は対象外")
            break
        payload = _payload(
            "claims",
            sentences=[{"sentence_id": z["sentence_id"], "text": z["text"]}
                       for z in batch],
            concepts=concept_labels)
        try:
            raw = extract_json(run(payload))
            report.llm_calls += 1
        except Exception as exc:
            report.errors.append(f"claims: {type(exc).__name__}")
            logger.warning("claims batch failed: %s", type(exc).__name__)
            continue
        for claim in repair_claims(raw, batch, concepts, report, timestamp=ts):
            if claim["nanopub_id"] in seen_ids:
                report.note("claims: 同一内容の主張を統合")
                continue
            seen_ids.add(claim["nanopub_id"])
            claims.append(claim)
        found = repair_ontology(raw, concepts, report)
        for key in ("concepts", "relations"):
            known = {json.dumps(v, sort_keys=True) for v in ontology[key]}
            ontology[key].extend(v for v in found[key]
                                 if json.dumps(v, sort_keys=True) not in known)

    limit = claims_max()
    if len(claims) > limit:
        report.notes.append(f"主張が {len(claims)} 件あるため上限 {limit} 件へ絞った")
        claims = claims[:limit]
    for i, claim in enumerate(claims, start=1):      # claim_id はサーバ側で通し採番
        claim["assertion"]["claim_id"] = f"cl-{i:03d}"
    logger.info("claims done targets=%d claims=%d relations=%d",
                len(targets), len(claims), len(ontology["relations"]))
    return claims, ontology


# ------------------------------------------------------------------ 入口


def analyze(
    run: RunFn,
    *,
    session: str,
    kg: dict[str, Any],
    docs: Sequence[Any] | None = None,
    timestamp: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], AnalysisReport]:
    """①文分割 → ②zone → ③claims/L5 をまとめて回し、サイドカーを組み立てる。

    戻り値の doc は §3.2 の形。arguments / refutes は M6 が埋めるので空配列。
    `ontology` は §3.2 に無い**追加キー**で、L5 の LLM 出力 (onto_class と
    is_a/part_of 候補) をそのまま残す — offline 再利用のときに層 A の情報が
    失われないようにするため、および M5 の OntologyChecker の入力にするため。

    progress は段の開始を知らせるフック ("zone" / "claims")。UI の進捗
    チェックリストを実際の進みに合わせるためだけに使う。
    """
    def announce(key: str) -> None:
        if progress is not None:
            progress(key)

    report = AnalysisReport()
    doc = layers_store.new_document(session)
    sentences = collect_sentences(docs, kg, report=report)
    if not sentences:
        announce("zone")
        announce("claims")
        report.notes.append("ゾーニングできる文が無い (資料本文も根拠スパンも空)")
        doc["stats"] = layers_store.compute_stats(doc, sentences=0, llm_calls=0)
        return doc, report

    announce("zone")
    doc["zones"] = run_zone(run, sentences, report=report)
    announce("claims")
    claims, ontology = run_claims(run, doc["zones"], kg,
                                  report=report, timestamp=timestamp)
    doc["claims"] = claims
    doc["ontology"] = ontology
    doc["stats"] = layers_store.compute_stats(
        doc, sentences=len(sentences), llm_calls=report.llm_calls)
    return doc, report
