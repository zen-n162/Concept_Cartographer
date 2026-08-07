"""R2b-1 ストア/コーパスの回帰テスト — R2b 設計書 §3。

主眼は 4 つ:

  - **索引は派生キャッシュである** (裁定 J)。消しても・壊れても・古くても、
    次のアクセスで作り直せる。作り直しは決定的 (built_at 以外は同じ内容)
  - **FTS と LIKE が同値** (§3)。日本語は trigram でも 3 文字未満を索引でき
    ないので、両経路が必ず同じ答えを出すことを機械で押さえる
  - **コーパスの併合規則** (裁定 K)。正規化ラベルで 1 点に畳み、出自を残し、
    エッジの出現回数を weight にする
  - **editing.py は聖域** (受け入れ基準 5)。cc_store は書き込み API を
    import せず、索引を作っても graphs/ の 1 バイトも変えない

各テストは tmp_path に graphs/ を作るので production/graphs を汚さない。
"""

from __future__ import annotations

import ast
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from cc_core.editing import append_edit, normalize_label
from cc_store import SessionStore, corpus
from cc_store.base import StoreBackend
from cc_store.index import SqliteIndex, rebuild_index

CC_STORE = Path(__file__).resolve().parents[1] / "src" / "cc_store"

# 索引が触れてよい editing の API = **読取だけ**。fold (apply_edits) は
# ファイルを書かない純関数なので読取側に含める
ALLOWED_EDITING_IMPORTS = {
    "EDITS_PREFIX", "GRAPHS_DIR", "KG_PREFIX", "PLAN_PREFIX",
    "EditError", "EditTargetNotFound",
    "apply_edits", "edits_file", "kg_file", "plan_file",
    "load_edits", "load_kg", "load_plan", "normalize_label",
}
# 触れたら聖域侵犯 (編集ログへの追記 = 決定性の唯一の入口)
FORBIDDEN_EDITING_API = ("append_edit", "append_revert", "rebuild_session",
                         "current_kg", "_record_evaluation")


# --------------------------------------------------------------- 補助


def write_session(graphs: Path, session: str, nodes: list[dict],
                  edges: list[dict], *, plan_importance: dict | None = None) -> None:
    """kg_session_{s}.json (+ 任意で重要度つき plan) を書く。"""
    graphs.mkdir(parents=True, exist_ok=True)
    kg = {
        "graph_version": "kg_r2b_test",
        "nodes": nodes,
        "edges": edges,
        "communities": [{"id": "comm_001", "name": "テーマ", "is_gap": False}],
    }
    (graphs / f"kg_session_{session}.json").write_text(
        json.dumps(kg, ensure_ascii=False), encoding="utf-8")
    if plan_importance:
        plan = {"detail_level": "standard",
                "nodes": [{"id": nid, "kind": "concept",
                           "importance": {"total": score}}
                          for nid, score in plan_importance.items()]}
        (graphs / f"layout_plan_session_{session}.json").write_text(
            json.dumps(plan, ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def graphs(tmp_path: Path) -> Path:
    """2 セッション。「機械学習」が両方に出る (正規化ラベルで併合される)。"""
    root = tmp_path / "graphs"
    write_session(
        root, "20260101_000000",
        nodes=[{"id": "c001", "label": "機械学習", "community_id": "comm_001"},
               {"id": "c002", "label": "衛星観測", "community_id": "comm_001"},
               {"id": "c003", "label": "ＡＩモデル", "community_id": "comm_001"}],
        edges=[{"id": "r001", "from": "c001", "to": "c002", "glyph": "arrow",
                "label": "精度を上げる",
                "evidence_span": [{"document_id": "a.pdf",
                                   "surface": "衛星観測の精度が向上した"}]},
               {"id": "r002", "from": "c003", "to": "c001", "glyph": "wave",
                "label": "土台になる"}],
        plan_importance={"c001": 0.9, "c002": 0.4, "c003": 0.2})
    write_session(
        root, "20260202_000000",
        nodes=[{"id": "n01", "label": "機械学習 ", "community_id": "comm_001",
                "onto_class": "bfo:Process"},
               {"id": "n02", "label": "データ同化", "community_id": "comm_001"}],
        edges=[{"id": "e01", "from": "n01", "to": "n02", "glyph": "arrow",
                "label": "精度を上げる"}],
        plan_importance={"n01": 0.5, "n02": 0.8})
    # セッション命名規約から外れた KG (資料単位)。走査対象にしない
    (root / "kg_s1290162_m3.json").write_text(
        json.dumps({"nodes": [{"id": "x", "label": "対象外"}], "edges": []}),
        encoding="utf-8")
    return root


@pytest.fixture()
def store(graphs: Path) -> SessionStore:
    return SessionStore(graphs)


def index_rows(path: Path) -> tuple[list, list, dict]:
    """索引の中身 (built_at を除く) を比較可能な形で取り出す。"""
    conn = sqlite3.connect(str(path))
    try:
        nodes = [tuple(r) for r in conn.execute("SELECT * FROM nodes ORDER BY rowid")]
        edges = [tuple(r) for r in conn.execute("SELECT * FROM edges ORDER BY rowid")]
        meta = {k: v for k, v in conn.execute("SELECT key, value FROM meta")}
    finally:
        conn.close()
    meta.pop("built_at", None)
    return nodes, edges, meta


def snapshot(graphs: Path) -> dict[str, tuple[bytes, int]]:
    """graphs/ 直下の実ファイル (派生物 corpus/ を除く) の内容と mtime。"""
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(graphs.iterdir()) if p.is_file()}


# ------------------------------------------------------ セッション走査


def test_list_sessions_newest_first(store: SessionStore) -> None:
    """セッションは新しい順。規約外の kg ファイルは拾わない。"""
    assert store.list_sessions() == ["20260202_000000", "20260101_000000"]


def test_load_kg_folds_edits(graphs: Path, store: SessionStore) -> None:
    """ストアが返すのは原本ではなく **fold 済みの現在の KG**。

    消したはずの概念が検索に出続けないことが要点。
    """
    append_edit("20260101_000000", {"op": "delete_node", "target": "c002"},
                graphs_dir=graphs, eval_log=None)
    labels = {n["label"] for n in store.load_kg("20260101_000000")["nodes"]}
    assert "衛星観測" not in labels
    assert "機械学習" in labels


# ---------------------------------------------------------- 索引の構築


def test_rebuild_is_deterministic(store: SessionStore) -> None:
    """同じファイル群からは同じ索引 (built_at を除く)。"""
    rebuild_index(store)
    first = index_rows(store.corpus_dir / "index.sqlite")
    rebuild_index(store)
    assert index_rows(store.corpus_dir / "index.sqlite") == first


def test_rebuild_is_deterministic_across_directories(
        graphs: Path, tmp_path: Path) -> None:
    """別ディレクトリへ複製しても同じ内容 (絶対パスが混ざっていない)。"""
    other = tmp_path / "copy" / "graphs"
    other.mkdir(parents=True)
    for path in graphs.iterdir():
        shutil.copy2(path, other / path.name)

    rebuild_index(SessionStore(graphs))
    rebuild_index(SessionStore(other))
    a = index_rows(graphs / "corpus" / "index.sqlite")
    b = index_rows(other / "corpus" / "index.sqlite")
    assert a[0] == b[0] and a[1] == b[1]


def test_missing_index_is_rebuilt_on_access(store: SessionStore) -> None:
    """索引を削除しても次のアクセスで作り直される (受け入れ基準 4)。"""
    rebuild_index(store)
    path = store.corpus_dir / "index.sqlite"
    path.unlink()
    assert not path.exists()

    assert store.search_nodes("機械学習")          # 例外にならず結果が返る
    assert path.exists()


def test_stale_fingerprint_triggers_rebuild(graphs: Path) -> None:
    """指紋が変われば自動再構築 (裁定 K)。--reindex を打たなくても新セッションが出る。"""
    store = SessionStore(graphs)
    rebuild_index(store)
    before = corpus.fingerprint(store)
    assert not store.search_nodes("量子計算")

    write_session(graphs, "20260303_000000",
                  nodes=[{"id": "q1", "label": "量子計算", "community_id": "comm_001"}],
                  edges=[])
    assert corpus.fingerprint(store) != before

    fresh = SessionStore(graphs)                   # 索引を開き直す
    hits = fresh.search_nodes("量子計算")
    assert [h["session"] for h in hits] == ["20260303_000000"]


def test_corrupt_index_is_rebuilt(store: SessionStore) -> None:
    """壊れた索引は例外ではなく作り直しで復旧する。"""
    rebuild_index(store)
    path = store.corpus_dir / "index.sqlite"
    path.write_bytes(b"not a database at all")

    assert SessionStore(store.graphs_dir).search_nodes("機械学習")


def test_rebuild_counts(store: SessionStore) -> None:
    """--reindex が表示する件数の内訳 (行数と併合後の数は別物)。"""
    counts = rebuild_index(store)
    assert counts["sessions"] == 2
    assert counts["nodes"] == 5           # 出自 1 件 = 1 行 (機械学習は 2 行)
    assert counts["corpus_nodes"] == 4    # 併合後 (機械学習 / 衛星観測 / AI / 同化)
    assert counts["edges"] == 3
    assert counts["corpus_edges"] == 3


# ------------------------------------------------------------ 検索


@pytest.mark.parametrize(
    "query", ["機械学習", "学習", "衛星", "ai", "ＡＩ", "データ同化", "同化",
              "せ", "精度", "なにもない", "50%", "_"])
def test_fts_and_like_return_the_same(store: SessionStore, query: str) -> None:
    """FTS 経路と LIKE フォールバックは同値 (設計 §3)。

    trigram は 3 文字未満のクエリを索引できないので、短い語では FTS 側が
    自動的に LIKE へ落ちる。ワイルドカード (`%` `_`) が literal に扱われる
    ことも同じ経路で確かめる。
    """
    rebuild_index(store)
    with_fts = SqliteIndex(store, use_fts=True)
    without = SqliteIndex(store, use_fts=False)
    assert with_fts.search_nodes(query, limit=10) == without.search_nodes(query, limit=10)


def test_search_normalizes_width_and_case(store: SessionStore) -> None:
    """NFKC + casefold で全角/半角・大小を吸収する (設計 §3)。"""
    rebuild_index(store)
    hits = store.search_nodes("AIモデル")
    assert [h["label"] for h in hits if h["kind"] == "node"] == ["ＡＩモデル"]
    assert hits[0]["exact"] is True                # 正規化後は完全一致


def test_search_ranks_exact_then_newest_session(store: SessionStore) -> None:
    """完全一致が先、その後はセッションの新しい順 (設計 §1)。"""
    rebuild_index(store)
    hits = [h for h in store.search_nodes("機械学習", limit=8) if h["kind"] == "node"]
    assert [h["session"] for h in hits] == ["20260202_000000", "20260101_000000"]
    assert all(h["exact"] for h in hits)


def test_search_respects_limit(store: SessionStore) -> None:
    rebuild_index(store)
    assert len(store.search_nodes("学", limit=8)) == 2      # 出自 2 セッション分
    assert len(store.search_nodes("学", limit=1)) == 1


def test_search_matches_edge_evidence(store: SessionStore) -> None:
    """関係は根拠文 (surface) でも当たる — 出典から辿る入口になる。"""
    rebuild_index(store)
    hits = store.search_nodes("精度が向上", limit=8)
    assert [h["kind"] for h in hits] == ["edge"]
    assert hits[0]["edge_id"] == "r001"
    assert "衛星観測の精度が向上した" in hits[0]["evidence"]


def test_search_carries_corpus_community(store: SessionStore) -> None:
    """検索結果はコーパス側の島 id を持つ (R2b-2 の global 経路が使う)。"""
    rebuild_index(store)
    hit = store.search_nodes("機械学習")[0]
    assert hit["corpus_community"].startswith("corpus_fine_")
    assert hit["onto_class"] == "bfo:Process"      # 出自の onto_class が乗る


def test_search_ignores_blank_query(store: SessionStore) -> None:
    rebuild_index(store)
    assert store.search_nodes("   ") == []


# ------------------------------------------------------ コーパス併合


def test_corpus_merges_by_normalized_label(store: SessionStore) -> None:
    """正規化ラベルが同じものは 1 点に畳み、出自を全部残す (裁定 K)。"""
    graph = corpus.build_corpus_graph(store)
    node = graph.nodes[normalize_label("機械学習")]
    assert [s["session"] for s in node.sources] == ["20260202_000000", "20260101_000000"]
    assert {s["node_id"] for s in node.sources} == {"n01", "c001"}
    assert node.importance == pytest.approx(0.9)   # 出自のうち最大


def test_corpus_representative_label_is_newest(store: SessionStore) -> None:
    """代表ラベルは最新セッションの表記 (末尾空白は正規化キーだけ吸収)。"""
    graph = corpus.build_corpus_graph(store)
    assert graph.nodes[normalize_label("機械学習")].label == "機械学習 "


def test_corpus_edge_weight_counts_occurrences(store: SessionStore) -> None:
    """同じ (from, to, glyph) は 1 本に畳み、出現回数を weight にする。"""
    graph = corpus.build_corpus_graph(store)
    weights = {(e.from_norm, e.to_norm, e.glyph): e.weight for e in graph.edges}
    assert weights[("機械学習", "衛星観測", "arrow")] == 1
    assert weights[("機械学習", "データ同化", "arrow")] == 1
    assert sum(weights.values()) == 3


def test_corpus_merges_edges_across_sessions(graphs: Path) -> None:
    """別セッションの同じ関係は 1 本になり weight が 2 になる。"""
    write_session(graphs, "20260303_000000",
                  nodes=[{"id": "z1", "label": "機械学習"},
                         {"id": "z2", "label": "衛星観測"}],
                  edges=[{"id": "z9", "from": "z1", "to": "z2", "glyph": "arrow",
                          "label": "精度を上げる"}])
    graph = corpus.build_corpus_graph(SessionStore(graphs))
    edge = next(e for e in graph.edges if e.key == ("機械学習", "衛星観測", "arrow"))
    assert edge.weight == 2
    assert [s["session"] for s in edge.sources] == ["20260303_000000", "20260101_000000"]


def test_corpus_reflects_edits(graphs: Path, store: SessionStore) -> None:
    """編集ログで改名した概念は、改名後のラベルで併合される。

    これが効かないと「直したのに検索は古い名前のまま」になる。
    """
    append_edit("20260101_000000",
                {"op": "rename_node", "target": "c002",
                 "payload": {"label": "データ同化"}},
                graphs_dir=graphs, eval_log=None)
    graph = corpus.build_corpus_graph(store)
    assert "衛星観測" not in graph.nodes
    assert len(graph.nodes["データ同化"].sources) == 2


# --------------------------------------------------- 階層コミュニティ


def test_two_level_communities(store: SessionStore) -> None:
    """粗 (0.4) / 細 (1.0) の 2 階層。粗いほうが島は多くならない (設計 §1)。"""
    meta = store.corpus_communities()
    coarse, fine = meta["levels"]["coarse"], meta["levels"]["fine"]
    assert len(coarse) <= len(fine)

    everything = sorted(corpus.build_corpus_graph(store).nodes)
    for level, groups in (("coarse", coarse), ("fine", fine)):
        members = [m for names in groups.values() for m in names]
        assert sorted(members) == everything      # 全ノードをちょうど 1 回ずつ被覆
        assert all(cid.startswith(f"corpus_{level}_") for cid in groups)


def test_corpus_meta_is_reused_until_fingerprint_changes(
        graphs: Path, store: SessionStore) -> None:
    """指紋が同じならファイルを読み直すだけ (再計算しない)。変われば作り直す。"""
    first = store.corpus_communities()
    assert store.corpus_communities()["built_at"] == first["built_at"]

    write_session(graphs, "20260303_000000",
                  nodes=[{"id": "q1", "label": "量子計算"}], edges=[])
    second = SessionStore(graphs).corpus_communities()
    assert second["fingerprint"] != first["fingerprint"]
    assert "20260303_000000" in second["sessions"]


def test_corpus_communities_are_deterministic(graphs: Path) -> None:
    """seed 固定なので、作り直しても同じ分割になる。"""
    a = SessionStore(graphs).corpus_communities()["levels"]
    b = corpus.corpus_communities(SessionStore(graphs), force=True)["levels"]
    assert a == b


def test_node_communities_lookup(store: SessionStore) -> None:
    mapping = corpus.node_communities(store.corpus_communities(), "fine")
    assert mapping["機械学習"].startswith("corpus_fine_")


# ------------------------------------------------- 要約キャッシュ (枠)


def test_summary_cache_roundtrip(store: SessionStore) -> None:
    """要約は**コミュニティ指紋**で引く (裁定 L)。R2b-1 では入れ物だけ。"""
    members = ["機械学習", "データ同化"]
    assert corpus.get_summary(store, members) is None

    key = corpus.save_summary(store, members, "2 つの概念の島", model="gpt-5.6-sol")
    hit = corpus.get_summary(store, list(reversed(members)))   # 並び順に依存しない
    assert hit and hit["text"] == "2 つの概念の島" and hit["model"] == "gpt-5.6-sol"
    assert key == corpus.community_fingerprint(members)
    assert corpus.get_summary(store, members + ["新概念"]) is None  # 変われば別の鍵


# -------------------------------------------------------------- 近傍


def test_neighborhood_expands_by_hops(store: SessionStore) -> None:
    """2-hop 近傍 (R2b-2 の local 経路が使う材料)。"""
    one = store.neighborhood("20260101_000000", ["c002"], hops=1)
    assert {n["id"] for n in one["nodes"]} == {"c002", "c001"}

    two = store.neighborhood("20260101_000000", ["c002"], hops=2)
    assert {n["id"] for n in two["nodes"]} == {"c002", "c001", "c003"}
    assert {e["id"] for e in two["edges"]} == {"r001", "r002"}


def test_neighborhood_caps_node_count(store: SessionStore) -> None:
    capped = store.neighborhood("20260101_000000", ["c002"], hops=2, max_nodes=2)
    assert len(capped["nodes"]) == 2
    assert capped["truncated"] is True


# ---------------------------------------------------- 聖域検査 (§3・基準 5)


def test_cc_store_imports_only_read_api_from_editing() -> None:
    """cc_store は editing の**書き込み系を import しない** (裁定 J)。

    編集の唯一の入口は編集ログへの追記であり、索引側から原本や plan を
    書き換える経路を作らせない。検査は AST で行う — 文字列で探すと docstring の
    説明文 (「append_edit は import しない」) に当たってしまい、**説明を書くほど
    テストが落ちる**という逆さまの圧力がかかるため。
    """
    for path in sorted(CC_STORE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        used: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "cc_core.editing":
                names = {alias.name for alias in node.names}
                assert names <= ALLOWED_EDITING_IMPORTS, f"{path.name}: {names}"
            if isinstance(node, ast.Import):
                assert all(a.name != "cc_core.editing" for a in node.names), \
                    f"{path.name}: モジュール丸ごとの import は書き込み API を隠す"
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                used |= {a.asname or a.name for a in node.names}
        touched = used & set(FORBIDDEN_EDITING_API)
        assert not touched, f"{path.name} が {touched} に触れている"


def test_editing_does_not_depend_on_store() -> None:
    """依存の向きは cc_store → cc_core の一方向だけ (循環させない)。"""
    editing = (CC_STORE.parent / "cc_core" / "editing.py").read_text(encoding="utf-8")
    assert "cc_store" not in editing


def test_rebuild_does_not_write_into_graphs(graphs: Path,
                                            store: SessionStore) -> None:
    """索引の構築は graphs/ の実ファイルを 1 バイトも変えない (受け入れ基準 5)。

    派生物は graphs/corpus/ の中だけに作る。
    """
    before = snapshot(graphs)
    rebuild_index(store)
    store.search_nodes("機械学習")
    store.corpus_communities()

    assert snapshot(graphs) == before
    assert {p.name for p in (graphs / "corpus").iterdir()} == {
        "index.sqlite", "corpus_meta.json"}


def test_session_store_satisfies_protocol(store: SessionStore) -> None:
    """SessionStore は StoreBackend を満たす (将来の AGE 版と差し替え可能)。"""
    assert isinstance(store, StoreBackend)


# ------------------------------------------------------------- CLI


def test_cli_reindex_and_search(graphs: Path, capsys) -> None:
    """`--reindex` は件数を、`--search` は当たりを表示する (設計 §1)。"""
    from cc_orchestrator import chat

    chat._print_reindex(graphs)
    out = capsys.readouterr().out
    assert "索引を再構築しました" in out and "セッション 2" in out

    chat._print_search("機械学習", graphs)
    out = capsys.readouterr().out
    assert "機械学習" in out and "20260202_000000" in out and "完全一致" in out

    chat._print_search("存在しない概念", graphs)
    assert "当たる概念・関係はありません" in capsys.readouterr().out
