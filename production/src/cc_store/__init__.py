"""cc_store — 地図の材料を読むストア層 (R2b 設計書 §1)。

裁定 J: **ファイルが正、SQLite 索引は派生キャッシュ**。書き込みの唯一の経路は
`cc_core.editing` の編集ログ追記であり、このパッケージはそこを迂回しない。

    from cc_store import SessionStore, rebuild_index

    store = SessionStore("graphs")
    rebuild_index(store)                     # CLI --reindex
    store.search_nodes("データ同化", limit=8)  # 索引が古ければ自動再構築
"""

from cc_store.base import CORPUS_DIRNAME, StoreBackend
from cc_store.corpus import (
    CorpusEdge,
    CorpusGraph,
    CorpusNode,
    build_corpus_graph,
    community_fingerprint,
    corpus_communities,
    fingerprint,
    get_summary,
    load_corpus_meta,
    load_summaries,
    node_communities,
    save_summary,
)
from cc_store.files import SessionStore
from cc_store.index import SqliteIndex, rebuild_index

__all__ = [
    "CORPUS_DIRNAME",
    "CorpusEdge",
    "CorpusGraph",
    "CorpusNode",
    "SessionStore",
    "SqliteIndex",
    "StoreBackend",
    "build_corpus_graph",
    "community_fingerprint",
    "corpus_communities",
    "fingerprint",
    "get_summary",
    "load_corpus_meta",
    "load_summaries",
    "node_communities",
    "rebuild_index",
    "save_summary",
]
