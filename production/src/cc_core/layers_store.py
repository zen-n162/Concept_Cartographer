"""layers_session サイドカーの I/O (R2a 設計書 §3.2)。

`graphs/layers_session_{s}.json` は **不変のサイドカー**で、生成時に 1 回だけ
書く。kg (可変・編集される) とは寿命が違うので別ファイルに分けてある。

**参照方向の不変則: kg → layers の一方向のみ** (§3.2)。layers 側は edge_id を
持たない。エッジは編集で消えたり向きが変わったりするので、そこを指してしまうと
「不変のはずのサイドカーが壊れた参照を抱える」ことになるため。kg から layers へは
`claim_refs` (nanopub_id) で指す。

nanopub_id は**サーバ側で採番**する (§6)。LLM に ID を作らせると同じ主張に
違う ID が付き、run をまたいだ突合ができなくなる。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from cc_core.logging_util import get_logger
from cc_core.sentences import SPLITTER_VERSION

logger = get_logger("cc_core.layers_store")

GRAPHS_DIR = "graphs"
LAYERS_PREFIX = "layers_session_"
KG_PREFIX = "kg_session_"

# サイドカーのスキーマ世代 (§3.2 の "version": 1)
LAYERS_VERSION = 1

# §3.2 のトップレベルキー。**空でも必ず全部書く** — 読む側が
# `doc["claims"]` を素で書けるようにするため (形が run ごとに変わらない)
DOCUMENT_KEYS: tuple[str, ...] = ("zones", "claims", "arguments", "refutes")

# 裁定 V: document_id -> 人が読めるファイル名の対応表。
# **additive な追加キー**で、DOCUMENT_KEYS には入れない (あちらは「必ず list」の
# 不変則を持つ配列群。documents は dict なので混ぜると読む側の前提が壊れる)。
DOCUMENTS_KEY = "documents"

# セッション ID はファイル名の一部になる。`..` や `/` を弾く
# (cc_web.sessions.SESSION_RE と同じ方針。cc_core は cc_web を import しない)
SESSION_RE = re.compile(r"^[0-9A-Za-z_\-]{1,64}$")


class InvalidSession(ValueError):
    """ファイル名に使えないセッション ID。"""


def _check(session: str) -> str:
    if not SESSION_RE.match(session or ""):
        raise InvalidSession(f"不正なセッション ID: {session!r}")
    return session


def path(session: str, *, graphs_dir: str | Path = GRAPHS_DIR) -> Path:
    """サイドカーのパス (存在は問わない)。"""
    return Path(graphs_dir) / f"{LAYERS_PREFIX}{_check(session)}.json"


def exists(session: str, *, graphs_dir: str | Path = GRAPHS_DIR) -> bool:
    try:
        return path(session, graphs_dir=graphs_dir).exists()
    except InvalidSession:
        return False


def load(session: str, *, graphs_dir: str | Path = GRAPHS_DIR) -> dict[str, Any]:
    """サイドカーを読む。壊れた JSON は例外にせず**空の骨格**を返す。

    層の情報が読めなくても地図の生成・再描画は続けられるべきなので、
    ここで落とさない (読めなかった事実はログに残す)。
    """
    file = path(session, graphs_dir=graphs_dir)
    try:
        doc = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("layers sidecar unreadable session=%s err=%s",
                       session, type(exc).__name__)
        return new_document(session)
    if not isinstance(doc, dict):
        logger.warning("layers sidecar is not an object session=%s", session)
        return new_document(session)
    for key in DOCUMENT_KEYS:                       # 旧世代・部分書き込みの穴を埋める
        if not isinstance(doc.get(key), list):
            doc[key] = []
    # 裁定 V: 対応表は**あるときだけ**通す。無いセッションに空 dict を生やすと
    # 「対応表があって解決できなかった」と「そもそも対応表が無い」の区別が
    # 消える。過去セッションは従来表示のままにしたいので遡及生成はしない。
    if DOCUMENTS_KEY in doc and not isinstance(doc[DOCUMENTS_KEY], dict):
        doc.pop(DOCUMENTS_KEY)
    doc.setdefault("version", LAYERS_VERSION)
    doc.setdefault("session", session)
    doc.setdefault("splitter", SPLITTER_VERSION)
    doc.setdefault("stats", {})
    return doc


def save(session: str, doc: dict[str, Any], *,
         graphs_dir: str | Path = GRAPHS_DIR) -> Path:
    """サイドカーを書く (§3.2 のキー順で整形)。"""
    file = path(session, graphs_dir=graphs_dir)
    file.parent.mkdir(parents=True, exist_ok=True)
    ordered = {"version": doc.get("version", LAYERS_VERSION),
               "session": session,
               "splitter": doc.get("splitter", SPLITTER_VERSION)}
    for key in DOCUMENT_KEYS:
        ordered[key] = list(doc.get(key) or [])
    if isinstance(doc.get(DOCUMENTS_KEY), dict) and doc[DOCUMENTS_KEY]:
        ordered[DOCUMENTS_KEY] = dict(doc[DOCUMENTS_KEY])   # 裁定 V (空なら書かない)
    ordered["stats"] = dict(doc.get("stats") or {})
    for key, value in doc.items():                  # 将来の追加キーも落とさない
        ordered.setdefault(key, value)
    file.write_text(json.dumps(ordered, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    logger.info("layers sidecar saved session=%s zones=%d claims=%d",
                session, len(ordered["zones"]), len(ordered["claims"]))
    return file


def new_document(session: str, *, splitter: str = SPLITTER_VERSION) -> dict[str, Any]:
    """空の骨格 (§3.2)。キーの形を 1 か所に集めておく。"""
    doc: dict[str, Any] = {"version": LAYERS_VERSION, "session": session,
                           "splitter": splitter}
    for key in DOCUMENT_KEYS:
        doc[key] = []
    doc["stats"] = {}
    return doc


# ------------------------------------------------- 裁定 V: document_id の解決


def collect_document_ids(doc: dict[str, Any] | None,
                         kg: dict[str, Any] | None = None) -> list[str]:
    """出典として画面に出うる document_id を全部集める (出現順・重複なし)。

    zones / claims / kg の evidence_span の 3 か所。ここが「解決したい id」の
    母集団で、対応表はこの集合に対してだけ作れば足りる。
    """
    seen: dict[str, None] = {}

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text:
            seen.setdefault(text, None)

    for zone in (doc or {}).get("zones", []) or []:
        if isinstance(zone, dict):
            add(zone.get("document_id"))
    for claim in (doc or {}).get("claims", []) or []:
        if isinstance(claim, dict):
            add((claim.get("pub_info") or {}).get("document_id"))
            for span in (claim.get("provenance") or {}).get("source_span", []) or []:
                if isinstance(span, dict):
                    add(span.get("document_id"))
    for bucket in ("nodes", "edges"):
        for element in (kg or {}).get(bucket, []) or []:
            if not isinstance(element, dict):
                continue
            for span in element.get("evidence_span") or []:
                if isinstance(span, dict):
                    add(span.get("document_id"))
    return list(seen)


def build_documents(document_ids: Iterable[str],
                    names: Iterable[str]) -> dict[str, str]:
    """document_id -> ファイル名の対応表を作る (裁定 V)。

    `names` は**取込 (ingest) が実際に見たファイル名**。突合は決定的な 4 規則
    だけで行い、当たらなかった id は表に入れない:

      1. 完全一致       id がファイル名そのもの (取込資料由来の id はこれ)
      2. basename 一致  id がパス風のとき末尾を見る ("drive/x/報告.pdf")
      3. 拡張子なし一致 id がファイル名の stem ("報告" -> "報告.pdf")
      4. 部分一致       ファイル名が id に埋まっている。**候補が 1 件のときだけ**

    Work IQ の不透明な M365 ID (実測では "409" のような値) はどの規則にも
    当たらないので表に載らない。**推測で埋めない**のが肝で、出典を間違った
    資料に結びつけるのは生の id を出すより悪い (研究判断の根拠になるため)。
    当たらなかった id は従来どおり生のまま表示される。
    """
    known = [str(n).strip() for n in names or () if str(n or "").strip()]
    if not known:
        return {}
    by_exact = {n: n for n in known}
    by_lower = {n.lower(): n for n in known}
    by_stem = {Path(n).stem.lower(): n for n in known}

    table: dict[str, str] = {}
    for raw in document_ids or ():
        doc_id = str(raw or "").strip()
        if not doc_id:
            continue
        base = Path(doc_id.replace("\\", "/")).name
        hit = (by_exact.get(doc_id) or by_lower.get(doc_id.lower())
               or by_exact.get(base) or by_lower.get(base.lower())
               or by_stem.get(doc_id.lower()) or by_stem.get(base.lower()))
        if hit is None:
            embedded = [n for n in known if len(n) >= 4 and n.lower() in doc_id.lower()]
            hit = embedded[0] if len(embedded) == 1 else None
        if hit is not None:
            table[doc_id] = hit
    return table


def documents_of(doc: dict[str, Any] | None) -> dict[str, str]:
    """サイドカーから対応表を取り出す (無ければ空 dict)。"""
    table = (doc or {}).get(DOCUMENTS_KEY)
    if not isinstance(table, dict):
        return {}
    return {str(k): str(v) for k, v in table.items() if k and v}


def resolve_document(document_id: Any, documents: dict[str, str] | None) -> str:
    """表示用のファイル名。対応表に無ければ**元の id をそのまま返す**。

    「過去セッションは対応表が無ければ従来表示」(裁定 V) をこの 1 行で守る。
    """
    text = str(document_id or "")
    return (documents or {}).get(text, text)


def session_of_kg_file(kg_file: str | Path | None) -> str | None:
    """`graphs/kg_session_{s}.json` から元セッション ID を取り出す。

    offline 実行は保存済み KG を読み直すが、セッション ID は新しく振られる。
    層の再利用 (§9) は「元のセッションのサイドカー」を探す必要があるので、
    ファイル名から辿る。命名規約から外れたパスは None (= 再利用しない)。
    """
    if not kg_file:
        return None
    stem = Path(kg_file).stem
    if not stem.startswith(KG_PREFIX):
        return None
    session = stem[len(KG_PREFIX):]
    return session if SESSION_RE.match(session) else None


def nanopub_id(claim_text: str, source_span: Iterable[str]) -> str:
    """`np:` + sha256(claim_text + source_span)[:16] (§6)。

    **サーバ側採番**。同じ主張・同じ根拠文なら run をまたいで同じ ID になる
    ので、kg 側の claim_refs が再生成後も指し先を失わない。
    """
    payload = str(claim_text or "") + "".join(str(s) for s in source_span or ())
    return "np:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def compute_stats(doc: dict[str, Any], *, sentences: int = 0,
                  llm_calls: int = 0) -> dict[str, int]:
    """§3.2 の stats を数える。検証段 (M5) / 論証段 (M6) が走る前は 0。

    `rejected` / `arguments` / `refutes` は §3.2 の表に無い追加キー。
    CLI の `--layers-summary` と Web の結果カードが「主張 n 件 (検証済 m)・
    矛盾 k 件」を出すのに要る数で、サイドカーを読み直さずに済ませるため。
    """
    claims = [c for c in doc.get("claims", []) if isinstance(c, dict)]
    statuses = [(c.get("validation") or {}).get("status") for c in claims]
    refutes = [r for r in doc.get("refutes", []) if isinstance(r, dict)]
    return {"sentences": sentences, "zoned": len(doc.get("zones", [])),
            "claims": len(claims),
            "validated": sum(1 for s in statuses if s == "validated"),
            "rejected": sum(1 for s in statuses if s == "rejected"),
            "arguments": len([a for a in doc.get("arguments", [])
                              if isinstance(a, dict)]),
            "refutes": sum(1 for r in refutes if r.get("verdict") == "refutes"),
            "llm_calls": llm_calls}
