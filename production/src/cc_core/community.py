"""可変詳細度の中核: コミュニティ検出・重要度スコア・ノード選抜・集約 (v3 §2.4)。

概念マップのノード数が概ね 50 を超えると人間の理解効率が急速に低下する
(v3 §2.4)。このモジュールは知識グラフから 3 段階の詳細度を機械的に導出する。

  Overview  10〜20 ノード   全体俯瞰・初学者の理解・外部説明資料
  Standard  20〜50 ノード   通常のレビュー・テーマ間の関係把握
  Detailed  50〜100 ノード  詳細分析・専門家レビュー・ギャップ精査

v3 §2.4 の技術実装方針に対応:
  ① グラフクラスタリング (Leiden 法) によるサブグラフ分割  -> detect_communities
  ② 重要度に基づく Top-K ノード抽出                        -> score_importance / select_nodes
     (媒介中心性・出現頻度・新規性スコアの加重平均)
  ③ 上位ノードへの集約に伴う短い要約ラベルの生成            -> build_aggregates
     (ラベル本文の生成は LLM 側。ここでは集約単位と代表根拠を決める)
  ④ 詳細モードからのドリルダウン「ノード展開」              -> expand_aggregate

設計上の制約:
- **決定的**であること。同じ knowledge_graph からは常に同じ結果が出る
  (Leiden の seed 固定、同点は node id で安定ソート)。切替のたびに図が
  変わると v3 §2.4 の「認知的所有感」を損なうため。
- グラフ DB に依存しない。R1 のマップ規模 (≤100 ノード) ではインメモリで足り、
  ACA 経路でも fallback 経路でも同一コードが動く (計画 §3-2)。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

import networkx as nx

from cc_core.logging_util import get_logger

logger = get_logger("cc_core.community")

# v3 §2.4 のノード数帯
LEVEL_BANDS: dict[str, tuple[int, int]] = {
    "overview": (10, 20),
    "standard": (20, 50),
    "detailed": (50, 100),
}
LEVEL_ORDER = ("overview", "standard", "detailed")

# 重要度の初期重み (v3 §2.4: 媒介中心性・出現頻度・新規性の加重平均)
# R1 パイロットで校正する前提の暫定値 (計画 §4)。
DEFAULT_WEIGHTS = {"betweenness": 0.4, "frequency": 0.3, "novelty": 0.3}

LEIDEN_SEED = 42  # 決定性のため固定


# ---------------------------------------------------------------- グラフ構築


def build_graph(kg: dict[str, Any]) -> nx.Graph:
    """knowledge_graph から無向グラフを作る (中心性・コミュニティ検出用)。

    重み付けはエッジの confidence (無指定は 1.0)。多重辺は重みを加算する。
    """
    g = nx.Graph()
    for n in kg.get("nodes", []):
        g.add_node(n["id"], label=n.get("label", n["id"]),
                   community_hint=n.get("community_id"))
    for e in kg.get("edges", []):
        a, b = e.get("from"), e.get("to")
        if a not in g or b not in g or a == b:
            continue
        w = float(e.get("confidence", 1.0) or 1.0)
        if g.has_edge(a, b):
            g[a][b]["weight"] += w
            g[a][b]["count"] += 1
        else:
            g.add_edge(a, b, weight=w, count=1)
    return g


# ------------------------------------------------------- コミュニティ検出 ①


def detect_communities(g: nx.Graph, *, resolution: float = 1.0) -> dict[str, str]:
    """Leiden 法でコミュニティを検出し node_id -> community_id を返す。

    leidenalg が使えない環境では networkx の Louvain へ自動フォールバックする
    (Leiden は Louvain の改良版で、どちらもモジュラリティ最大化。結果の質は
    落ちるが可変詳細度の機能自体は維持される)。
    """
    if g.number_of_nodes() == 0:
        return {}
    if g.number_of_edges() == 0:
        # 孤立ノードのみ: 各ノードを独立コミュニティにする
        return {n: f"comm_{i:03d}" for i, n in enumerate(sorted(g.nodes()))}

    partition: list[list[str]] | None = None
    try:
        import igraph as ig
        import leidenalg

        nodes = sorted(g.nodes())
        index = {n: i for i, n in enumerate(nodes)}
        ig_graph = ig.Graph(n=len(nodes))
        ig_graph.add_edges([(index[u], index[v]) for u, v in sorted(g.edges())])
        ig_graph.es["weight"] = [g[u][v]["weight"] for u, v in sorted(g.edges())]
        part = leidenalg.find_partition(
            ig_graph, leidenalg.RBConfigurationVertexPartition,
            weights="weight", resolution_parameter=resolution, seed=LEIDEN_SEED)
        partition = [[nodes[i] for i in sorted(comm)] for comm in part]
        logger.info("communities detected method=leiden n=%d", len(partition))
    except Exception as exc:  # pragma: no cover - 環境依存の退避路
        logger.warning("leiden unavailable (%s); falling back to louvain",
                       type(exc).__name__)
        from networkx.algorithms.community import louvain_communities

        comms = louvain_communities(g, weight="weight", seed=LEIDEN_SEED,
                                    resolution=resolution)
        partition = [sorted(c) for c in comms]
        logger.info("communities detected method=louvain n=%d", len(partition))

    # 大きい順 → 同数なら代表 id 順に並べ、決定的な community_id を振る
    partition.sort(key=lambda c: (-len(c), c[0]))
    mapping: dict[str, str] = {}
    for i, members in enumerate(partition):
        cid = f"comm_{i:03d}"
        for n in members:
            mapping[n] = cid
    return mapping


# --------------------------------------------------------------- 重要度 ②


@dataclass
class ImportanceBreakdown:
    """重要度の内訳 (UI で「なぜこのノードが残ったか」を説明するために保持)。"""

    betweenness: float
    frequency: float
    novelty: float
    total: float

    def to_dict(self) -> dict[str, float]:
        return {
            "betweenness": round(self.betweenness, 4),
            "frequency": round(self.frequency, 4),
            "novelty": round(self.novelty, 4),
            "total": round(self.total, 4),
        }


def _normalize(values: dict[str, float]) -> dict[str, float]:
    """0-1 に正規化する (全て同値なら 0.5 を返し、順位を作らない)。"""
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if math.isclose(hi, lo):
        return {k: 0.5 for k in values}
    return {k: (v - lo) / (hi - lo) for k, v in values.items()}


def score_importance(
    g: nx.Graph,
    kg: dict[str, Any],
    *,
    weights: dict[str, float] | None = None,
) -> dict[str, ImportanceBreakdown]:
    """v3 §2.4 の「媒介中心性・出現頻度・新規性スコアの加重平均」を計算する。

    - 媒介中心性: グラフ上の橋渡し度。低いと周辺、高いとテーマ間の要。
    - 出現頻度:   資料中での言及の多さ。knowledge_graph の nodes[].mentions
                  があればそれを、無ければ次数 (関係の本数) を代理に使う。
    - 新規性:     既知概念でないほど高い。nodes[].novelty があればそれを、
                  無ければ「次数が低く、かつ最近の資料に出た」ほど高いとみなす
                  代理指標を使う (資料日付が無い場合は次数の逆数のみ)。
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    node_meta = {n["id"]: n for n in kg.get("nodes", [])}
    ids = sorted(g.nodes())
    if not ids:
        return {}

    # --- 媒介中心性 ---
    if g.number_of_edges() == 0:
        betw = {n: 0.0 for n in ids}
    else:
        betw = nx.betweenness_centrality(g, weight=None, normalized=True)

    # --- 出現頻度 ---
    freq: dict[str, float] = {}
    for n in ids:
        meta = node_meta.get(n, {})
        if meta.get("mentions") is not None:
            freq[n] = float(meta["mentions"])
        else:
            freq[n] = float(g.degree(n))

    # --- 新規性 ---
    nov: dict[str, float] = {}
    max_deg = max((g.degree(n) for n in ids), default=0) or 1
    for n in ids:
        meta = node_meta.get(n, {})
        if meta.get("novelty") is not None:
            nov[n] = float(meta["novelty"])
        else:
            # 既存の中心概念ほど「既知」とみなし、周辺の概念に新規性を与える。
            nov[n] = 1.0 - (g.degree(n) / max_deg)

    nb, nf, nv = _normalize(betw), _normalize(freq), _normalize(nov)
    out: dict[str, ImportanceBreakdown] = {}
    for n in ids:
        total = (w["betweenness"] * nb[n] + w["frequency"] * nf[n]
                 + w["novelty"] * nv[n])
        out[n] = ImportanceBreakdown(nb[n], nf[n], nv[n], total)
    return out


# ---------------------------------------------------------- ノード選抜 ②


# 各レベルが全体の何割を見せるか。帯 (v3 §2.4) だけだと、元のグラフが
# 小さいとき 3 レベルが同一になって「詳細度を選ぶ」意味が消えるため、
# 比率でレベルを分化させたうえで帯を上限・下限として適用する。
LEVEL_RATIO = {"overview": 0.35, "standard": 0.70, "detailed": 1.0}


def _target_count(level: str, total: int) -> int:
    """その詳細度で表示するノード数を決める。

    v3 §2.4 の帯を絶対的な上限とし、比率で下位レベルとの差を作る:
      total=100 -> overview 20 / standard 50 / detailed 100
      total= 60 -> overview 20 / standard 42 / detailed  60
      total= 19 -> overview 10 / standard 19 / detailed  19
    元のグラフが帯の下限より小さければ、そのまま全部見せる。
    """
    lo, hi = LEVEL_BANDS[level]
    if total <= lo:
        return total
    target = round(LEVEL_RATIO[level] * total)
    return int(min(hi, total, max(lo, target)))


def _select_k(
    importance: dict[str, ImportanceBreakdown],
    communities: dict[str, str],
    k: int,
) -> list[str]:
    """重要度上位 k 件を選ぶ。ただし各コミュニティから最低 1 つを確保する。

    単純な上位 K だと 1 つのコミュニティに偏って「島」が丸ごと消えるため、
    まず各コミュニティの最重要ノードを確保し、残り枠を全体順で埋める。
    これにより Overview でも全テーマが地図上に残る (v3 §2.4「全体俯瞰」)。
    """
    by_comm: dict[str, list[str]] = {}
    for node, cid in communities.items():
        if node in importance:
            by_comm.setdefault(cid, []).append(node)

    def rank_key(n: str) -> tuple[float, str]:
        return (-importance[n].total, n)  # 重要度降順 → 同点は id 昇順 (決定性)

    selected: list[str] = []
    for cid in sorted(by_comm, key=lambda c: (-len(by_comm[c]), c)):
        members = sorted(by_comm[cid], key=rank_key)
        if members and len(selected) < k:
            selected.append(members[0])

    for n in sorted((n for n in importance if n not in selected), key=rank_key):
        if len(selected) >= k:
            break
        selected.append(n)

    return sorted(selected, key=rank_key)


def count_aggregates(
    communities: dict[str, str],
    visible: Iterable[str],
    *,
    min_members: int = 2,
) -> int:
    """その選抜で生じる集約ノードの数 (表示枠を消費する)。"""
    visible_set = set(visible)
    hidden: dict[str, int] = {}
    for node, cid in communities.items():
        if node not in visible_set:
            hidden[cid] = hidden.get(cid, 0) + 1
    return sum(1 for c in hidden.values() if c >= min_members)


def select_nodes(
    importance: dict[str, ImportanceBreakdown],
    communities: dict[str, str],
    level: str,
    *,
    total_nodes: int | None = None,
    min_members: int = 2,
) -> list[str]:
    """指定した詳細度で表示する概念ノードを Top-K 選抜する。

    **集約ノードも画面上の表示枠を消費する**ため、概念数 + 集約数が帯の上限を
    超えないよう概念枠を削って収束させる。認知負荷 (v3 §2.4) は「画面に出る
    要素の総数」で決まり、集約ノードも読む対象だからである。
    """
    total = total_nodes if total_nodes is not None else len(importance)
    hi = LEVEL_BANDS[level][1]
    n_comms = len(set(communities.values())) or 1

    k = _target_count(level, total)
    selected = _select_k(importance, communities, k)
    if total <= hi and k >= total:
        return selected  # 全部表示できるなら集約は生じない

    # (1) 概念 k + 集約 a <= hi になるまで k を下げる
    for _ in range(12):
        aggs = count_aggregates(communities, selected, min_members=min_members)
        if len(selected) + aggs <= hi or k <= n_comms:
            break
        k = max(n_comms, hi - aggs)
        selected = _select_k(importance, communities, k)

    # (2) それでも超えるなら (コミュニティ数が多く「代表 + 集約」で 2 枠ずつ
    #     消費している状態)、重要度の低いコミュニティから代表を落とし、
    #     そのコミュニティは集約ノードだけで表現する。1 枠ずつ確実に減る。
    members_of: dict[str, list[str]] = {}
    for node, cid in communities.items():
        if node in importance:
            members_of.setdefault(cid, []).append(node)

    def comm_rank(cid: str) -> tuple[float, str]:
        best = max((importance[n].total for n in members_of[cid]), default=0.0)
        return (best, cid)

    selected_set = set(selected)
    # 重要度の低いコミュニティ順 (同点は id 降順) に代表を外す候補にする
    for cid in sorted(members_of, key=comm_rank):
        if len(selected_set) + count_aggregates(
                communities, selected_set, min_members=min_members) <= hi:
            break
        reps = [n for n in members_of[cid] if n in selected_set]
        # 全メンバーを隠しても集約が成立する (>= min_members) 場合のみ落とす。
        # そうでないと島そのものが地図から消えてしまう。
        if len(reps) == 1 and len(members_of[cid]) >= min_members:
            selected_set.discard(reps[0])

    return sorted(selected_set, key=lambda n: (-importance[n].total, n))


# ------------------------------------------------------------- 集約 ③


@dataclass
class Aggregate:
    """集約ノード: そのコミュニティの非表示メンバーを1つに畳んだもの。"""

    id: str
    community_id: str
    summary_label: str
    member_node_ids: list[str]
    representative_node_id: str
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "community_id": self.community_id,
            "summary_label": self.summary_label,
            "member_node_ids": self.member_node_ids,
            "representative_node_id": self.representative_node_id,
            "evidence": self.evidence,
        }


def build_aggregates(
    kg: dict[str, Any],
    communities: dict[str, str],
    importance: dict[str, ImportanceBreakdown],
    visible: Iterable[str],
    *,
    community_names: dict[str, str] | None = None,
    min_members: int = 2,
    max_aggregates: int | None = None,
) -> list[Aggregate]:
    """非表示ノードをコミュニティ単位で集約ノードにまとめる。

    summary_label は「コミュニティ名 ほかN概念」の機械生成ラベルを既定とし、
    LLM による短い要約ラベル (v3 §2.4③) が与えられた場合はそれで上書きする
    (cc_orchestrator 側で cc-map-architect が前計算する)。

    max_aggregates を超える場合は、重要度の低いコミュニティを 1 つの
    「その他」集約へ併合する。コミュニティ数が表示枠より多い大規模グラフでも
    画面上の要素数が帯 (v3 §2.4) を超えないようにするため。
    """
    visible_set = set(visible)
    node_meta = {n["id"]: n for n in kg.get("nodes", [])}
    names = community_names or {}

    hidden_by_comm: dict[str, list[str]] = {}
    for node, cid in communities.items():
        if node not in visible_set and node in node_meta:
            hidden_by_comm.setdefault(cid, []).append(node)

    aggregates: list[Aggregate] = []
    for cid in sorted(hidden_by_comm):
        members = sorted(hidden_by_comm[cid],
                         key=lambda n: (-importance[n].total, n)
                         if n in importance else (0.0, n))
        if len(members) < min_members:
            continue
        rep = members[0]
        base = names.get(cid) or node_meta.get(rep, {}).get("label", cid)
        evidence = []
        for m in members[:3]:
            spans = node_meta.get(m, {}).get("evidence_span") or []
            if spans:
                evidence.append({"node_id": m, "span": spans[0]})
        aggregates.append(Aggregate(
            id=f"agg-{cid}",
            community_id=cid,
            summary_label=f"{base} ほか{len(members)}概念",
            member_node_ids=members,
            representative_node_id=rep,
            evidence=evidence,
        ))

    if max_aggregates is not None and len(aggregates) > max_aggregates > 0:
        def agg_rank(a: Aggregate) -> tuple[float, str]:
            best = max((importance[m].total for m in a.member_node_ids
                        if m in importance), default=0.0)
            return (-best, a.community_id)

        aggregates.sort(key=agg_rank)
        keep, merge = aggregates[: max_aggregates - 1], aggregates[max_aggregates - 1:]
        merged_members: list[str] = []
        for a in merge:
            merged_members.extend(a.member_node_ids)
        keep.append(Aggregate(
            id="agg-misc",
            community_id="comm_misc",
            summary_label=f"その他{len(merge)}領域 ほか{len(merged_members)}概念",
            member_node_ids=sorted(merged_members),
            representative_node_id=merge[0].representative_node_id,
            evidence=[e for a in merge[:2] for e in a.evidence[:1]],
        ))
        logger.info("aggregates merged into misc: %d -> %d", len(aggregates), len(keep))
        aggregates = keep

    return sorted(aggregates, key=lambda a: a.id)


# ------------------------------------------------------- ドリルダウン ④


def expand_aggregate(plan: dict[str, Any], aggregate_id: str) -> list[str]:
    """集約ノードを展開したときに現れるメンバー node_id を返す (v3 §2.4④)。

    レイアウトの再計算は cc_core.layout 側が担当する。ここは対応表のみ。
    """
    for agg in plan.get("aggregates", []):
        if agg["id"] == aggregate_id:
            return list(agg.get("member_node_ids", []))
    raise KeyError(f"aggregate not found: {aggregate_id}")


# ------------------------------------------------------------- 一括計算


@dataclass
class DetailAnalysis:
    """3 レベル分の選抜結果 (layout 生成の入力)。"""

    communities: dict[str, str]
    importance: dict[str, ImportanceBreakdown]
    visible: dict[str, list[str]]           # level -> node_ids
    aggregates: dict[str, list[Aggregate]]  # level -> aggregates

    def visible_at(self, node_id: str) -> dict[str, bool]:
        return {lv: node_id in self.visible[lv] for lv in LEVEL_ORDER}


def analyze(
    kg: dict[str, Any],
    *,
    weights: dict[str, float] | None = None,
    community_names: dict[str, str] | None = None,
    resolution: float = 1.0,
) -> DetailAnalysis:
    """knowledge_graph から 3 レベル分の可視ノードと集約を決定的に導出する。

    knowledge_graph が community_id を持っている場合でも、Leiden の結果を
    正とする (LLM が付けたコミュニティは粒度が不安定なため)。ただし
    communities[].name は集約ラベルのヒントとして利用する。
    """
    g = build_graph(kg)
    detected = detect_communities(g, resolution=resolution)

    # LLM 由来のコミュニティ名を Leiden コミュニティへ引き継ぐ
    # (Leiden コミュニティ内で最も多い元コミュニティの名前を採用)
    names: dict[str, str] = dict(community_names or {})
    if not names:
        src_names = {c["id"]: c.get("name", c["id"])
                     for c in kg.get("communities", [])}
        node_src = {n["id"]: n.get("community_id") for n in kg.get("nodes", [])}
        tally: dict[str, dict[str, int]] = {}
        for node, cid in detected.items():
            src = node_src.get(node)
            if src in src_names:
                tally.setdefault(cid, {}).setdefault(src, 0)
                tally[cid][src] += 1
        for cid, counts in tally.items():
            best = max(sorted(counts), key=lambda s: counts[s])
            names[cid] = src_names[best]

    importance = score_importance(g, kg, weights=weights)
    total = len(importance)

    visible: dict[str, list[str]] = {}
    aggregates: dict[str, list[Aggregate]] = {}
    for level in LEVEL_ORDER:
        sel = select_nodes(importance, detected, level, total_nodes=total)
        visible[level] = sel
        aggregates[level] = build_aggregates(
            kg, detected, importance, sel, community_names=names,
            max_aggregates=max(1, LEVEL_BANDS[level][1] - len(sel)),
        )

    logger.info(
        "detail analysis nodes=%d communities=%d visible=%s",
        total, len(set(detected.values())),
        {lv: len(visible[lv]) for lv in LEVEL_ORDER},
    )
    return DetailAnalysis(detected, importance, visible, aggregates)
