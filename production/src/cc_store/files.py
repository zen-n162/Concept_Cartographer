"""graphs/ ディレクトリを読むストア (R2b 設計書 §1・裁定 J)。

**読取専用**。パス解決と読み込みは `cc_core.editing` の既存ヘルパを再利用する
(kg_file / plan_file / load_kg / load_plan / load_edits)。同じ規約を 2 か所に
書くと、片方だけ直したときに「CLI では見えるのに検索には出ない」類の齟齬が
出るため。書き込み系 (append_edit / rebuild_session) は**一切 import しない** —
編集の唯一の経路は編集ログへの追記であり、ストアはそこを迂回しない (裁定 J)。

`load_kg` が返すのは原本ではなく **fold 済みの「現在の KG」** である。検索や
コーパスは画面に出ている地図と一致していなければ意味がない (原本のままだと、
消したはずの概念が検索に出続ける)。fold そのものは `editing.apply_edits` の
純関数で、ファイルには何も書かない。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from cc_core import layers_store
from cc_core.editing import (
    EDITS_PREFIX,
    GRAPHS_DIR,
    KG_PREFIX,
    apply_edits,
    edits_file,
    kg_file,
    load_edits,
    load_kg,
    load_plan,
    plan_file,
)
from cc_core.editing import EditTargetNotFound
from cc_core.logging_util import get_logger
from cc_store.base import CORPUS_DIRNAME

if TYPE_CHECKING:  # 循環 import を避ける (index は files を使う側)
    from cc_store.index import SqliteIndex

logger = get_logger("cc_store.files")

# 指紋 (裁定 K) に効くファイル。ここに載っているものが変わったら索引と
# コーパスを丸ごと作り直す。plan を含めるのは重要度スコアの供給源だから。
FINGERPRINT_GLOBS = (
    f"{KG_PREFIX}*.json",
    f"{EDITS_PREFIX}*.jsonl",
    "layout_plan_session_*.json",
)


class SessionStore:
    """graphs/ のファイル読取バックエンド (`base.StoreBackend` の実装)。

    `graphs_dir` を引数に取るのは、テストが tmp_path でまるごと再現できるように
    するため (実データを汚さない)。
    """

    def __init__(self, graphs_dir: str | Path = GRAPHS_DIR) -> None:
        self.graphs_dir = Path(graphs_dir)
        self._index: SqliteIndex | None = None

    # ------------------------------------------------------------ パス
    @property
    def corpus_dir(self) -> Path:
        """派生物 (index.sqlite / corpus_meta.json / summaries.json) の置き場。"""
        return self.graphs_dir / CORPUS_DIRNAME

    def kg_path(self, session: str) -> Path:
        return kg_file(session, graphs_dir=self.graphs_dir)

    def plan_path(self, session: str) -> Path:
        return plan_file(session, graphs_dir=self.graphs_dir)

    def edits_path(self, session: str) -> Path:
        return edits_file(session, graphs_dir=self.graphs_dir)

    # -------------------------------------------------------- セッション
    def list_sessions(self) -> list[str]:
        """知識グラフを持つセッション ID を**新しい順**で返す。

        セッション ID は生成時刻 (`20260807_143804`) なので、文字列の降順が
        そのまま新しい順になる。命名規約から外れた KG (`kg_s1290162_m3.json`
        のような資料単位のもの) はセッションではないので拾わない。
        """
        if not self.graphs_dir.exists():
            return []
        sessions = [p.stem[len(KG_PREFIX):]
                    for p in self.graphs_dir.glob(f"{KG_PREFIX}*.json")]
        return sorted((s for s in sessions if layers_store.SESSION_RE.match(s)),
                      reverse=True)

    def load_kg(self, session: str) -> dict[str, Any]:
        """fold 済みの「現在の KG」を返す (原本 + 編集ログ)。

        `editing.current_kg` と違い plan による関係ポリシーの復元は行わない。
        検索とコミュニティ分割が使うのは概念ラベルと接続関係で、glyph の
        因果/相関の別は結果を変えないため、plan の読み込み分だけ軽くしてある。
        """
        base = load_kg(session, graphs_dir=self.graphs_dir)
        edits = load_edits(session, graphs_dir=self.graphs_dir)
        if not edits:
            return base
        kg, _ = apply_edits(base, edits)
        return kg

    def load_plan(self, session: str) -> dict[str, Any] | None:
        return load_plan(session, graphs_dir=self.graphs_dir)

    def load_layers(self, session: str) -> dict[str, Any] | None:
        """多層分析サイドカー (無ければ None)。

        `layers_store.load` は無い場合も空の骨格を返す仕様なので、ここで
        存在を確かめて None に落とす — 呼ぶ側が「層が無いセッション」と
        「層が空のセッション」を区別できるようにするため。
        """
        if not layers_store.exists(session, graphs_dir=self.graphs_dir):
            return None
        return layers_store.load(session, graphs_dir=self.graphs_dir)

    def importance_map(self, session: str) -> dict[str, float]:
        """plan から node_id -> 重要度 (total) を取り出す。

        `detailed` レベルが縮約前の全量を持つので、そこを最優先で読む
        (`editing._plan_relation_index` と同じ方針)。plan が無い / 旧世代で
        重要度が入っていないセッションは空 = 0.0 扱いになる。
        """
        plan = self.load_plan(session)
        if not plan:
            return {}
        out: dict[str, float] = {}
        levels = plan.get("_level_plans") or {}
        sources = [levels.get(lv, {}).get("nodes") or []
                   for lv in ("detailed", "standard", "overview")]
        sources.append(plan.get("nodes") or [])
        for nodes in sources:
            for node in nodes:
                if node.get("kind") == "aggregate":
                    continue
                nid = str(node.get("id") or "")
                score = node.get("importance")
                if not nid or nid in out or not isinstance(score, dict):
                    continue
                try:
                    out[nid] = float(score.get("total") or 0.0)
                except (TypeError, ValueError):
                    continue
        return out

    # ------------------------------------------------------------ 指紋
    def fingerprint_inputs(self) -> list[tuple[str, int, int]]:
        """指紋の材料: (graphs/ からの相対パス, サイズ, mtime_ns) を名前順で。

        絶対パスを入れないのは、同じファイル群を別ディレクトリへ複製しても
        同じ索引内容になる (= 決定的である) ことを見やすくするため。
        """
        rows: list[tuple[str, int, int]] = []
        if not self.graphs_dir.exists():
            return rows
        for pattern in FINGERPRINT_GLOBS:
            for path in self.graphs_dir.glob(pattern):
                try:
                    st = path.stat()
                except OSError:  # 走査中に消えたファイルは無かったことにする
                    continue
                rows.append((path.name, st.st_size, st.st_mtime_ns))
        return sorted(rows)

    # -------------------------------------------------------------- 近傍
    def neighborhood(self, session: str, node_ids: list[str], *,
                     hops: int = 2, max_nodes: int = 40) -> dict[str, Any]:
        """起点ノードの n-hop 近傍を部分グラフとして返す (R2b 設計書 §2)。

        幅優先で近い順に採り、`max_nodes` で頭打ちにする。同じ距離で溢れる
        ときは **id の昇順**で決定的に切る (同じ問いに毎回同じ材料を渡す)。
        起点そのものは距離 0 として必ず含める。
        """
        kg = self.load_kg(session)
        nodes = {str(n["id"]): n for n in kg.get("nodes", []) if n.get("id")}
        adjacency: dict[str, set[str]] = {nid: set() for nid in nodes}
        for edge in kg.get("edges", []):
            a, b = str(edge.get("from")), str(edge.get("to"))
            if a in adjacency and b in adjacency and a != b:
                adjacency[a].add(b)
                adjacency[b].add(a)

        seeds = [nid for nid in dict.fromkeys(str(n) for n in node_ids) if nid in nodes]
        selected: list[str] = list(seeds[:max_nodes])
        seen = set(selected)
        frontier = list(selected)
        for _ in range(max(0, hops)):
            if len(selected) >= max_nodes:
                break
            nxt: list[str] = []
            for nid in sorted({n for f in frontier for n in adjacency.get(f, ())}):
                if nid in seen:
                    continue
                seen.add(nid)
                nxt.append(nid)
                selected.append(nid)
                if len(selected) >= max_nodes:
                    break
            if not nxt:
                break
            frontier = nxt

        keep = set(selected)
        sub_edges = [e for e in kg.get("edges", [])
                     if str(e.get("from")) in keep and str(e.get("to")) in keep]
        return {
            "session": session,
            "seeds": seeds,
            "nodes": [nodes[nid] for nid in selected],
            "edges": sub_edges,
            "truncated": len(keep) < len(nodes) and len(selected) >= max_nodes,
        }

    # -------------------------------------------------- 検索 / コーパス
    def index(self) -> SqliteIndex:
        """派生キャッシュ (遅延生成)。index は files を使う側なのでここで import。"""
        if self._index is None:
            from cc_store.index import SqliteIndex

            self._index = SqliteIndex(self)
        return self._index

    def search_nodes(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """全セッション横断検索。索引が無い/古ければ自動再構築される (裁定 J)。"""
        return self.index().search_nodes(query, limit=limit)

    def corpus_communities(self) -> dict[str, Any]:
        """コーパスグラフの階層コミュニティ (粗/細)。指紋が合えば再利用する。"""
        from cc_store import corpus

        return corpus.corpus_communities(self)


__all__ = ["SessionStore", "EditTargetNotFound", "FINGERPRINT_GLOBS"]
