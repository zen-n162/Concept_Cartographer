"""QA 経路 — local / global / hybrid (R2b 設計書 §2・裁定 L/M)。

地図を作らずに問いへ答える 3 経路。どれも「材料を決定的に集め、文章化だけを
LLM に任せる」形になっている。答えの根拠が索引の中の実在するノード・関係・
コミュニティに限られるので、**出典 (sources) が必ず辿れる**。

  local   索引検索 → 出自セッションの 2-hop 近傍 → task=qa 1 call
  global  質問語 × コーパスコミュニティ → 各要約 (キャッシュ命中なら 0 call)
          → task=qa で統合 1 call
  hybrid  local の材料 + global の要約 → task=qa で統合 1 call

裁定 L の要点は「インデックス時の LLM 呼び出しはゼロ」。要約は問いに関係する
上位コミュニティだけをクエリ時に作り、コミュニティ指紋つきでキャッシュする
(`cc_store.corpus.get_summary` / `save_summary`)。2 回目以降は 0 call になる。

## 日本語の質問をどう検索語に割るか

形態素解析器は入れない (新規依存を増やさない・裁定 J の精神)。代わりに向きを
逆にする: **質問を語へ割る**のではなく、**既知の概念ラベルが質問文に含まれるか**
を見る。日本語は分かち書きしないので、割る側に回ると辞書なしでは必ず取りこぼす
が、含まれるかどうかなら NFKC 正規化した部分文字列の判定で済む。
既知ラベルが 1 つも当たらないときだけ、助詞での素朴な分割へ落ちる。

## offline

local は LLM 無しの決定的要約 (関係の列挙 + 根拠) を「LLM なし要約」と明記して
返す。global / hybrid はコーパス要約が LLM 前提なので、当たったコミュニティの
一覧だけを示して「オンライン実行が必要」と案内する。**どちらもエラーにしない** —
Foundry に繋がらないことは利用者の操作の失敗ではないため。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from cc_core import layers_store
from cc_core.editing import normalize_label
from cc_core.logging_util import get_logger
from cc_orchestrator import analysis
from cc_orchestrator.agents_def import MODELS
from cc_store import corpus
from cc_store.files import SessionStore

logger = get_logger("cc_orchestrator.qa")

QA_ROUTES = ("local", "global", "hybrid")

MAX_SEEDS = 8              # 起点にする概念の数 (設計 §2: 上位 ≤8)
MAX_CONTEXT_NODES = 40     # 2-hop 近傍の合計上限 (設計 §2)
HOPS = 2
TOP_COMMUNITIES = 3        # 要約するコミュニティ数 (設計 §2)
MAX_TERMS = 6
MIN_TERM_CHARS = 2
EVIDENCE_CHARS = 240
MAX_SUMMARY_RELATIONS = 30
# 大域 QA は粗いほうの階層を見る。「全体像は?」に対して細かい島を 3 つ選ぶと、
# コーパスの一部を全体像として語ってしまう (corpus.LEVEL_RESOLUTIONS 参照)
GLOBAL_LEVEL = "coarse"

# 関係記号の日本語名。cc_core.normalize.VALID_GLYPHS と 1:1
# (cc_web/static/app.js の GLYPH_INFO と同じ語彙にそろえること)
GLYPH_JA: dict[str, str] = {
    "arrow": "因果", "wave": "相関", "double": "補強", "zigzag": "矛盾",
    "tension": "対立候補", "hole": "ギャップ", "isa": "分類", "partof": "構成",
    "precedes": "時系列", "question": "疑問",
}

# 素朴分割で落とす語 (問いかけの骨組みそのもの。概念名ではない)
QUESTION_STOPWORDS: frozenset[str] = frozenset({
    "関係", "繋がり", "つながり", "全体像", "俯瞰", "テーマ", "まとめ", "まとめて",
    "教えて", "説明", "概要", "違い", "比較", "影響", "原因", "経緯", "理由",
    "なに", "どれ", "どの", "どう", "なぜ", "どうして", "ついて", "教え",
    "研究", "自分", "今週", "今月", "先週", "先月", "最近", "全体",
})
# 区切り文字と助詞。助詞は 1 文字でも割る — 割りすぎた語は索引に当たらない
# だけで害が無く、割り足りないと 1 語も当たらないため (安全側は割るほう)
_SPLIT_RE = re.compile(
    r"[\s、。,\.\?？!！「」『』（）()\[\]【】:：;；/／・…~〜\-—+=]+")
_PARTICLE_RE = re.compile(
    r"(?:との|とは|には|では|から|まで|って|など|ほど|より|ような|[はがをにでとやのへも])")


# ------------------------------------------------------------------ 予算


@dataclass
class Budget:
    """1 問あたりの LLM 呼び出し上限 (設計 §2: CC_QA_MAX_CALLS=6)。

    「使い切ったら黙って質を落とす」ことはしない。`skipped` に残し、答えの
    末尾へその旨を書く — 材料が欠けた答えと十分な材料の答えを、読み手が
    区別できなければならないため。
    """

    limit: int
    used: int = 0
    skipped: int = 0

    def take(self, n: int = 1, *, reserve: int = 0) -> bool:
        """n call ぶん確保する。`reserve` は後段のために残す枠。"""
        if self.used + n + reserve > self.limit:
            self.skipped += n
            return False
        self.used += n
        return True

    @property
    def exhausted(self) -> bool:
        return self.skipped > 0


# ------------------------------------------------------------ 検索語 (§2)


def known_terms(question: str, vocabulary: Iterable[str]) -> list[str]:
    """質問文に現れる既知の概念ラベル (長い順)。

    長い一致に含まれる短い一致は落とす — 「動的概念地図」が当たっている
    ときに「概念地図」も検索語にすると、無関係なセッションの近傍まで
    引き込んで材料が薄まるため。
    """
    norm = normalize_label(question)
    if not norm:
        return []
    found = sorted({str(v) for v in vocabulary
                    if len(str(v)) >= MIN_TERM_CHARS and str(v) in norm},
                   key=lambda v: (-len(v), v))
    kept: list[str] = []
    for term in found:
        if any(term in longer for longer in kept):
            continue
        kept.append(term)
    return kept[:MAX_TERMS]


def split_terms(question: str) -> list[str]:
    """既知ラベルが 1 つも当たらないときの逃げ道 (助詞での素朴な分割)。"""
    out: list[str] = []
    for chunk in _SPLIT_RE.split(question or ""):
        for piece in _PARTICLE_RE.split(chunk):
            term = normalize_label(piece)
            if (len(term) < MIN_TERM_CHARS or term in QUESTION_STOPWORDS
                    or term in out):
                continue
            out.append(term)
    return out[:MAX_TERMS]


def vocabulary(store: SessionStore, level: str = corpus.DEFAULT_LEVEL) -> list[str]:
    """コーパスに存在する正規化ラベルの一覧 (corpus_meta から)。"""
    meta = store.corpus_communities()
    return sorted(corpus.node_communities(meta, level))


def question_terms(question: str, store: SessionStore) -> list[str]:
    """質問文から検索語を決める (既知ラベル優先 → 無ければ素朴分割)。"""
    terms = known_terms(question, vocabulary(store))
    return terms or split_terms(question)


# ------------------------------------------------------------ 材料 (§2)


@dataclass
class Material:
    """QA へ渡す材料と、出典を引き直すための索引。

    `context` はそのまま cc-analysis の payload になる (ref つき)。`sources` は
    ref -> 表示用の出典レコードで、LLM が返した cited を**実在するものだけ**へ
    絞り込むための台帳でもある。
    """

    context: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {"concepts": [], "relations": [], "summaries": []})
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    seeds: list[str] = field(default_factory=list)
    sessions: list[str] = field(default_factory=list)
    communities: list[str] = field(default_factory=list)
    truncated: bool = False
    cache_hits: int = 0

    @property
    def refs(self) -> list[str]:
        return list(self.sources)

    def is_empty(self) -> bool:
        return not any(self.context[k] for k in self.context)

    def payload(self) -> dict[str, list[dict[str, Any]]]:
        """空の節を落とした payload (LLM に「材料が無い」節を見せない)。"""
        return {k: v for k, v in self.context.items() if v}

    def source_list(self, cited: Sequence[str]) -> list[dict[str, Any]]:
        """出典リスト。LLM が何も引かなかったときは**渡した起点**を出す。

        「出典なし」で黙るより、何を見て答えたのかを示すほうが検証できる。
        引用があったかどうかは summary["qa"]["cited"] から分かる。
        """
        picked = [r for r in cited if r in self.sources]
        if not picked:
            picked = [r for r in self.seeds if r in self.sources]
        return [self.sources[r] for r in picked]


def _edge_document(edge: Mapping[str, Any]) -> str:
    for span in edge.get("evidence_span") or ():
        if isinstance(span, Mapping) and span.get("document_id"):
            return str(span["document_id"])
    return ""


def _documents_of(store: SessionStore, session: str) -> dict[str, str]:
    """セッションの document_id -> ファイル名 対応表 (裁定 V)。

    サイドカーが無い / 対応表が無い過去セッションでは空 dict。呼び出し側は
    `resolve_document` を通すので、その場合は生の id がそのまま表示される。
    """
    try:
        return layers_store.documents_of(store.load_layers(session))
    except Exception as exc:      # 層が読めなくても QA そのものは続ける
        logger.warning("qa: layers を読めません session=%s err=%s",
                       session, type(exc).__name__)
        return {}


def _node_documents(edges: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """ノード -> 出典文書。**接続する関係の出典が 1 つに定まるときだけ**返す。

    セッションの代表文書を全ノードに貼ると、実際にはその資料に出てこない
    概念にも出典が付いてしまう。曖昧なら空 (= 出典なし) が正しい。
    """
    seen: dict[str, set[str]] = {}
    for edge in edges:
        doc = _edge_document(edge)
        if not doc:
            continue
        for key in ("from", "to"):
            seen.setdefault(str(edge.get(key)), set()).add(doc)
    return {nid: next(iter(docs)) for nid, docs in seen.items() if len(docs) == 1}


def _seed_hits(store: SessionStore, terms: Sequence[str],
               *, limit: int = MAX_SEEDS) -> list[dict[str, Any]]:
    """検索語ごとに索引を引き、ノードの当たりを順序を保って束ねる (§2)。"""
    seeds: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for term in terms:
        for hit in store.search_nodes(term, limit=limit):
            if hit.get("kind") != "node":
                continue
            key = (str(hit.get("session")), str(hit.get("node_id")))
            if key in seen:
                continue
            seen.add(key)
            seeds.append(hit)
        if len(seeds) >= limit:
            break
    return seeds[:limit]


def local_material(store: SessionStore, terms: Sequence[str], *,
                   max_nodes: int = MAX_CONTEXT_NODES,
                   max_seeds: int = MAX_SEEDS) -> Material:
    """索引の当たり → 出自セッションの 2-hop 近傍 → 材料 (設計 §2)。

    近傍はセッションごとに取り、合計が `max_nodes` を超えないよう予算を
    分け合う。セッションの順序は検索の順位そのもの (決定的)。
    """
    material = Material()
    seeds = _seed_hits(store, terms, limit=max_seeds)
    if not seeds:
        return material

    grouped: dict[str, list[str]] = {}
    for hit in seeds:
        grouped.setdefault(str(hit["session"]), []).append(str(hit["node_id"]))

    budget = max_nodes
    for session, node_ids in grouped.items():
        if budget <= 0:
            material.truncated = True
            break
        try:
            sub = store.neighborhood(session, node_ids, hops=HOPS, max_nodes=budget)
        except Exception as exc:      # 壊れた 1 セッションで問い全体を失わない
            logger.warning("qa: neighborhood failed session=%s err=%s",
                           session, type(exc).__name__)
            continue
        material.sessions.append(session)
        material.truncated = material.truncated or bool(sub.get("truncated"))
        budget -= len(sub["nodes"])

        seed_ids = set(sub.get("seeds") or ())
        docs = _node_documents(sub["edges"])
        names = _documents_of(store, session)      # 裁定 V
        labels: dict[str, str] = {}
        for node in sub["nodes"]:
            nid = str(node.get("id") or "")
            label = str(node.get("label") or "")
            if not nid or not label:
                continue
            labels[nid] = label
            ref = f"n:{session}:{nid}"
            material.context["concepts"].append(
                {"ref": ref, "label": label, "session": session})
            material.sources[ref] = {
                "kind": "node", "label": label, "session": session,
                "document_id": layers_store.resolve_document(
                    docs.get(nid, ""), names)}
            if nid in seed_ids:
                material.seeds.append(ref)
        for edge in sub["edges"]:
            eid = str(edge.get("id") or "")
            src, dst = labels.get(str(edge.get("from"))), labels.get(str(edge.get("to")))
            if not eid or not src or not dst:
                continue
            glyph = str(edge.get("glyph") or "wave")
            evidence = corpus.edge_evidence_text(edge)[:EVIDENCE_CHARS]
            ref = f"e:{session}:{eid}"
            material.context["relations"].append({
                "ref": ref, "from": src, "to": dst,
                "type": GLYPH_JA.get(glyph, glyph),
                "label": str(edge.get("label") or ""), "evidence": evidence,
            })
            material.sources[ref] = {
                "kind": "edge",
                "label": f"{src} →（{GLYPH_JA.get(glyph, glyph)}）→ {dst}",
                "session": session,
                "document_id": layers_store.resolve_document(
                    _edge_document(edge), names)}
    logger.info("qa local material seeds=%d concepts=%d relations=%d sessions=%d",
                len(material.seeds), len(material.context["concepts"]),
                len(material.context["relations"]), len(material.sessions))
    return material


# ------------------------------------------------- コミュニティ要約 (裁定 L)


def rank_communities(meta: Mapping[str, Any], terms: Sequence[str], *,
                     level: str = GLOBAL_LEVEL,
                     top: int = TOP_COMMUNITIES) -> list[tuple[str, list[str]]]:
    """質問語 × メンバーラベルの重なりで上位コミュニティを選ぶ (設計 §2)。

    重なりが 0 のとき (「全体像をまとめて」のように概念名を含まない問い) は
    **大きい島から順に**返す。全体像を尋ねられて「該当なし」と答えるのは
    答えになっていないので、コーパスの主要な塊を材料にする。
    """
    levels = (meta.get("levels") or {}).get(level) or {}
    scored: list[tuple[int, int, str, list[str]]] = []
    for cid, members in levels.items():
        names = [str(m) for m in members]
        score = sum(1 for m in names
                    for t in terms if t and (t in m or m in t))
        scored.append((-score, -len(names), str(cid), names))
    scored.sort()
    return [(cid, members) for _, _, cid, members in scored[:top]]


def community_title(cid: str, members: Sequence[str],
                    labels: Mapping[str, str] | None = None) -> str:
    """島の見出し。**決定的に**作る (キャッシュ命中でも同じ文字列になる)。

    LLM のつけた題を使うと、要約がキャッシュから来たときだけ題が変わる。
    出典の見え方が呼び出しの偶然で揺れるのは避けたい。
    """
    labels = labels or {}
    shown = [labels.get(m, m) for m in list(members)[:2]]
    rest = max(0, len(members) - len(shown))
    head = "・".join(shown) if shown else str(cid)
    return f"{head} ほか {rest} 概念" if rest else head


def _community_relations(graph: corpus.CorpusGraph, members: Sequence[str],
                         *, limit: int = MAX_SUMMARY_RELATIONS) -> list[dict[str, Any]]:
    """島の内側の関係だけを要約の材料にする (外へ出る関係は文脈が足りない)。"""
    inside = set(members)
    rows: list[dict[str, Any]] = []
    for edge in graph.edges:
        if edge.from_norm not in inside or edge.to_norm not in inside:
            continue
        rows.append({
            "from": graph.nodes[edge.from_norm].label,
            "to": graph.nodes[edge.to_norm].label,
            "type": GLYPH_JA.get(edge.glyph, edge.glyph),
            "label": edge.label,
        })
        if len(rows) >= limit:
            break
    return rows


def global_material(store: SessionStore, terms: Sequence[str], *,
                    run: analysis.RunFn | None,
                    report: analysis.AnalysisReport,
                    budget: Budget, reserve: int = 1,
                    model: str = "") -> Material:
    """上位コミュニティの要約を集める (キャッシュ命中なら LLM 0 call・裁定 L)。"""
    material = Material()
    meta = store.corpus_communities()
    picks = rank_communities(meta, terms)
    if not picks:
        return material

    # 合成グラフは**要約の有無に関わらず**組む。ここから採るのは表示ラベル
    # (併合キーは正規化済みなので「ai概念自動抽出」のような小文字になる) で、
    # キャッシュ命中のときだけ島の名前が変わって見えるのを避けるため。
    # LLM は 1 度も呼ばないので、受け入れ基準 3 の「2 回目は 0〜1 call」は保つ。
    graph = corpus.build_corpus_graph(store)
    labels = {k: n.label for k, n in graph.nodes.items()}
    for cid, members in picks:
        material.communities.append(cid)
        cached = corpus.get_summary(store, members)
        text = str((cached or {}).get("text") or "")
        if cached and text:
            material.cache_hits += 1
        elif run is not None:
            if not budget.take(reserve=reserve):
                logger.info("qa: summary skipped (budget) community=%s", cid)
                continue
            result = analysis.run_community_summary(
                run, [labels.get(m, m) for m in members],
                _community_relations(graph, members), report=report)
            text = result.get("summary") or ""
            if text:
                corpus.save_summary(store, members, text, model=model)
        if not text:
            continue
        ref = f"c:{cid}"
        title = community_title(cid, members, labels)
        material.context["summaries"].append(
            {"ref": ref, "title": title, "text": text})
        material.sources[ref] = {"kind": "community", "label": title,
                                 "session": "", "document_id": ""}
        material.seeds.append(ref)
    logger.info("qa global material communities=%d cache_hits=%d calls=%d",
                len(material.communities), material.cache_hits, budget.used)
    return material


# --------------------------------------------------------------- 応答 (§2)


def _merge(*materials: Material) -> Material:
    """材料を束ねる (hybrid 用)。ref が同じものは先勝ち。"""
    merged = Material()
    for part in materials:
        for key, rows in part.context.items():
            for row in rows:
                if row.get("ref") in merged.sources:
                    continue
                merged.context.setdefault(key, []).append(row)
                merged.sources[str(row.get("ref"))] = part.sources[str(row["ref"])]
        merged.seeds.extend(r for r in part.seeds if r not in merged.seeds)
        merged.sessions.extend(s for s in part.sessions if s not in merged.sessions)
        merged.communities.extend(c for c in part.communities
                                  if c not in merged.communities)
        merged.truncated = merged.truncated or part.truncated
        merged.cache_hits += part.cache_hits
    return merged


def _result(route: str, answer: str, material: Material, *,
            cited: Sequence[str] = (), terms: Sequence[str] = (),
            budget: Budget | None = None,
            report: analysis.AnalysisReport | None = None,
            offline: bool = False, insufficient: bool = False) -> dict[str, Any]:
    """summary へそのまま載る形 (answer / sources / qa の 3 キー)。"""
    info: dict[str, Any] = {
        "route": route,
        "terms": list(terms),
        "llm_calls": budget.used if budget else 0,
        "cited": len(cited),
        "seeds": len(material.seeds),
        "sessions": list(material.sessions),
        "communities": list(material.communities),
        "cache_hits": material.cache_hits,
        "truncated": material.truncated,
        "insufficient": insufficient,
    }
    if offline:
        info["offline"] = True
    if budget is not None and budget.exhausted:
        info["budget_exceeded"] = budget.skipped
    if report is not None:
        info.update(report.to_dict())
        info.pop("sentence_source", None)     # 文分割の記録は QA では意味がない
    return {"answer": answer, "sources": material.source_list(cited), "qa": info}


def _no_material(route: str, question: str, terms: Sequence[str]) -> dict[str, Any]:
    """材料が 1 つも無いときの答え。**LLM は呼ばない** (聞く材料が無い)。"""
    hint = ("「" + "」「".join(terms) + "」に当たる概念が索引にありません"
            if terms else "質問から検索語を取り出せませんでした")
    return _result(route,
                   f"{hint}。まだ地図にしていない話題かもしれません。"
                   "`--reindex` で索引を作り直すか、その資料を地図にしてから"
                   "もう一度お尋ねください。",
                   Material(), terms=terms, insufficient=True)


def _offline_local_answer(question: str, material: Material) -> str:
    """LLM なしの決定的要約 (設計 §2 の offline)。列挙であることを明記する。"""
    lines = ["【LLM なし要約】索引から機械的に並べています "
             "(文章にまとめるには Foundry への接続が要ります)。", ""]
    concepts = material.context["concepts"]
    if concepts:
        lines.append(f"■ 関係する概念 ({len(concepts)} 件)")
        for row in concepts[:12]:
            lines.append(f"  ・{row['label']}  [{row['session']}]")
        if len(concepts) > 12:
            lines.append(f"  … 他 {len(concepts) - 12} 件")
    relations = material.context["relations"]
    if relations:
        lines.append("")
        lines.append(f"■ 見つかった関係 ({len(relations)} 件)")
        for row in relations[:12]:
            label = f": {row['label']}" if row.get("label") else ""
            lines.append(f"  ・{row['from']} —[{row['type']}{label}]→ {row['to']}")
            if row.get("evidence"):
                lines.append(f"      根拠: {row['evidence'][:80]}")
        if len(relations) > 12:
            lines.append(f"  … 他 {len(relations) - 12} 件")
    return "\n".join(lines)


def _offline_note(material: Material) -> str:
    """global / hybrid の offline 案内 (エラーにしない)。"""
    lines = ["【オンライン実行が必要】コーパス全体の要約は Foundry の "
             "cc-analysis が作ります。ここでは索引から当たった話題の"
             "かたまりだけをお見せします。"]
    if material.communities:
        lines.append("")
        lines.append(f"■ 関係しそうな話題のかたまり ({len(material.communities)} 件)")
        for cid in material.communities:
            lines.append(f"  ・{cid}")
    return "\n".join(lines)


def _budget_note(budget: Budget) -> str:
    """上限に当たったことを答えの末尾に明記する (設計 §2)。"""
    return (f"※ LLM 呼び出しの上限 (CC_QA_MAX_CALLS={budget.limit}) に達したため、"
            f"材料の一部 ({budget.skipped} 件ぶん) を省いて答えています。")


def _with_budget_note(answer: str, budget: Budget) -> str:
    return f"{answer}\n\n{_budget_note(budget)}" if budget.exhausted else answer


def _runner(client: Any) -> analysis.RunFn | None:
    if client is None:
        return None
    return lambda prompt: client.run(analysis.AGENT, prompt)


# -------------------------------------------------------------- 3 つの入口


def answer_local(question: str, store: SessionStore, client: Any = None, *,
                 offline: bool = False) -> dict[str, Any]:
    """「X と Y の関係は?」— 索引検索 + 2-hop 近傍から答える (設計 §2)。"""
    terms = question_terms(question, store)
    material = local_material(store, terms)
    if material.is_empty():
        return _no_material("local", question, terms)

    run = None if offline else _runner(client)
    if run is None:
        return _result("local", _offline_local_answer(question, material),
                       material, terms=terms, offline=True)

    budget = Budget(analysis.qa_max_calls())
    report = analysis.AnalysisReport()
    if not budget.take():        # 枠が無い (CC_QA_MAX_CALLS を絞った実行)
        return _result("local",
                       _offline_local_answer(question, material) + "\n\n"
                       + _budget_note(budget), material, terms=terms,
                       budget=budget, report=report, insufficient=True)
    result = analysis.run_qa(run, question, material.payload(), report=report)
    return _result("local", result["answer"], material, cited=result["cited"],
                   terms=terms, budget=budget, report=report,
                   insufficient=result["insufficient"])


def answer_global(question: str, store: SessionStore, client: Any = None, *,
                  offline: bool = False) -> dict[str, Any]:
    """「全体像は?」— コーパスコミュニティの要約から答える (設計 §2・裁定 L)。"""
    terms = question_terms(question, store)
    budget = Budget(analysis.qa_max_calls())
    report = analysis.AnalysisReport()
    run = None if offline else _runner(client)
    material = global_material(store, terms, run=run, report=report,
                               budget=budget, model=MODELS["analysis"])
    if run is None:
        return _result("global", _offline_note(material), material,
                       terms=terms, offline=True)
    if material.is_empty():
        if budget.exhausted:      # 要約を 1 つも作れないほど枠が狭かった
            return _result("global",
                           "コーパスの要約を 1 つも作れませんでした。\n\n"
                           + _budget_note(budget), material, terms=terms,
                           budget=budget, report=report, insufficient=True)
        return _no_material("global", question, terms)

    if not budget.take():
        return _result("global", _offline_note(material), material, terms=terms,
                       budget=budget, report=report, insufficient=True)
    result = analysis.run_qa(run, question, material.payload(), report=report)
    return _result("global", _with_budget_note(result["answer"], budget), material,
                   cited=result["cited"], terms=terms, budget=budget, report=report,
                   insufficient=result["insufficient"])


def answer_hybrid(question: str, store: SessionStore, client: Any = None, *,
                  offline: bool = False) -> dict[str, Any]:
    """local の近傍 + global の要約を 1 回で統合する (設計 §2)。"""
    terms = question_terms(question, store)
    budget = Budget(analysis.qa_max_calls())
    report = analysis.AnalysisReport()
    run = None if offline else _runner(client)

    local_part = local_material(store, terms)
    global_part = global_material(store, terms, run=run, report=report,
                                  budget=budget, model=MODELS["analysis"])
    material = _merge(local_part, global_part)
    if run is None:
        note = _offline_note(material)
        if not local_part.is_empty():
            note += "\n\n" + _offline_local_answer(question, local_part)
        return _result("hybrid", note, material, terms=terms, offline=True)
    if material.is_empty():
        return _no_material("hybrid", question, terms)

    if not budget.take():
        return _result("hybrid",
                       _offline_local_answer(question, material) + "\n\n"
                       + _budget_note(budget), material, terms=terms,
                       budget=budget, report=report, insufficient=True)
    result = analysis.run_qa(run, question, material.payload(), report=report)
    return _result("hybrid", _with_budget_note(result["answer"], budget), material,
                   cited=result["cited"], terms=terms, budget=budget, report=report,
                   insufficient=result["insufficient"])


# 経路名 -> 応答関数 (pipeline のディスパッチ表がこれを取り込む)
ANSWERERS: dict[str, Callable[..., dict[str, Any]]] = {
    "local": answer_local,
    "global": answer_global,
    "hybrid": answer_hybrid,
}

__all__ = ["ANSWERERS", "Budget", "Material", "QA_ROUTES", "answer_global",
           "answer_hybrid", "answer_local", "known_terms", "local_material",
           "question_terms", "rank_communities", "split_terms", "vocabulary"]
