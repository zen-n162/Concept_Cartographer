"""コーパスグラフ — 全セッションを 1 枚に併合した合成グラフ (R2b 設計書 §1・裁定 K)。

セッションごとの KG は「その週に読んだ資料の地図」でしかない。「X と Y の関係は?」
「全体像は?」に答えるには、**セッションを跨いで同じ概念を同じ点として見る**必要が
ある。id はセッションローカルなので、併合キーは `editing.normalize_label`
(NFKC + trim + casefold) — 編集の重複検査が使っているのと同じ照合キーである。

指紋 = 構成ファイルの (パス, サイズ, mtime) ハッシュ。**指紋が変わったら丸ごと
再計算**する (裁定 K)。個人規模 (数千ノード) では全再計算が最軽量で、増分更新の
状態管理を持たない分だけ壊れる余地が無い。真の増分は AGE 導入時に入れる。

生成物は 3 つ。いずれも `graphs/corpus/` 以下の**派生物**で、消しても作り直せる:

  corpus_meta.json  {fingerprint, levels: {coarse, fine}, built_at}
  summaries.json    {community_fingerprint: {text, made_at, model}}  ← R2b-1 では枠のみ
  index.sqlite      検索索引 (cc_store.index)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import networkx as nx

from cc_core.community import detect_communities
from cc_core.editing import normalize_label
from cc_core.logging_util import get_logger

if TYPE_CHECKING:
    from cc_store.files import SessionStore

logger = get_logger("cc_store.corpus")

CORPUS_META = "corpus_meta.json"
SUMMARIES = "summaries.json"

# コーパス成果物のスキーマ世代。上げると指紋が一致しても作り直す
CORPUS_VERSION = 1

# 階層コミュニティの 2 段 (設計 §1)。
# RBConfigurationVertexPartition は **resolution が高いほど細かく割れる**ので、
# fine = 1.0 / coarse = 0.4 になる。ここを取り違えると「粗いほうが島が多い」
# という逆さまの階層ができるので、対応を表で固定しておく。
LEVEL_RESOLUTIONS: dict[str, float] = {"coarse": 0.4, "fine": 1.0}
LEVEL_ORDER = ("coarse", "fine")
# 索引の corpus_community 列と QA の材料に使う既定レベル
DEFAULT_LEVEL = "fine"


# ---------------------------------------------------------------- データ型


@dataclass
class CorpusNode:
    """併合後の概念。`label_norm` が同一性、`sources` が出自。

    `sources` の各行は {session, node_id, label, importance, onto_class} を持つ。
    そのセッションでの**元の表記**を残すのは、併合しても「どのセッションでは
    どう書かれていたか」を失わないため (索引の 1 行 = 1 出自になる)。
    """

    label_norm: str
    label: str                                   # 代表ラベル = 最新セッションの表記
    sources: list[dict[str, Any]] = field(default_factory=list)
    onto_class: str = ""
    importance: float = 0.0                      # 出自のうち最大値

    @property
    def sessions(self) -> list[str]:
        return sorted({s["session"] for s in self.sources}, reverse=True)

    def to_dict(self) -> dict[str, Any]:
        return {"label_norm": self.label_norm, "label": self.label,
                "sources": self.sources, "onto_class": self.onto_class,
                "importance": round(self.importance, 4)}


@dataclass
class CorpusEdge:
    """併合後の関係。`weight` = 何回現れたか (裁定 K)。

    `sources` の各行は {session, edge_id, label, evidence} を持つ (evidence は
    根拠文 surface の連結)。
    """

    from_norm: str
    to_norm: str
    glyph: str
    label: str = ""
    weight: int = 0
    sources: list[dict[str, Any]] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.from_norm, self.to_norm, self.glyph)

    def to_dict(self) -> dict[str, Any]:
        return {"from_norm": self.from_norm, "to_norm": self.to_norm,
                "glyph": self.glyph, "label": self.label,
                "weight": self.weight, "sources": self.sources}


@dataclass
class CorpusGraph:
    """併合済みの合成グラフ。ノードは正規化ラベル 1 件につき 1 つ。"""

    nodes: dict[str, CorpusNode] = field(default_factory=dict)
    edges: list[CorpusEdge] = field(default_factory=list)
    sessions: list[str] = field(default_factory=list)

    def to_networkx(self) -> nx.Graph:
        """コミュニティ検出用の無向グラフ (重み = 出現回数)。

        自己ループは落とす (`community.build_graph` と同じ扱い)。同じ概念対に
        向き違い・glyph 違いの関係が何本あっても、結び付きの強さとしては
        足し合わせる。
        """
        g = nx.Graph()
        for key, node in self.nodes.items():
            g.add_node(key, label=node.label)
        for edge in self.edges:
            a, b = edge.from_norm, edge.to_norm
            if a == b or a not in g or b not in g:
                continue
            if g.has_edge(a, b):
                g[a][b]["weight"] += float(edge.weight)
            else:
                g.add_edge(a, b, weight=float(edge.weight))
        return g


# ------------------------------------------------------------------ 指紋


def fingerprint(store: SessionStore) -> str:
    """構成ファイルの (パス, サイズ, mtime) から指紋を作る (裁定 K)。

    内容ハッシュにしないのは、数百 MB を読み直さずに「変わったか」を判定
    したいから。mtime は複製 (`cp -p` 等) を跨いで保存されるので、同じ
    ファイル群なら別ディレクトリでも同じ指紋になる。
    """
    payload = "\n".join(f"{name}\t{size}\t{mtime}"
                        for name, size, mtime in store.fingerprint_inputs())
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"v{CORPUS_VERSION}:{digest}"


def community_fingerprint(members: list[str]) -> str:
    """コミュニティの指紋 = メンバー正規化ラベルの集合ハッシュ (裁定 L)。

    要約キャッシュの鍵。**並び順に依存しない**ので、Leiden が同じ集合を違う
    順で返しても、あるいは community_id が振り直されても、要約は使い回せる。
    メンバーが 1 つでも変われば別の鍵になり、古い要約は自然に使われなくなる。
    """
    payload = "\n".join(sorted(set(members)))
    return "cf:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# -------------------------------------------------------------- 併合 (§1)


def edge_evidence_text(edge: dict[str, Any]) -> str:
    """エッジの evidence_span から検索対象の本文をつくる (surface の連結)。

    「どの資料のどの文からこの関係が出たか」は検索の入口として強い。
    span の形が揃っていない旧世代 (文字列だけ) も拾えるようにしてある。
    """
    parts: list[str] = []
    for span in edge.get("evidence_span") or []:
        if isinstance(span, dict):
            text = span.get("surface") or span.get("text") or ""
        else:
            text = str(span)
        text = str(text).strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def build_corpus_graph(store: SessionStore) -> CorpusGraph:
    """全セッションの KG を正規化ラベルで併合した合成グラフを作る (§1)。

    - ノード: `normalize_label` が同じものは 1 つ。代表ラベルは**最新
      セッションの表記** (「機械学習」を後で「ML」と直したなら新しいほうを見せる)
    - エッジ: (from_norm, to_norm, glyph) で併合し、出現回数を weight にする
    - 出自 (session, node_id/edge_id) は全部残す — QA が「どのセッションの
      どの資料から来たか」を示せなければ、答えは検証できない
    """
    graph = CorpusGraph()
    edge_index: dict[tuple[str, str, str], CorpusEdge] = {}
    # 新しい順に処理する。代表ラベルは先に見たもの (= 最新) が勝つ
    graph.sessions = store.list_sessions()

    for session in graph.sessions:
        try:
            kg = store.load_kg(session)
        except Exception as exc:  # 壊れた 1 セッションで全体を失わない
            logger.warning("corpus: session skipped session=%s err=%s",
                           session, type(exc).__name__)
            continue
        importance = store.importance_map(session)

        local: dict[str, str] = {}       # node_id -> label_norm
        for node in sorted(kg.get("nodes", []), key=lambda n: str(n.get("id") or "")):
            nid = str(node.get("id") or "")
            key = normalize_label(node.get("label"))
            if not nid or not key:
                continue
            local[nid] = key
            onto = str(node.get("onto_class") or "")
            score = float(importance.get(nid, 0.0))
            entry = graph.nodes.get(key)
            if entry is None:
                entry = CorpusNode(label_norm=key, label=str(node.get("label") or key))
                graph.nodes[key] = entry
            entry.sources.append({
                "session": session, "node_id": nid,
                "label": str(node.get("label") or key),
                "community_id": str(node.get("community_id") or ""),
                "onto_class": onto, "importance": score,
            })
            if not entry.onto_class and onto:
                entry.onto_class = onto
            entry.importance = max(entry.importance, score)

        for edge in sorted(kg.get("edges", []), key=lambda e: str(e.get("id") or "")):
            src, dst = local.get(str(edge.get("from"))), local.get(str(edge.get("to")))
            if not src or not dst:
                continue
            key = (src, dst, str(edge.get("glyph") or "wave"))
            merged = edge_index.get(key)
            if merged is None:
                merged = CorpusEdge(from_norm=src, to_norm=dst, glyph=key[2],
                                    label=str(edge.get("label") or ""))
                edge_index[key] = merged
                graph.edges.append(merged)
            merged.weight += 1
            merged.sources.append({
                "session": session, "edge_id": str(edge.get("id") or ""),
                "label": str(edge.get("label") or ""),
                "evidence": edge_evidence_text(edge),
            })

    graph.edges.sort(key=lambda e: e.key)   # 決定的な並び (再構築で同じ内容)
    logger.info("corpus graph built sessions=%d nodes=%d edges=%d",
                len(graph.sessions), len(graph.nodes), len(graph.edges))
    return graph


# ------------------------------------------------------ 階層コミュニティ


def _detect_levels(graph: CorpusGraph) -> dict[str, dict[str, list[str]]]:
    """粗 (0.4) / 細 (1.0) の 2 段で Leiden をかける (§1)。

    seed は `community.LEIDEN_SEED` で固定されているので、同じ合成グラフから
    は毎回同じ分割が出る。community_id はレベル名で名前空間を分ける
    (`corpus_fine_000`) — 2 段の id が混ざると、どちらの階層の話をしているか
    分からなくなるため。
    """
    g = graph.to_networkx()
    levels: dict[str, dict[str, list[str]]] = {}
    for level in LEVEL_ORDER:
        mapping = detect_communities(g, resolution=LEVEL_RESOLUTIONS[level])
        members: dict[str, list[str]] = {}
        for label_norm, cid in mapping.items():
            members.setdefault(f"corpus_{level}_{cid[len('comm_'):]}", []).append(label_norm)
        levels[level] = {cid: sorted(names) for cid, names in sorted(members.items())}
    return levels


def node_communities(meta: dict[str, Any], level: str = DEFAULT_LEVEL) -> dict[str, str]:
    """corpus_meta から label_norm -> community_id の逆引きを作る。"""
    out: dict[str, str] = {}
    for cid, members in ((meta.get("levels") or {}).get(level) or {}).items():
        for name in members:
            out[str(name)] = str(cid)
    return out


# --------------------------------------------------------------- 入出力


def meta_path(store: SessionStore) -> Path:
    return store.corpus_dir / CORPUS_META


def summaries_path(store: SessionStore) -> Path:
    return store.corpus_dir / SUMMARIES


def load_corpus_meta(store: SessionStore) -> dict[str, Any] | None:
    """corpus_meta.json を読む。壊れていたら None (= 作り直す)。"""
    path = meta_path(store)
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return meta if isinstance(meta, dict) else None


def save_corpus_meta(store: SessionStore, meta: dict[str, Any]) -> Path:
    path = meta_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def corpus_communities(store: SessionStore, *, force: bool = False,
                       graph: CorpusGraph | None = None) -> dict[str, Any]:
    """コーパスの階層コミュニティを返す (指紋が合えばファイルを再利用)。

    戻り値 = corpus_meta.json の中身:
      {version, fingerprint, built_at, sessions, nodes, edges,
       levels: {coarse: {cid: [label_norm…]}, fine: {…}}}

    `built_at` 以外は同じファイル群から常に同じ内容になる (決定性)。
    """
    fp = fingerprint(store)
    if not force:
        cached = load_corpus_meta(store)
        if cached and cached.get("fingerprint") == fp and cached.get("levels"):
            return cached
        if cached:
            logger.info("corpus meta stale; rebuilding (fingerprint changed)")

    corpus = graph if graph is not None else build_corpus_graph(store)
    levels = _detect_levels(corpus)
    meta = {
        "version": CORPUS_VERSION,
        "fingerprint": fp,
        "built_at": dt.datetime.now().isoformat(timespec="seconds"),
        "sessions": corpus.sessions,
        "nodes": len(corpus.nodes),
        "edges": len(corpus.edges),
        "levels": levels,
    }
    save_corpus_meta(store, meta)
    logger.info("corpus communities built coarse=%d fine=%d",
                len(levels.get("coarse") or {}), len(levels.get("fine") or {}))
    return meta


# ------------------------------------------------- 要約キャッシュ (枠のみ)
#
# 裁定 L: インデックス時の LLM 呼び出しはゼロ。要約は「質問に関係する上位
# コミュニティだけ」をクエリ時に作り、コミュニティ指紋つきでここへ貯める。
# R2b-1 では入れ物だけ用意し、書き込むのは R2b-2 (cc_orchestrator.qa)。


def load_summaries(store: SessionStore) -> dict[str, Any]:
    """要約キャッシュを読む。無い/壊れていれば空 (= 全部作り直し)。"""
    try:
        data = json.loads(summaries_path(store).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def get_summary(store: SessionStore, members: list[str]) -> dict[str, Any] | None:
    """そのメンバー集合に対する要約 (キャッシュ命中なら LLM 0 call)。"""
    return load_summaries(store).get(community_fingerprint(members))


def save_summary(store: SessionStore, members: list[str], text: str, *,
                 model: str = "") -> str:
    """要約を指紋つきで貯める。戻り値は使った鍵 (community_fingerprint)。"""
    key = community_fingerprint(members)
    cache = load_summaries(store)
    cache[key] = {
        "text": str(text),
        "made_at": dt.datetime.now().isoformat(timespec="seconds"),
        "model": str(model),
    }
    path = summaries_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return key
