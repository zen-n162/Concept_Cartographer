"""検索索引 `graphs/corpus/index.sqlite` (R2b 設計書 §1・裁定 J)。

**派生キャッシュであって正ではない**。learned.json と同じ思想で、消しても・
壊れても・古くても、次にアクセスした時点でファイル群から丸ごと作り直される。
だからここには「索引にしか無い情報」を置かない。

sqlite3 は標準ライブラリなので新規依存はゼロ (設計 §1)。

## 日本語検索と FTS5 の話 (実装上いちばん効くところ)

FTS5 の既定トークナイザ (unicode61) は**空白で区切る**ので、「機械学習と同化」は
まるごと 1 トークンになる。この状態で「同化」を引いても当たらない —
日本語では単語検索がほぼ機能しない。そこで **trigram トークナイザ**を使う。
trigram は 3 文字単位で索引を作るので `MATCH` が部分一致になり、LIKE と同じ
意味論になる。ただし**クエリが 3 文字未満だと trigram は何も返せない**
(「同化」は 2 文字なので当たらない)。

そこで照合の定義を 1 つに固定する:

    「正規化ラベルに、正規化クエリが部分文字列として含まれること」

FTS はこの述語を**速く絞り込むための道具**としてだけ使い、最後は必ず同じ
LIKE 述語で確定させる。結果として:

  - クエリ 3 文字以上 & FTS5 あり  → FTS で候補を絞り、LIKE で確定
  - それ以外 (2 文字以下 / FTS5 無し) → LIKE のみ

どちらの経路でも**結果は同一**になる (設計 §3「FTS と LIKE フォールバック同値」)。
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cc_core.editing import normalize_label
from cc_core.logging_util import get_logger
from cc_store import corpus

if TYPE_CHECKING:
    from cc_store.files import SessionStore

logger = get_logger("cc_store.index")

INDEX_FILE = "index.sqlite"
# 索引のスキーマ世代。上げると指紋が一致しても作り直す
INDEX_VERSION = 1

# trigram トークナイザが扱える最小クエリ長。これ未満は LIKE 経路へ回す
TRIGRAM_MIN = 3

SCHEMA = """
CREATE TABLE nodes (
    label_norm       TEXT NOT NULL,
    label            TEXT NOT NULL,
    session          TEXT NOT NULL,
    node_id          TEXT NOT NULL,
    community_id     TEXT,
    corpus_community TEXT,
    importance       REAL,
    onto_class       TEXT
);
CREATE TABLE edges (
    session       TEXT NOT NULL,
    edge_id       TEXT NOT NULL,
    from_norm     TEXT NOT NULL,
    to_norm       TEXT NOT NULL,
    glyph         TEXT,
    label         TEXT,
    evidence      TEXT,
    -- 設計 §1 の表に対する追加 2 列。LIKE は生の文字列をそのまま比べるので、
    -- NFKC + casefold 済みの列が無いと「ＡＩ」と「ai」が別物になり、設計 §3 の
    -- 「日本語正規化検索」が満たせない。表示は label/evidence、照合はこちら
    label_norm    TEXT,
    evidence_norm TEXT
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX idx_nodes_label ON nodes(label_norm);
CREATE INDEX idx_nodes_session ON nodes(session);
CREATE INDEX idx_edges_from ON edges(from_norm);
CREATE INDEX idx_edges_to ON edges(to_norm);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE node_fts USING fts5(label, tokenize='trigram');
CREATE VIRTUAL TABLE edge_fts USING fts5(label, evidence, tokenize='trigram');
"""


def _like_pattern(query: str) -> str:
    """部分一致パターン。`%` `_` `\\` はクエリ側の文字として扱う。

    ユーザーが「50%」で検索したときにワイルドカードとして解釈されると、
    無関係なものが全部当たってしまう。ESCAPE 付き LIKE で literal にする。
    """
    escaped = (query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))
    return f"%{escaped}%"


def _fts_query(query: str) -> str:
    """FTS5 の文字列リテラル (中の `"` は 2 個重ねて逃がす)。

    クエリを裸で渡すと `AND` `NEAR` `*` などが FTS 構文として解釈され、
    「and」を検索した瞬間に構文エラーになる。常に引用符で囲んで literal に。
    """
    return '"' + query.replace('"', '""') + '"'


class SqliteIndex:
    """`graphs/corpus/index.sqlite` の読み書き (派生キャッシュ)。

    `use_fts=False` で FTS5 が無いビルドを模せる (設計 §3 の同値テスト用)。
    """

    def __init__(self, store: SessionStore, *, use_fts: bool = True) -> None:
        self.store = store
        self.path = store.corpus_dir / INDEX_FILE
        self._want_fts = use_fts
        self._conn: sqlite3.Connection | None = None
        self.has_fts = False

    # ------------------------------------------------------------ 接続
    def _connect(self, path: Path | str) -> sqlite3.Connection:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        return conn

    def _fts_available(self) -> bool:
        """このビルドの sqlite3 が fts5(trigram) を作れるか。

        判定は**インメモリの別接続**で行う。索引ファイル側で試すと、確認の
        ためだけに派生キャッシュへ書き込むことになり (読取のつもりの操作が
        ファイルを変える)、読み取り専用で開けない環境で落ちる。
        """
        if not self._want_fts:
            return False
        probe = sqlite3.connect(":memory:")
        try:
            probe.execute("CREATE VIRTUAL TABLE p USING fts5(x, tokenize='trigram')")
            return True
        except sqlite3.Error:
            logger.info("fts5(trigram) unavailable; using LIKE search")
            return False
        finally:
            probe.close()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------ 自動再構築
    def _read_meta(self, conn: sqlite3.Connection) -> dict[str, str]:
        try:
            return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
        except sqlite3.Error:
            return {}

    def connection(self) -> sqlite3.Connection:
        """索引を開く。無い / 古い / 壊れているときは**自動で作り直す** (裁定 J)。

        「壊れていたら例外」ではなく「壊れていたら作り直す」が正しい。派生物の
        破損でユーザーの操作が止まる理由が無いため。
        """
        if self._conn is not None:
            return self._conn

        want = corpus.fingerprint(self.store)
        if self.path.exists():
            try:
                conn = self._connect(self.path)
                meta = self._read_meta(conn)
                if (meta.get("fingerprint") == want
                        and meta.get("version") == str(INDEX_VERSION)):
                    # ファイル側に FTS 表があり、かつ今の sqlite3 が読めるときだけ使う
                    self.has_fts = meta.get("fts") == "1" and self._fts_available()
                    self._conn = conn
                    return conn
                conn.close()
                logger.info("index stale (fingerprint changed); rebuilding")
            except sqlite3.DatabaseError:
                logger.warning("index unreadable; rebuilding from files")
        else:
            logger.info("index missing; building from files")
        self.rebuild()
        assert self._conn is not None  # rebuild が必ず張り直す
        return self._conn

    # -------------------------------------------------------- 構築 (§1)
    def rebuild(self) -> dict[str, int]:
        """全セッションを走査して索引を作り直す (設計 §1 `rebuild_index`)。

        一時ファイルへ書いてから差し替えるので、途中で落ちても**半端な索引が
        残らない** (次回また作り直せる状態のまま)。同じファイル群からは
        `built_at` 以外つねに同じ内容になる (決定性)。
        """
        self.close()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".sqlite.tmp")
        tmp.unlink(missing_ok=True)

        graph = corpus.build_corpus_graph(self.store)
        meta = corpus.corpus_communities(self.store, force=True, graph=graph)
        by_label = corpus.node_communities(meta, corpus.DEFAULT_LEVEL)

        conn = self._connect(tmp)
        self.has_fts = self._fts_available()
        conn.executescript(SCHEMA)
        if self.has_fts:
            conn.executescript(FTS_SCHEMA)

        # rowid は**明示的に振る**。検索は FTS の rowid で本体表を引くので、
        # 2 つの表の rowid がずれると「当たったのと違う行」が黙って返る。
        # 暗黙の採番 (挿入順 = 1..N) に頼らず、同じ番号を両方へ書き込む。
        node_rows: list[tuple[Any, ...]] = []
        for key in sorted(graph.nodes):
            node = graph.nodes[key]
            for src in sorted(node.sources,
                              key=lambda s: (s["session"], s["node_id"])):
                node_rows.append((
                    len(node_rows) + 1,
                    key, src["label"], src["session"], src["node_id"],
                    src.get("community_id") or "", by_label.get(key, ""),
                    float(src.get("importance") or 0.0), src.get("onto_class") or "",
                ))
        conn.executemany(
            "INSERT INTO nodes (rowid, label_norm, label, session, node_id,"
            " community_id, corpus_community, importance, onto_class)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            node_rows)

        edge_rows: list[tuple[Any, ...]] = []
        for edge in graph.edges:
            for src in sorted(edge.sources, key=lambda s: (s["session"], s["edge_id"])):
                label = src.get("label") or ""
                evidence = src.get("evidence") or ""
                edge_rows.append((
                    len(edge_rows) + 1,
                    src["session"], src["edge_id"], edge.from_norm, edge.to_norm,
                    edge.glyph, label, evidence,
                    normalize_label(label), normalize_label(evidence),
                ))
        conn.executemany(
            "INSERT INTO edges (rowid, session, edge_id, from_norm, to_norm, glyph,"
            " label, evidence, label_norm, evidence_norm) VALUES (?,?,?,?,?,?,?,?,?,?)",
            edge_rows)

        if self.has_fts:
            # FTS へは**正規化済みの文字列**を入れる。LIKE が見る列と同じものを
            # 見せないと、2 つの経路が別の答えを出しうるため
            conn.executemany(
                "INSERT INTO node_fts (rowid, label) VALUES (?, ?)",
                [(row[0], row[1]) for row in node_rows])
            conn.executemany(
                "INSERT INTO edge_fts (rowid, label, evidence) VALUES (?, ?, ?)",
                [(row[0], row[8], row[9]) for row in edge_rows])

        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [("version", str(INDEX_VERSION)),
             ("fingerprint", meta["fingerprint"]),
             ("built_at", dt.datetime.now().isoformat(timespec="seconds")),
             ("sessions", str(len(graph.sessions))),
             ("corpus_nodes", str(len(graph.nodes))),
             ("corpus_edges", str(len(graph.edges))),
             ("fts", "1" if self.has_fts else "0")])
        conn.commit()
        conn.close()

        tmp.replace(self.path)
        self._conn = self._connect(self.path)
        counts = {"sessions": len(graph.sessions), "nodes": len(node_rows),
                  "edges": len(edge_rows), "corpus_nodes": len(graph.nodes),
                  "corpus_edges": len(graph.edges),
                  "communities": len((meta.get("levels") or {}).get(
                      corpus.DEFAULT_LEVEL) or {})}
        logger.info("index rebuilt %s", counts)
        return counts

    # -------------------------------------------------------- 検索 (§1)
    def _use_fts(self, query: str) -> bool:
        """この検索で FTS 経路を使うか (trigram は 3 文字未満を索引できない)。"""
        return self.has_fts and len(query) >= TRIGRAM_MIN

    def _candidates(self, fts_table: str, columns: tuple[str, ...],
                    query: str) -> str:
        """WHERE 句を組む。FTS が使えるときは MATCH で絞ってから LIKE で確定。

        LIKE を必ず AND するのが肝で、これが 2 経路の同値を**構造的に**保証する
        (trigram は部分一致なので LIKE の上位集合であり、AND で差が消える)。
        """
        like = " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in columns)
        if self._use_fts(query):
            return (f"rowid IN (SELECT rowid FROM {fts_table}"
                    f" WHERE {fts_table} MATCH ?) AND ({like})")
        return f"({like})"

    def _params(self, columns: tuple[str, ...], query: str) -> list[Any]:
        pattern = _like_pattern(query)
        if self._use_fts(query):
            return [_fts_query(query)] + [pattern] * len(columns)
        return [pattern] * len(columns)

    def search_nodes(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """全セッション横断でノードと関係を検索する (設計 §1)。

        並び順は「完全一致 → セッション新しい順 → 重要度降順」。設計は
        「セッション新しい順 + importance」だが、**ラベルがクエリそのもの**の
        概念を、たまたま新しいセッションにある部分一致より下に出すのは
        検索として不自然なので、完全一致だけ先頭に引き上げている。
        以降は同点を id で割って完全に決定的にする。

        戻り値は node / edge が混ざった 1 本のリスト (`kind` で区別)。
        R2b-2 の QA が「上位 N 件の出自セッション」を辿れるよう、
        session と id を必ず入れる。
        """
        norm = normalize_label(query)
        if not norm:
            return []
        conn = self.connection()

        rows = conn.execute(
            "SELECT label_norm, label, session, node_id, community_id,"
            " corpus_community, importance, onto_class,"
            " CASE WHEN label_norm = ? THEN 1 ELSE 0 END AS exact"
            f" FROM nodes WHERE {self._candidates('node_fts', ('label_norm',), norm)}"
            " ORDER BY exact DESC, session DESC, importance DESC, label_norm, node_id"
            " LIMIT ?",
            [norm, *self._params(("label_norm",), norm), max(1, limit)],
        ).fetchall()
        hits: list[dict[str, Any]] = [
            {"kind": "node", "label": r["label"], "label_norm": r["label_norm"],
             "session": r["session"], "node_id": r["node_id"],
             "community_id": r["community_id"],
             "corpus_community": r["corpus_community"],
             "importance": r["importance"], "onto_class": r["onto_class"],
             "exact": bool(r["exact"])}
            for r in rows]

        # 残り枠を関係で埋める。概念が先なのは、問いの入口はほぼ常に概念名だから
        room = max(0, limit - len(hits))
        if room:
            cols = ("label_norm", "evidence_norm")
            erows = conn.execute(
                "SELECT session, edge_id, from_norm, to_norm, glyph, label, evidence"
                f" FROM edges WHERE {self._candidates('edge_fts', cols, norm)}"
                " ORDER BY session DESC, from_norm, to_norm, edge_id LIMIT ?",
                [*self._params(cols, norm), room],
            ).fetchall()
            hits.extend(
                {"kind": "edge", "label": r["label"], "session": r["session"],
                 "edge_id": r["edge_id"], "from_norm": r["from_norm"],
                 "to_norm": r["to_norm"], "glyph": r["glyph"],
                 "evidence": r["evidence"], "exact": False}
                for r in erows)
        return hits

    # ---------------------------------------------------------- 補助
    def stats(self) -> dict[str, Any]:
        """索引の中身の要約 (CLI 表示・テスト用)。"""
        conn = self.connection()
        meta = self._read_meta(conn)
        return {
            "path": str(self.path),
            "nodes": conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
            "edges": conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
            "sessions": int(meta.get("sessions") or 0),
            "corpus_nodes": int(meta.get("corpus_nodes") or 0),
            "corpus_edges": int(meta.get("corpus_edges") or 0),
            "fingerprint": meta.get("fingerprint", ""),
            "built_at": meta.get("built_at", ""),
            "fts": meta.get("fts") == "1",
        }


def rebuild_index(store: SessionStore, *, use_fts: bool = True) -> dict[str, int]:
    """索引を作り直して件数を返す (設計 §1 / CLI `--reindex`)。"""
    return SqliteIndex(store, use_fts=use_fts).rebuild()
