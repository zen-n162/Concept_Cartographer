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
  CC_ZONE_MAX_SENTENCES  ゾーニングする文の総数上限     (既定 400)
  CC_CLAIMS_MAX          主張の件数上限                 (既定 40)
  CC_CLAIMS_BATCH        claims 1 call あたりの文数     (既定 60)
  CC_CLAIMS_MAX_CALLS    claims の call 数上限          (既定 2)
  CC_CGW_BATCH           cgw 1 call あたりの主張数      (既定 20)
  CC_CGW_MAX_CALLS       cgw の call 数上限             (既定 2)
  CC_REFUTES_MAX_PAIRS   矛盾を問う候補ペアの上限       (既定 30)
  CC_QA_MAX_CALLS        QA 1 問あたりの call 数上限     (既定 6・R2b §2)

**既定値は「最悪でも合計 30 call/run 以下」から逆算してある** (裁定 G)。
設計書 §6 は CC_ZONE_MAX_SENTENCES=500 と書いていたが、それだと zone だけで
10 call になり、他段と足して上限を超えうる。M7 で実測に合わせて 400 へ縮めた:

  zone      400 文 / 50 = 8 call
  claims    CC_CLAIMS_MAX_CALLS                    2 call
  validate  CC_VALIDATE_MAX_CALLS (verifiers.py)  16 call
  cgw       CC_CGW_MAX_CALLS                       2 call
  refutes   1 回だけ                                1 call
                                            合計  29 call (最悪値)

実測は summary["layers"]["stats"]["llm_calls"] に出る。
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

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
    """ゾーニングする文の総数上限 (裁定 G: 400 = zone を 8 call に収める)。"""
    return _knob("CC_ZONE_MAX_SENTENCES", 400)


def claims_max() -> int:
    return _knob("CC_CLAIMS_MAX", 40)


def claims_batch() -> int:
    return _knob("CC_CLAIMS_BATCH", 60)


def claims_max_calls() -> int:
    """claims の call 数上限 (裁定 G: 3 -> 2)。"""
    return _knob("CC_CLAIMS_MAX_CALLS", 2)


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


# ------------------------------------------------------------------ cgw (§6)


def cgw_batch() -> int:
    return _knob("CC_CGW_BATCH", 20)


def cgw_max_calls() -> int:
    return _knob("CC_CGW_MAX_CALLS", 2)


def refutes_max_pairs() -> int:
    return _knob("CC_REFUTES_MAX_PAIRS", 30)


# epistemic_strength の重み (§6)。**決定的コード**で算出する — 論証の強さを
# LLM に自己申告させると、根拠が薄い主張ほど自信ありげに返ってくるため。
GROUNDS_TARGET = 3
STRENGTH_LEVELS: tuple[tuple[float, str], ...] = (
    (0.75, "strong"), (0.5, "moderate"), (0.3, "weak"),
)
DEFAULT_LEVEL = "speculative"
MAX_WARRANT_CHARS = 200
NEIGHBOUR_WINDOW = 1


def epistemic_strength(grounds: Sequence[Mapping[str, Any]],
                       warrant: str) -> dict[str, Any]:
    """論証の強さ (§6)。0.4·根拠の数 + 0.4·根拠の確信 + 0.2·warrant の有無。

    「根拠が 3 本あれば数としては十分」と置いて頭打ちにする。数だけ多くても
    確信度が低ければ moderate 止まりになるよう、2 つを同じ重みにしてある。
    """
    count = min(1.0, len(grounds) / GROUNDS_TARGET) if grounds else 0.0
    mean = (sum(_as_confidence(g.get("confidence")) for g in grounds) / len(grounds)
            if grounds else 0.0)
    score = round(0.4 * count + 0.4 * mean + 0.2 * (1.0 if warrant.strip() else 0.0), 4)
    level = next((name for threshold, name in STRENGTH_LEVELS if score >= threshold),
                 DEFAULT_LEVEL)
    return {"score": score, "level": level}


def validated_claims(claims: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """⑤validate を通った主張だけ (§6 の cgw は validated のみ対象)。

    検証していない主張に論証を組み立てさせると、根拠の薄い主張ほど立派な
    warrant が付いて見え、地図の読み手を誤らせる。
    """
    return [c for c in claims or ()
            if isinstance(c, Mapping)
            and (c.get("validation") or {}).get("status") == "validated"]


def _neighbour_sentences(
    zones: Sequence[Mapping[str, Any]],
    claims: Sequence[Mapping[str, Any]],
    *, window: int = NEIGHBOUR_WINDOW,
) -> list[dict[str, str]]:
    """主張の根拠文とその前後を集める (§6 の cgw 入力「近傍文」)。

    根拠文そのものだけだと「その文が主張と同じことを言っている」という自明な
    論証しか出てこない。前後 1 文を足して、裏付けを探せる幅を持たせる。
    """
    order = [z for z in zones or () if isinstance(z, Mapping) and z.get("sentence_id")]
    position = {str(z["sentence_id"]): i for i, z in enumerate(order)}
    wanted: set[int] = set()
    for claim in claims or ():
        for sid in (claim.get("provenance") or {}).get("source_span") or ():
            idx = position.get(str(sid))
            if idx is None:
                continue
            for j in range(max(0, idx - window), min(len(order), idx + window + 1)):
                wanted.add(j)
    return [{"sentence_id": str(order[i]["sentence_id"]),
             "text": str(order[i].get("text") or "")} for i in sorted(wanted)]


def repair_arguments(
    raw: Any,
    claims: Sequence[Mapping[str, Any]],
    sentences: Sequence[Mapping[str, str]],
    report: AnalysisReport,
) -> list[dict[str, Any]]:
    """cgw task の出力を §3.2 の arguments レコードへ修復する。

    - claim_id は入力にあるものだけ。1 主張 1 論証 (重複は捨てる)
    - grounds の span_ref は入力の文だけ。text は**入力から引き直す**
      (原文の同一性を LLM に委ねない — repair_zone_labels と同じ)
    - epistemic_strength は LLM の申告を使わず、こちらで計算し直す
    """
    known_sentences = {s["sentence_id"]: s["text"] for s in sentences}
    by_claim_id = {str((c.get("assertion") or {}).get("claim_id") or ""): c
                   for c in claims}
    by_claim_id.pop("", None)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in _as_list(raw, "arguments", "results"):
        if not isinstance(item, dict):
            report.note("cgw: 未知の要素を破棄")
            continue
        claim_id = str(item.get("claim_id") or "")
        claim = by_claim_id.get(claim_id)
        if claim is None:
            report.note("cgw: 入力に無い claim_id を破棄")
            continue
        if claim_id in seen:
            report.note("cgw: 同じ主張への重複した論証を破棄")
            continue
        grounds: list[dict[str, Any]] = []
        for ground in _as_list(item.get("grounds"), "grounds"):
            if not isinstance(ground, dict):
                continue
            span_ref = str(ground.get("span_ref") or "")
            if span_ref not in known_sentences:
                report.note("cgw: 入力に無い span_ref を破棄")
                continue
            if any(g["span_ref"] == span_ref for g in grounds):
                continue
            grounds.append({"span_ref": span_ref, "text": known_sentences[span_ref],
                            "confidence": _as_confidence(ground.get("confidence"))})
        warrant = str(item.get("warrant") or "").strip()[:MAX_WARRANT_CHARS]
        seen.add(claim_id)
        out.append({
            "argument_id": f"arg-{len(out) + 1:03d}",
            "claim_ref": str(claim.get("nanopub_id") or ""),
            "grounds": grounds,
            "warrant": warrant,
            "epistemic_strength": epistemic_strength(grounds, warrant),
        })
    return out


def run_cgw(
    run: RunFn,
    claims: Sequence[Mapping[str, Any]],
    zones: Sequence[Mapping[str, Any]],
    *,
    report: AnalysisReport,
) -> list[dict[str, Any]]:
    """論証の抽出 (§6 の task: cgw)。**validated な主張だけ**を対象にする。"""
    targets = validated_claims(claims)
    if not targets:
        report.notes.append("論証の対象になる validated な主張が無い")
        return []
    sentences = _neighbour_sentences(zones, targets)
    if not sentences:
        report.notes.append("論証の材料になる近傍文が無い (zones が空)")
        return []

    arguments: list[dict[str, Any]] = []
    max_calls = cgw_max_calls()
    for call_no, batch in enumerate(_batched(targets, cgw_batch())):
        if call_no >= max_calls:
            report.notes.append(
                f"cgw の call 上限 {max_calls} に達したため残りの主張は対象外")
            break
        payload = _payload(
            "cgw",
            claims=[{"claim_id": (c.get("assertion") or {}).get("claim_id", ""),
                     "claim_text": (c.get("assertion") or {}).get("claim_text", "")}
                    for c in batch],
            sentences=sentences)
        try:
            raw = extract_json(run(payload))
            report.llm_calls += 1
        except Exception as exc:
            report.errors.append(f"cgw: {type(exc).__name__}")
            logger.warning("cgw batch failed: %s", type(exc).__name__)
            continue
        for argument in repair_arguments(raw, batch, sentences, report):
            argument["argument_id"] = f"arg-{len(arguments) + 1:03d}"
            arguments.append(argument)
    logger.info("cgw done validated=%d arguments=%d", len(targets), len(arguments))
    return arguments


# ------------------------------------------------------------------ refutes (§6)

# 候補ペアの決定的な絞り込みに使う手がかり (§6: ここは LLM を使わない)。
# 「主張の極性」を粗く読むためだけのもので、判定そのものは LLM が行う。
NEGATION_CUES: tuple[str, ...] = (
    "ない", "なかった", "ません", "でない", "否定", "不可", "困難",
    "見られなかった", "認められなかった", "有意差はな", "変わらな",
    " not ", "n't", " no ", "fail", "without", "lack", "absence", "unable",
)
# 対になる語。片方ずつを別の主張が含んでいれば対立の候補とみなす
OPPOSITE_CUE_PAIRS: tuple[tuple[str, str], ...] = (
    ("増加", "減少"), ("上昇", "低下"), ("向上", "低下"), ("改善", "悪化"),
    ("促進", "抑制"), ("有効", "無効"), ("正の相関", "負の相関"),
    ("多い", "少ない"), ("高い", "低い"), ("速い", "遅い"),
    ("increase", "decrease"), ("improve", "worsen"), ("higher", "lower"),
    ("effective", "ineffective"), ("positive", "negative"),
)

VERDICTS: tuple[str, ...] = ("refutes", "disagrees", "none")


def claim_polarity(text: str) -> str:
    """主張の粗い極性 (§3.1 の 3 値と同じ語彙)。否定の手がかり語だけで決める。"""
    lowered = f" {str(text or '').lower()} "
    return "negative" if any(cue.lower() in lowered
                             for cue in NEGATION_CUES) else "positive"


def _opposite_cue_split(a: str, b: str) -> bool:
    """対になる語が 2 つの主張に分かれて出ているか。"""
    low_a, low_b = a.lower(), b.lower()
    for left, right in OPPOSITE_CUE_PAIRS:
        if (left in low_a and right in low_b) or (right in low_a and left in low_b):
            return True
    return False


def refutes_candidates(
    claims: Sequence[Mapping[str, Any]],
    *,
    limit: int | None = None,
    report: AnalysisReport | None = None,
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """矛盾を問う候補ペアを**決定的に**絞り込む (§6)。

    全組み合わせを LLM に投げると n² で費用が爆発するうえ、無関係な対にも
    もっともらしい矛盾を作られる。条件は 2 つ:

      1. related_concepts を 1 つ以上共有する (同じものについて語っている)
      2. 極性が反転している、または対になる語 (増加↔減少 等) が分かれている

    rejected な主張は対象外 — 登録しないものの矛盾を数えても意味がない。
    順序は claims の順で決まるので、同じ入力なら同じペアが出る。
    """
    usable = [c for c in claims or ()
              if isinstance(c, Mapping)
              and (c.get("validation") or {}).get("status") != "rejected"]
    cap = limit if limit is not None else refutes_max_pairs()
    texts = [str((c.get("assertion") or {}).get("claim_text") or "") for c in usable]
    concepts = [{normalize_label(x)
                 for x in (c.get("assertion") or {}).get("related_concepts") or ()}
                for c in usable]
    polarity = [claim_polarity(t) for t in texts]

    pairs: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    truncated = 0
    for i in range(len(usable)):
        for j in range(i + 1, len(usable)):
            if not (concepts[i] & concepts[j]):
                continue
            if not (polarity[i] != polarity[j]
                    or _opposite_cue_split(texts[i], texts[j])):
                continue
            if len(pairs) >= cap:
                truncated += 1
                continue
            pairs.append((usable[i], usable[j]))
    if truncated and report is not None:
        report.notes.append(
            f"矛盾の候補ペアが上限 {cap} を超えたため {truncated} 組を対象外にした "
            "(CC_REFUTES_MAX_PAIRS)")
    return pairs


def repair_refutes(
    raw: Any,
    pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    report: AnalysisReport,
) -> list[dict[str, Any]]:
    """refutes task の出力を §3.2 の refutes レコードへ修復する。

    LLM には「pairs と同じ順序・同じ個数で返す」と指示してあるが、実際には
    ずれることがある。**個数が合わない返答は丸ごと捨てる** — 順序に依存した
    突合で 1 つずれると、無関係な対に矛盾の判定が付いてしまうため。
    """
    results = _as_list(raw, "results", "refutes")
    if len(results) != len(pairs):
        report.note("refutes: pairs と個数が合わない応答を破棄")
        return []
    out: list[dict[str, Any]] = []
    for (a, b), item in zip(pairs, results):
        if not isinstance(item, dict):
            report.note("refutes: 未知の要素を破棄")
            continue
        verdict = str(item.get("verdict") or "").strip().lower()
        if verdict not in VERDICTS:
            report.note(f"refutes: 未知の verdict '{item.get('verdict')}' を none へ")
            verdict = "none"
        out.append({"pair": [str(a.get("nanopub_id") or ""),
                             str(b.get("nanopub_id") or "")],
                    "verdict": verdict,
                    "confidence": _as_confidence(item.get("confidence")),
                    "rationale": str(item.get("rationale") or "").strip()[:120]})
    return out


def run_refutes(
    run: RunFn,
    claims: Sequence[Mapping[str, Any]],
    *,
    report: AnalysisReport,
) -> list[dict[str, Any]]:
    """内部矛盾の検出 (§6 の task: refutes)。候補ペアの絞り込みは決定的。"""
    pairs = refutes_candidates(claims, report=report)
    if not pairs:
        report.notes.append("矛盾の候補ペアが無い (概念の共有と極性の反転がそろわない)")
        return []
    payload = _payload("refutes", pairs=[
        {"a": str((a.get("assertion") or {}).get("claim_text") or ""),
         "b": str((b.get("assertion") or {}).get("claim_text") or "")}
        for a, b in pairs])
    try:
        raw = extract_json(run(payload))
        report.llm_calls += 1
    except Exception as exc:
        report.errors.append(f"refutes: {type(exc).__name__}")
        logger.warning("refutes call failed: %s", type(exc).__name__)
        return []
    records = repair_refutes(raw, pairs, report)
    logger.info("refutes done pairs=%d judged=%d confirmed=%d", len(pairs),
                len(records), sum(1 for r in records if r["verdict"] == "refutes"))
    return records


def analyze_rhetoric(
    run: RunFn,
    *,
    claims: Sequence[Mapping[str, Any]],
    zones: Sequence[Mapping[str, Any]] = (),
    report: AnalysisReport | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], AnalysisReport]:
    """⑥rhetoric — 論証 (cgw) と内部矛盾 (refutes) をまとめて回す (§6)。

    LLM 呼び出しは最大 3 (cgw 2 + refutes 1)。どちらかが失敗しても他方は
    続ける — 論証が取れなくても矛盾の検出には意味があるため。
    """
    report = report or AnalysisReport()
    arguments = run_cgw(run, claims, zones, report=report)
    refutes = run_refutes(run, claims, report=report)
    return arguments, refutes, report


# ------------------------------------------------------------- QA (R2b §2)
#
# 裁定 M: QA のために新しいエージェントは作らず、cc-analysis に task を 2 つ
# 足す。ここでも仕事の本体は「形の修復」で、方針はゾーン/主張と同じ:
# **LLM が返した参照を信用せず、こちらが渡した材料と突合する**。
# 出典 (cited) に実在しない ref が混ざると、根拠を辿れない答えが「出典つき」
# として表示されてしまい、検証できない主張が一番たちの悪い形で残る。

MAX_ANSWER_CHARS = 1200
MAX_SUMMARY_CHARS = 400
MAX_TITLE_CHARS = 40


def qa_max_calls() -> int:
    """QA 1 問あたりの LLM 呼び出し上限 (設計 §2: 既定 6)。"""
    return _knob("CC_QA_MAX_CALLS", 6)


def _as_text(raw: Any, *keys: str) -> str:
    """{"answer": "..."} でも "..." でも本文を取り出す。"""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        for key in keys:
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def repair_qa(raw: Any, known_refs: Iterable[str],
              report: AnalysisReport) -> dict[str, Any]:
    """qa task の出力を {answer, cited, insufficient} へ修復する。

    cited は**こちらが渡した ref だけ**に絞る。LLM が付けた出典が実在するか
    どうかは呼び出し側では判定できないので、ここで落としきる。
    """
    refs = list(dict.fromkeys(str(r) for r in known_refs))
    allowed = set(refs)
    answer = _as_text(raw, "answer", "text", "response")[:MAX_ANSWER_CHARS]
    cited: list[str] = []
    source = raw.get("cited") if isinstance(raw, dict) else None
    if isinstance(source, str):
        source = [source]
    for item in source or ():
        ref = str(item.get("ref") if isinstance(item, dict) else item).strip()
        if ref not in allowed:
            report.note("qa: context に無い出典を破棄")
            continue
        if ref not in cited:
            cited.append(ref)
    if not answer:
        report.note("qa: answer が空の応答")
    insufficient = bool(isinstance(raw, dict) and raw.get("insufficient"))
    return {"answer": answer, "cited": cited, "insufficient": insufficient}


def run_qa(run: RunFn, question: str, context: Mapping[str, Any], *,
           report: AnalysisReport) -> dict[str, Any]:
    """集めた材料だけで質問に答えさせる (§2 の task: qa)。1 call。

    失敗しても例外は投げない — QA は「答えられませんでした」で終われるべきで、
    ここで落とすと呼び出し側が組み立てた材料まで捨てることになる。
    """
    refs = [str(item.get("ref"))
            for key in ("concepts", "relations", "summaries")
            for item in (context.get(key) or ())
            if isinstance(item, Mapping) and item.get("ref")]
    payload = _payload("qa", question=str(question), context=dict(context))
    try:
        raw = extract_json(run(payload))
        report.llm_calls += 1
    except Exception as exc:
        report.errors.append(f"qa: {type(exc).__name__}")
        logger.warning("qa call failed: %s", type(exc).__name__)
        return {"answer": "", "cited": [], "insufficient": True}
    result = repair_qa(raw, refs, report)
    logger.info("qa done refs=%d cited=%d insufficient=%s",
                len(refs), len(result["cited"]), result["insufficient"])
    return result


def repair_community_summary(raw: Any, members: Sequence[str],
                             report: AnalysisReport) -> dict[str, str]:
    """community_summary task の出力を {title, summary} へ修復する。

    title が取れないときは**メンバーの先頭から作る** (空タイトルの島を
    画面に出さないため)。要約が空なら呼び出し側がキャッシュしない。
    """
    title = _as_text(raw, "title", "name")[:MAX_TITLE_CHARS]
    summary = _as_text(raw, "summary", "text", "description")[:MAX_SUMMARY_CHARS]
    if not title:
        report.note("community_summary: title が空なのでメンバー名で代用")
        title = "・".join(str(m) for m in list(members)[:2])[:MAX_TITLE_CHARS]
    if not summary:
        report.note("community_summary: summary が空の応答")
    return {"title": title, "summary": summary}


def run_community_summary(run: RunFn, members: Sequence[str],
                          relations: Sequence[Mapping[str, Any]] = (), *,
                          report: AnalysisReport) -> dict[str, str]:
    """概念クラスタを 1 段落に要約する (§2 の task: community_summary)。1 call。

    裁定 L: インデックス時には呼ばない。問いに関係する上位コミュニティだけを
    クエリ時に要約し、結果はコミュニティ指紋つきでキャッシュする。
    """
    payload = _payload("community_summary", members=[str(m) for m in members],
                       relations=[dict(r) for r in relations])
    try:
        raw = extract_json(run(payload))
        report.llm_calls += 1
    except Exception as exc:
        report.errors.append(f"community_summary: {type(exc).__name__}")
        logger.warning("community summary failed: %s", type(exc).__name__)
        return {"title": "", "summary": ""}
    return repair_community_summary(raw, members, report)


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
