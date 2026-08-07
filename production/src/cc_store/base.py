"""ストアの抽象 (R2b 設計書 §1・裁定 J)。

**ファイルが正、SQLite 索引は派生キャッシュ**。この Protocol は「地図の材料を
どこから読むか」だけを定める境界で、書き込みは 1 つも持たない。編集の書き込みは
`cc_core.editing` の追記ログが唯一の経路であり (決定性の聖域)、ストアはそこを
迂回しない。

いま実装があるのは `cc_store.files.SessionStore` (graphs/ のファイル読取) だけ。
Apache AGE (PostgreSQL) バックエンドは Docker/Azure の手配後に足す予定で、
R2b では**署名だけ予約して実装しない** (裁定 J)。予約の目的は 2 つ:

  1. 呼び出し側 (cc_orchestrator.qa など) が `StoreBackend` だけに依存し、
     バックエンドの差し替えで壊れないようにする
  2. AGE 側で「増分更新」を入れるときに、ファイル側の全再計算 (裁定 K) と
     同じ形の戻り値を返せばよいと分かるようにする
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# 索引・コーパスの成果物を置く場所 (graphs/ 配下。gitignore 済み)。
# **派生物しか置かない** — 消しても次回アクセスで自動再構築される。
CORPUS_DIRNAME = "corpus"


@runtime_checkable
class StoreBackend(Protocol):
    """地図の材料を読む口 (R2b 設計書 §1)。読取専用。"""

    # ---------------------------------------------------------- セッション
    def list_sessions(self) -> list[str]:
        """知識グラフを持つセッション ID を**新しい順**で返す。"""
        ...

    def load_kg(self, session: str) -> dict[str, Any]:
        """そのセッションの「現在の知識グラフ」(原本 + 編集ログの畳み込み)。"""
        ...

    def load_plan(self, session: str) -> dict[str, Any] | None:
        """layout_plan (無ければ None)。重要度スコアの供給源。"""
        ...

    def load_layers(self, session: str) -> dict[str, Any] | None:
        """多層分析サイドカー (無ければ None)。出典の document_id を持つ。"""
        ...

    # ---------------------------------------------------------------- 検索
    def search_nodes(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """全セッション横断でノード/関係を検索する (索引は自動再構築)。"""
        ...

    def neighborhood(self, session: str, node_ids: list[str], *,
                     hops: int = 2, max_nodes: int = 40) -> dict[str, Any]:
        """起点ノードの n-hop 近傍を部分グラフとして返す。"""
        ...

    def corpus_communities(self) -> dict[str, Any]:
        """コーパスグラフの階層コミュニティ (粗/細の 2 段)。"""
        ...


# --------------------------------------------------------- AGE 用の予約
#
# 以下は Apache AGE バックエンドを足すときの署名。**実装しない** (裁定 J)。
# ファイル版が全再計算なのに対し、AGE 版は差分適用ができるので、指紋比較の
# 代わりに「前回取り込み済みの session/edit_id まで」を進める形になる。
#
# class AgeBackend(StoreBackend):
#     def __init__(self, dsn: str, graph_name: str = "concept_cartographer") -> None: ...
#
#     def upsert_session(self, session: str, kg: dict) -> dict:
#         """1 セッション分の KG を MERGE で流し込む (差分取り込みの単位)。"""
#
#     def apply_edits(self, session: str, edits: list[dict]) -> dict:
#         """編集ログの未適用分だけを反映する (ファイル版の fold 相当)。
#         **原本の書き換えではない** — グラフ DB 側の投影を進めるだけ。"""
#
#     def cypher(self, query: str, params: dict | None = None) -> list[dict]:
#         """openCypher をそのまま流す口 (近傍探索・経路探索をサーバ側で行う)。"""
#
#     def sync_state(self) -> dict:
#         """{session: last_applied_edit_id} — どこまで取り込んだかの目印。"""
