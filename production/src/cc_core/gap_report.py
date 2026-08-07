"""ギャップレポート — 型別の「次の一手」 (R2c 設計書 §2.1、裁定 R)。

ギャップ検出 (`cc_core.gaps`) は「ここに穴がある」までしか言わない。研究者が
次に知りたいのは**その穴をどう埋めるか**で、それには手元の資料を横断して
「もう答えが自分の中にあるか」を確かめる必要がある。ここがその工程。

## 設計の芯: finding (決定的) と suggestion (LLM) を分ける

各項目は 2 段構えになっている。

  finding     store を検索して得た**事実**。LLM を 1 回も呼ばずに出る。
              「この 2 概念に両方触れている資料が 3 件ある」のような、
              後から人が同じ検索をして確かめられる内容だけを書く。
  suggestion  LLM の 1 文提案。**任意**で、上限内でしか呼ばず、
              呼べない環境では丸ごと省略される (キー自体を出さない)。

この分離が要点で、レポートの価値は finding 側にある。suggestion が無くても
レポートは成立しなければならない (受け入れ基準 3)。逆に言えば LLM の作文を
finding と混ぜてはいけない — 「3 件あった」と「あるかもしれない」を同じ
段落に置くと、検証できない文が検証できる文の信用を借りてしまう。

## 情報源の優先順位 (裁定 R)

  ① 自セッション群 (cc_store)     常に使う。決定的で、外部アクセスも無い
  ② approved-literature KB       env CC_KB_AGENT があるときだけ
  ③ 公開 API (arXiv)             env CC_EXTERNAL_RECS=1 のときだけ

**既定は ① のみ**。②③ は明示的に設定されるまで*試みもしない* — 設定を
読む前にクライアントを組み立てたりホスト名を引いたりもしない。研究資料の
概念名が黙って社外へ出ることは無い、という保証をコードの形で持たせている
(tests は socket レベルで、既定実行中に接続が 1 本も張られないことを見る)。
③ を有効にした場合も、送るのは**概念ラベルだけ**で、全送信を
`logs/external_queries.jsonl` に残す。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from cc_core.causal import CAUSAL_CUES_EN, CAUSAL_CUES_JA, find_causal_cues
from cc_core.editing import normalize_label
from cc_core.gaps import KIND_CAUSAL, KIND_DISCOURSE, KIND_STRUCTURAL
from cc_core.layers_store import documents_of, resolve_document
from cc_core.logging_util import get_logger
from cc_core.mcp_client import extract_json

logger = get_logger("cc_core.gap_report")

EXPORT_DIR = "exports"
EXTERNAL_LOG = "logs/external_queries.jsonl"

# 裁定 R の 2 つのスイッチ。**既定は両方とも未設定**
KB_AGENT_ENV = "CC_KB_AGENT"
EXTERNAL_ENV = "CC_EXTERNAL_RECS"

# cc-analysis のエージェント名。cc_core は cc_orchestrator を import しない
# (層の向きを保つ) ので、名前だけをここに置く
AGENT = "cc-analysis"
SUGGEST_TASK = "gap_suggest"

# arXiv。**この 2 定数を使うのは _arxiv_search だけ**で、その関数は
# CC_EXTERNAL_RECS=1 のときしか呼ばれない
ARXIV_API = "https://export.arxiv.org/api/query"
USER_AGENT = "ConceptCartographer/1.0 (research concept-map tool; contact: local operator)"

# discourse ギャップが「無い」と言っている語り口 (gaps.py の判定と揃える)
METHOD_ZONES = ("Method", "Experiment")

MAX_SOURCES = 6            # 1 項目あたりの出典の上限 (読み切れる量に留める)
SEARCH_LIMIT = 20          # store.search_nodes の取得件数
KB_NOT_CONNECTED = "kb: 未接続"


# ============================================================ 材料の下ごしらえ


class _Corpus:
    """セッションをまたぐ読み出しのキャッシュ。

    1 レポートの中で同じ KG を何度も読むことになる (ギャップ 16 件 x 概念 2 個)
    ので、ここで 1 回に潰す。レポート 1 本ぶんの寿命しか無い使い捨て。
    """

    def __init__(self, store: Any) -> None:
        self.store = store
        self._kg: dict[str, dict[str, Any]] = {}
        self._layers: dict[str, dict[str, Any]] = {}
        self._sessions: list[str] | None = None
        self._mentions: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def sessions(self) -> list[str]:
        if self._sessions is None:
            try:
                self._sessions = list(self.store.list_sessions())
            except Exception as exc:
                logger.warning("gap_report: セッション一覧を取れません (%s)",
                               type(exc).__name__)
                self._sessions = []
        return self._sessions

    def kg(self, session: str) -> dict[str, Any]:
        if session not in self._kg:
            try:
                self._kg[session] = self.store.load_kg(session) or {}
            except Exception as exc:   # 壊れた 1 セッションでレポートを止めない
                logger.warning("gap_report: KG を読めません session=%s (%s)",
                               session, type(exc).__name__)
                self._kg[session] = {}
        return self._kg[session]

    def layers(self, session: str) -> dict[str, Any]:
        if session not in self._layers:
            try:
                self._layers[session] = self.store.load_layers(session) or {}
            except Exception as exc:
                logger.warning("gap_report: layers を読めません session=%s (%s)",
                               session, type(exc).__name__)
                self._layers[session] = {}
        return self._layers[session]

    def documents(self, session: str) -> dict[str, str]:
        """裁定 V の対応表 (無ければ空 = 生の id 表示)。"""
        return documents_of(self.layers(session))

    def search(self, query: str) -> list[dict[str, Any]]:
        if not query:
            return []
        try:
            return list(self.store.search_nodes(query, limit=SEARCH_LIMIT))
        except Exception as exc:
            logger.warning("gap_report: 検索に失敗 query=%s (%s)",
                           query, type(exc).__name__)
            return []


def _nodes(kg: dict[str, Any]) -> list[dict[str, Any]]:
    return [n for n in kg.get("nodes", []) or [] if isinstance(n, dict)]


def _edges(kg: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in kg.get("edges", []) or [] if isinstance(e, dict)]


def _label_map(kg: dict[str, Any]) -> dict[str, str]:
    return {str(n.get("id")): str(n.get("label") or "") for n in _nodes(kg)
            if n.get("id")}


def _representative(kg: dict[str, Any], community_id: str,
                    *, exclude: Sequence[str] = ()) -> str:
    """コミュニティの代表概念 = 次数が最大のノードのラベル。

    「代表」に importance を使わないのは、importance がレイアウト用の派生値で
    セッションによって入っていないことがあるため。次数なら kg だけで必ず出る。
    """
    members = [n for n in _nodes(kg)
               if str(n.get("community_id") or "") == str(community_id)
               and str(n.get("id")) not in set(exclude)]
    if not members:
        return ""
    degree: dict[str, int] = {}
    for edge in _edges(kg):
        for key in ("from", "to"):
            nid = str(edge.get(key) or "")
            degree[nid] = degree.get(nid, 0) + 1
    members.sort(key=lambda n: (-degree.get(str(n.get("id")), 0),
                                str(n.get("label") or "")))
    return str(members[0].get("label") or "")


def _spans_of(element: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in element.get("evidence_span") or [] if isinstance(s, dict)]


def _source(kind: str, session: str, document_id: str, names: dict[str, str],
            label: str = "", note: str = "") -> dict[str, Any]:
    """出典 1 件。document_id は裁定 V の対応表でファイル名へ寄せる。"""
    row = {"kind": kind, "session": session,
           "document_id": str(document_id or ""),
           "name": resolve_document(document_id, names)}
    if label:
        row["label"] = label
    if note:
        row["note"] = note
    return row


def _dedup(sources: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[tuple, dict[str, Any]] = {}
    for src in sources:
        key = (src.get("kind"), src.get("session"), src.get("document_id"),
               src.get("label"))
        seen.setdefault(key, src)
    return list(seen.values())[:MAX_SOURCES]


# ---------------------------------------------------------------- 横断検索


def _mentions(corpus: _Corpus, label: str) -> dict[str, list[dict[str, Any]]]:
    """ラベルに言及している資料を全セッションから拾う。

    戻り値は session -> [出典]。索引 (`search_nodes`) で当たりを付けてから、
    そのセッションの KG で根拠スパンの document_id を引く — 索引は
    「どこにあるか」までしか持たないため。同じラベルは 1 レポート中に何度も
    問われる (両側検索 x ギャップ数) ので結果を憶えておく。
    """
    if not label:
        return {}
    if label in corpus._mentions:
        return corpus._mentions[label]
    target = normalize_label(label)
    found: dict[str, list[dict[str, Any]]] = {}
    for hit in corpus.search(label):
        if hit.get("kind") != "node":
            continue
        if normalize_label(str(hit.get("label") or "")) != target:
            continue
        session = str(hit.get("session") or "")
        node_id = str(hit.get("node_id") or "")
        if not session:
            continue
        kg = corpus.kg(session)
        names = corpus.documents(session)
        docs: list[dict[str, Any]] = []
        for node in _nodes(kg):
            if str(node.get("id")) != node_id:
                continue
            for span in _spans_of(node):
                docs.append(_source("document", session,
                                    str(span.get("document_id") or ""),
                                    names, label=label))
        for edge in _edges(kg):
            if node_id not in (str(edge.get("from")), str(edge.get("to"))):
                continue
            for span in _spans_of(edge):
                docs.append(_source("document", session,
                                    str(span.get("document_id") or ""),
                                    names, label=label))
        if not docs:                       # 索引にあるのに根拠が無い = 概念だけ
            docs = [_source("session", session, "", names, label=label,
                            note="概念のみ (根拠スパンなし)")]
        found.setdefault(session, []).extend(docs)
    corpus._mentions[label] = found
    return found


def _both_sides(corpus: _Corpus, left: str, right: str) -> tuple[list, list, list]:
    """左右のラベル両方に言及しているセッションを求める。

    戻り値 (共通セッション, 左だけ, 右だけ)。「両方に触れた資料がある」は
    橋渡しの手がかりが**すでに手元にある**という意味なので、そこを最優先で
    出す (裁定 R の ①: まず自分のセッション群)。
    """
    a, b = _mentions(corpus, left), _mentions(corpus, right)
    both = sorted(set(a) & set(b))
    return both, sorted(set(a) - set(b)), sorted(set(b) - set(a))


# ============================================================ 型別の finding


def _structural(corpus: _Corpus, session: str, gap: dict[str, Any],
                kg: dict[str, Any]) -> dict[str, Any]:
    """弱接続の両側の代表概念で横断検索する (設計 §2.1)。

    「両側」の取り方は gap_id の形で決まる:
      bridge   … 2 コミュニティの代表どうし
      isolated / weak … その概念と、所属コミュニティの代表 (= 繋がるべき先)
    """
    labels = _label_map(kg)
    related = [str(x) for x in gap.get("related_node_ids") or []]
    gap_id = str(gap.get("gap_id") or "")

    left = right = ""
    if gap_id.startswith("gap-bridge-"):
        parts = gap_id[len("gap-bridge-"):].split("-")
        if len(parts) >= 2:
            left = _representative(kg, parts[0])
            right = _representative(kg, "-".join(parts[1:]))
    if not (left and right):
        left = labels.get(related[0], "") if related else ""
        community = str(gap.get("community_id") or "")
        right = (_representative(kg, community, exclude=related[:1])
                 if community else "")
        if not right and len(related) > 1:
            right = labels.get(related[1], "")

    if not left:
        return {"finding": "対象の概念を特定できませんでした (関連ノードが空)。",
                "sources": [], "anchors": []}

    if not right:
        hits = _mentions(corpus, left)
        others = [s for s in hits if s != session]
        if others:
            text = (f"「{left}」は他の {len(others)} セッション "
                    f"({'、'.join(others)}) にも出てきます。"
                    "そこでの繋がり方が橋渡しの手がかりになります。")
        else:
            text = (f"「{left}」に言及している資料は現在のセッション以外に"
                    "ありません。孤立は資料側の不足である可能性が高いです。")
        return {"finding": text, "anchors": [left],
                "sources": _dedup(s for v in hits.values() for s in v)}

    both, only_left, only_right = _both_sides(corpus, left, right)
    if both:
        text = (f"「{left}」と「{right}」の**両方**に触れている資料が "
                f"{len(both)} セッション ({'、'.join(both)}) にあります。"
                "この資料が 2 つの island を繋ぐ根拠になりえます。")
    elif only_left or only_right:
        text = (f"「{left}」と「{right}」を**同時に**扱った資料はありません "
                f"(片側のみ: {left}={len(only_left)} 件 / {right}={len(only_right)} 件)。"
                "両者を結ぶ記述が手元の資料に無いことが断絶の理由です。")
    else:
        text = (f"「{left}」「{right}」のいずれも他セッションに見当たりません。"
                "まず資料を増やす段階です。")

    sources = []
    for name in both or (only_left + only_right):
        sources.extend(_mentions(corpus, left).get(name, []))
        sources.extend(_mentions(corpus, right).get(name, []))
    return {"finding": text, "anchors": [left, right], "sources": _dedup(sources)}


def _discourse(corpus: _Corpus, session: str, gap: dict[str, Any],
               kg: dict[str, Any]) -> dict[str, Any]:
    """欠けている zone (Method/Experiment) を持つ**他セッションの同概念**を探す。"""
    labels = _label_map(kg)
    related = [str(x) for x in gap.get("related_node_ids") or []]
    label = labels.get(related[0], "") if related else ""
    if not label:
        return {"finding": "対象の概念を特定できませんでした。",
                "sources": [], "anchors": []}

    hits = _mentions(corpus, label)
    with_method: list[str] = []
    sources: list[dict[str, Any]] = []
    for other in sorted(s for s in hits if s != session):
        zones = [z for z in corpus.layers(other).get("zones", []) or []
                 if isinstance(z, dict)
                 and str(z.get("zone_label") or "") in METHOD_ZONES]
        if not zones:
            continue
        with_method.append(other)
        names = corpus.documents(other)
        for zone in zones[:2]:
            sources.append(_source("zone", other, str(zone.get("document_id") or ""),
                                   names, label=str(zone.get("zone_label") or ""),
                                   note=str(zone.get("text") or "")[:80]))

    if with_method:
        text = (f"同じ概念「{label}」は {len(with_method)} 件の他セッション "
                f"({'、'.join(with_method)}) にもあり、そちらには手法 "
                f"({'/'.join(METHOD_ZONES)}) の記述があります。"
                "その資料から手法を引いて来られます。")
    elif hits.keys() - {session}:
        text = (f"「{label}」は他セッションにもありますが、どこにも手法 "
                f"({'/'.join(METHOD_ZONES)}) の文がありません。"
                "手法を書いた資料そのものが手元に無い状態です。")
    else:
        # 他セッションに無い場合は**自セッションの中**を見る。このギャップは
        # 「セッションに手法の文が無い」ではなく「その文がこの概念を根拠づけて
        # いない」なので (gaps.py の detection_signal は method_of=0/N)、
        # セッションの zone 一覧をそのまま出すと「Method はあるのに無いと
        # 言う」自己矛盾になる。どちらなのかを見分けて書く。
        own = [z for z in corpus.layers(session).get("zones", []) or []
               if isinstance(z, dict)
               and str(z.get("zone_label") or "") in METHOD_ZONES]
        names = corpus.documents(session)
        if own:
            text = (f"「{label}」を扱った資料はこのセッションだけです。手法 "
                    f"({'/'.join(METHOD_ZONES)}) の文自体は {len(own)} 件ありますが、"
                    f"「{label}」を根拠づける関係には使われていません。"
                    "既存の手法記述とこの概念の対応を確かめるのが先です。")
            sources = [_source("zone", session, str(z.get("document_id") or ""),
                               names, label=str(z.get("zone_label") or ""),
                               note=str(z.get("text") or "")[:80]) for z in own[:3]]
        else:
            text = (f"「{label}」を扱った資料はこのセッションだけで、手法 "
                    f"({'/'.join(METHOD_ZONES)}) の文もありません。"
                    "手法を記した資料の追加が要ります。")
            sources = [s for v in hits.values() for s in v]
    return {"finding": text, "anchors": [label], "sources": _dedup(sources)}


def _causal_terms() -> list[str]:
    """機序手がかり語の平坦化 (CAUSAL_CUES の mechanism 系を優先)。"""
    terms: list[str] = []
    for lexicon in (CAUSAL_CUES_JA, CAUSAL_CUES_EN):
        for category in ("mechanism", "intervention", "counterfactual"):
            terms.extend(lexicon.get(category, []))
    return terms


def _rejection_reason(gap: dict[str, Any]) -> tuple[str, str]:
    """rejection_log の場所と対象エッジ id を取り出す (evidence_links から)。"""
    for link in gap.get("evidence_links") or []:
        if isinstance(link, dict) and link.get("rejection_log"):
            return str(link["rejection_log"]), str(link.get("target_id") or "")
    return "", ""


def _read_rejection(path: str, target_id: str) -> dict[str, Any]:
    """却下ログから対象 1 行を引く。読めなければ空 (finding は続行する)。"""
    if not path or not target_id:
        return {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in reversed(lines):        # 最新の判定が勝つ
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and str(row.get("target_id") or "") == target_id:
            return row
    return {}


def _causal(corpus: _Corpus, session: str, gap: dict[str, Any],
            kg: dict[str, Any]) -> dict[str, Any]:
    """却下理由 + 機序手がかり語で evidence を横断検索する (設計 §2.1)。"""
    labels = _label_map(kg)
    log_path, target_id = _rejection_reason(gap)
    rejected = _read_rejection(log_path, target_id)

    edge = next((e for e in _edges(kg) if str(e.get("id")) == target_id), {})
    left = labels.get(str(edge.get("from") or ""), "")
    right = labels.get(str(edge.get("to") or ""), "")
    if not (left and right):
        related = [labels.get(str(x), "") for x in gap.get("related_node_ids") or []]
        related = [r for r in related if r]
        left = left or (related[0] if related else "")
        right = right or (related[1] if len(related) > 1 else "")

    terms = _causal_terms()
    mechanism: list[dict[str, Any]] = []
    for other in corpus.sessions():
        names = corpus.documents(other)
        for element in _nodes(corpus.kg(other)) + _edges(corpus.kg(other)):
            for span in _spans_of(element):
                surface = str(span.get("surface") or "")
                if not surface or not any(t in surface for t in terms):
                    continue
                if left and left not in surface and right and right not in surface:
                    continue
                cues = find_causal_cues(surface)
                mechanism.append(_source("evidence", other,
                                         str(span.get("document_id") or ""), names,
                                         label="、".join(cues[:3]),
                                         note=surface[:80]))

    scores = rejected.get("scores") or {}
    why = (f"却下時のスコア: " +
           "、".join(f"{k}={v}" for k, v in scores.items()) if scores
           else str(gap.get("detection_signal") or ""))
    pair = f"「{left}」→「{right}」" if left and right else "この関係"
    if mechanism:
        where = sorted({str(s["session"]) for s in mechanism})
        text = (f"{pair} について、機序を述べた記述が {len(mechanism)} 件 "
                f"({'、'.join(where)}) にあります。これを根拠に再検証できます。"
                f"{('（' + why + '）') if why else ''}")
    else:
        text = (f"{pair} の機序を述べた記述は全セッションの根拠スパンに"
                "**ありません**。相関どまりなのは資料に機序が書かれていないため"
                f"で、機序を示す実験か文献が要ります。{('（' + why + '）') if why else ''}")
    return {"finding": text, "anchors": [x for x in (left, right) if x],
            "sources": _dedup(mechanism)}


FINDERS: dict[str, Callable[..., dict[str, Any]]] = {
    KIND_STRUCTURAL: _structural,
    KIND_DISCOURSE: _discourse,
    KIND_CAUSAL: _causal,
}


# ============================================================ LLM (任意)


def _runner(client: Any) -> Callable[[str], str] | None:
    """`client` を prompt -> text の関数に正規化する。

    cc_core は cc_orchestrator を import しない (層の向きを保つ) ので、
    Foundry のクライアントそのものは知らない。`.run(agent, prompt)` を持つ
    オブジェクトか、素の callable のどちらでも受ける — テストは callable を
    渡すだけで済む (このリポジトリの他のテストと同じやり方)。
    """
    if client is None:
        return None
    if callable(client):
        return client
    run = getattr(client, "run", None)
    if callable(run):
        return lambda prompt: run(AGENT, prompt)
    logger.warning("gap_report: client の形が不明なので suggestion を省きます")
    return None


def _suggest(run: Callable[[str], str], item: dict[str, Any]) -> str:
    """1 文の提案をもらう。失敗は握り潰して省略する (finding は残る)。"""
    payload = ("次の JSON を処理し、JSON のみで応答してください。\n" + json.dumps(
        {"task": SUGGEST_TASK, "gap_type": item.get("gap_type"),
         "reason": item.get("reason"), "finding": item.get("finding"),
         "concepts": item.get("anchors") or []}, ensure_ascii=False))
    try:
        data = extract_json(run(payload))
    except Exception as exc:
        logger.warning("gap_report: gap_suggest 失敗 gap=%s (%s)",
                       item.get("gap_id"), type(exc).__name__)
        return ""
    if isinstance(data, dict):
        return str(data.get("suggestion") or "").strip()
    return ""


# ============================================================ ② KB / ③ 外部


def _kb_status(kb_agent: str | None) -> dict[str, Any]:
    """②approved-literature KB。**未設定なら試みもしない** (裁定 R)。"""
    name = kb_agent if kb_agent is not None else os.environ.get(KB_AGENT_ENV, "")
    if not name:
        return {"connected": False, "note": KB_NOT_CONNECTED}
    return {"connected": True, "agent": name,
            "note": f"kb: {name} (R2c では接続先の記録のみ)"}


def _external_enabled(external: bool | None) -> bool:
    if external is not None:
        return bool(external)
    return os.environ.get(EXTERNAL_ENV, "") == "1"


def _log_external(url: str, query: str, *, log_path: str | Path = EXTERNAL_LOG) -> None:
    """送信を 1 件残らず記録する (裁定 R)。書けなければ**送らない**。

    記録できない状態で送信だけ通すと「何を外に出したか」が復元できなくなる。
    ログは付随物ではなく送信の条件。
    """
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
           "url": url, "query": query}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _arxiv_search(query: str, *, limit: int = 3,
                  log_path: str | Path = EXTERNAL_LOG) -> list[dict[str, Any]]:
    """③公開 API。**CC_EXTERNAL_RECS=1 のときしか呼ばれない**。

    送るのは概念ラベルだけ (問い・資料本文・セッション ID は一切含めない)。
    httpx はこの関数の中で import する — モジュール読込の時点でネットワーク
    クライアントを組み立てないため。
    """
    params = f"search_query=all:{query}&start=0&max_results={limit}"
    url = f"{ARXIV_API}?{params}"
    _log_external(url, query, log_path=log_path)      # 送信前に記録する
    try:
        import httpx
        resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        if resp.status_code != 200:
            logger.info("arxiv %d (skip) query=%s", resp.status_code, query)
            return []
        # Atom フィード。先頭の <title> はフィード自身の名前なので落とす
        titles = [t.strip() for t in
                  re.findall(r"<title>(.*?)</title>", resp.text,
                             flags=re.DOTALL)][1:limit + 1]
    except Exception as exc:
        logger.warning("arxiv 検索に失敗 query=%s (%s)", query, type(exc).__name__)
        return []
    return [{"kind": "external", "source": "arxiv", "query": query,
             "title": " ".join(t.split())} for t in titles]


# ============================================================ 組み立て


def _llm_priority(gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """LLM の予算をどこへ使うかの順序 = **型を回しながら**確度の高い順。

    素朴に確度順だけで並べると、同じ型が上位を占めて予算を食い尽くす
    (実測: 5 call すべてが言説ギャップに行き、ほぼ同じ 1 文が 4 本並び、
    構造ギャップ 11 件には 1 件も提案が付かなかった)。型ごとに 1 件ずつ
    配るほうが、同じ call 数で読み手の得る情報が多い。

    未確定 (candidate) を確定済みより先に見るのは変えていない — 提案が要る
    のはまだ人が判断していないギャップのほうなので。
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for gap in gaps:
        buckets.setdefault(str(gap.get("gap_type") or KIND_STRUCTURAL), []).append(gap)
    for rows in buckets.values():
        rows.sort(key=lambda g: (str(g.get("status")) != "candidate",
                                 -float(g.get("confidence") or 0.0),
                                 str(g.get("gap_id"))))
    order = [k for k in (KIND_STRUCTURAL, KIND_DISCOURSE, KIND_CAUSAL)
             if k in buckets] + [k for k in sorted(buckets)
                                 if k not in FINDERS]
    out: list[dict[str, Any]] = []
    for i in range(max((len(v) for v in buckets.values()), default=0)):
        for kind in order:
            if i < len(buckets[kind]):
                out.append(buckets[kind][i])
    return out


def build_gap_report(session: str, store: Any, *, client: Any = None,
                     max_llm_calls: int = 5, external: bool | None = None,
                     kb_agent: str | None = None,
                     external_log: str | Path = EXTERNAL_LOG) -> dict[str, Any]:
    """セッションのギャップに型別の「次の一手」を付けて返す (設計 §2.1)。

    `client` が None なら LLM を 1 回も呼ばず、`suggestion` の無いレポートを
    返す。これで成立することが受け入れ基準 3 の本体。
    """
    corpus = _Corpus(store)
    kg = corpus.kg(session)
    plan = {}
    try:
        plan = store.load_plan(session) or {}
    except Exception as exc:
        logger.warning("gap_report: plan を読めません session=%s (%s)",
                       session, type(exc).__name__)
    gaps = [g for g in plan.get("gaps", []) or [] if isinstance(g, dict)]

    run = _runner(client)
    calls_left = max(0, int(max_llm_calls)) if run else 0
    kb = _kb_status(kb_agent)
    use_external = _external_enabled(external)
    external_note = ""

    items: list[dict[str, Any]] = []
    for gap in _llm_priority(gaps):
        kind = str(gap.get("gap_type") or KIND_STRUCTURAL)
        finder = FINDERS.get(kind)
        if finder is None:
            logger.warning("gap_report: 未知の gap_type=%s", kind)
            continue
        found = finder(corpus, session, gap, kg)
        item: dict[str, Any] = {
            "gap_id": str(gap.get("gap_id") or ""),
            "gap_type": kind,
            "status": str(gap.get("status") or "candidate"),
            "reason": str(gap.get("reason") or ""),
            "anchors": found.get("anchors") or [],
            "finding": found.get("finding") or "",
            "sources": found.get("sources") or [],
        }
        if use_external and item["anchors"]:
            try:
                hits = _arxiv_search(item["anchors"][0], log_path=external_log)
            except OSError as exc:
                # 送信記録が書けない = 送ってよい条件が崩れている。ここまでで
                # 1 件も送っていない (記録は送信の前) ので、以降は諦めて
                # finding だけのレポートにする。**黙って縮退させない** —
                # 外部照会を頼んだ人には理由が要る
                use_external = False
                external_note = (f"送信記録 ({external_log}) を書けないため"
                                 f"外部照会を中止しました ({type(exc).__name__})")
                logger.warning("gap_report: %s", external_note)
            else:
                if hits:
                    item["external"] = hits
        if run and calls_left > 0:
            text = _suggest(run, item)
            calls_left -= 1                # 失敗しても予算は消費する (暴走防止)
            if text:
                item["suggestion"] = text
        items.append(item)

    counts: dict[str, int] = {}
    for item in items:
        counts[item["gap_type"]] = counts.get(item["gap_type"], 0) + 1
    report = {
        "session": session,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "items": items,
        "counts": counts,
        "external_used": bool(use_external and any("external" in i for i in items)),
        "kb": kb,
        # 呼んだ回数と付いた提案の数は別物。呼んで失敗した (トークン切れ等) 場合、
        # llm_calls は増えるが suggestions は増えない — 表示はこちらを使う
        "llm_calls": (max(0, int(max_llm_calls)) - calls_left) if run else 0,
        "suggestions": sum(1 for i in items if i.get("suggestion")),
        "sessions_searched": len(corpus.sessions()),
    }
    if external_note:
        report["external_note"] = external_note
    logger.info("gap report session=%s items=%d counts=%s llm=%d external=%s",
                session, len(items), counts, report["llm_calls"],
                report["external_used"])
    return report


GAP_KIND_JA = {KIND_STRUCTURAL: "構造", KIND_DISCOURSE: "言説", KIND_CAUSAL: "因果"}


def to_markdown(report: dict[str, Any]) -> str:
    """人が読む版。CLI の標準出力と `.md` の中身は同じものを使う。"""
    counts = report.get("counts") or {}
    lines = [
        f"# ギャップレポート — セッション {report.get('session')}",
        "",
        f"- 生成: {report.get('generated_at')}",
        "- 内訳: " + ("、".join(
            f"{GAP_KIND_JA.get(k, k)} {v} 件" for k, v in sorted(counts.items()))
            or "ギャップ候補なし"),
        f"- 横断検索したセッション: {report.get('sessions_searched', 0)} 件",
        f"- LLM 提案: {report.get('suggestions', 0)} 件"
        + ("" if report.get("suggestions") else " (finding のみで成立)"),
        f"- 外部照会: {'あり' if report.get('external_used') else 'なし (既定)'}"
        f" / {(report.get('kb') or {}).get('note', KB_NOT_CONNECTED)}",
        "",
    ]
    if report.get("external_note"):
        lines += [f"> ⚠ {report['external_note']}", ""]
    if not report.get("items"):
        lines += ["ギャップ候補はありません。", ""]
        return "\n".join(lines)

    for kind in (KIND_STRUCTURAL, KIND_DISCOURSE, KIND_CAUSAL):
        group = [i for i in report["items"] if i.get("gap_type") == kind]
        if not group:
            continue
        lines += [f"## {GAP_KIND_JA[kind]}ギャップ ({len(group)} 件)", ""]
        for item in group:
            lines.append(f"### {item.get('gap_id')} [{item.get('status')}]")
            if item.get("reason"):
                lines += ["", f"{item['reason']}"]
            lines += ["", f"**わかっていること**: {item.get('finding')}"]
            if item.get("suggestion"):
                lines += ["", f"**次の一手 (提案)**: {item['suggestion']}"]
            for src in item.get("sources") or []:
                where = src.get("name") or src.get("document_id") or "(資料不明)"
                # ラベルを出さないと、同じ資料が別の概念で 2 回出たときに
                # 重複行にしか見えない (実際はどちらの概念の根拠かが違う)
                what = f" 〈{src['label']}〉" if src.get("label") else ""
                note = f" — {src['note']}" if src.get("note") else ""
                lines.append(f"- 出典: {where}{what} (session {src.get('session')})"
                             f"{note}")
            for ext in item.get("external") or []:
                lines.append(f"- 外部: [{ext.get('source')}] {ext.get('title')}")
            lines.append("")
    return "\n".join(lines)


def save_report(report: dict[str, Any], *,
                out_dir: str | Path = EXPORT_DIR) -> dict[str, Path]:
    """`exports/gap_report_{session}.{json,md}` に保存する (設計 §2.1)。"""
    base = Path(out_dir)
    base.mkdir(parents=True, exist_ok=True)
    session = str(report.get("session") or "unknown")
    json_path = base / f"gap_report_{session}.json"
    md_path = base / f"gap_report_{session}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    md_path.write_text(to_markdown(report), encoding="utf-8")
    return {"json": json_path, "md": md_path}


__all__ = [
    "AGENT", "EXPORT_DIR", "EXTERNAL_ENV", "EXTERNAL_LOG", "GAP_KIND_JA",
    "KB_AGENT_ENV", "KB_NOT_CONNECTED", "SUGGEST_TASK",
    "build_gap_report", "save_report", "to_markdown",
]
