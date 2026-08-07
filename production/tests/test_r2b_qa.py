"""R2b-2 Routing 拡張と QA 経路の回帰テスト — R2b 設計書 §3。

主眼は 5 つ:

  - **既存 3 経路の判定が 1 件も変わらない** (裁定 N)。新しい cue は
    MAP/BASIC の後ろ・VECTOR の前にしか入らない
  - **経路ディスパッチ表**が「map 以外 → 直答」の if を置き換えたこと。
    basic / vector のプロンプトは R1 のまま (受け入れ基準 1)
  - **出典が実在すること** (裁定 M)。LLM が付けた引用は索引に無ければ捨てる。
    引用が無ければ「何を見たか」(起点) を出す
  - **要約キャッシュ命中で LLM 0 call** (裁定 L)。2 回目の global が安い
  - **offline でも落ちない**。local は「LLM なし要約」、global は案内文

各テストは tmp_path に graphs/ を作るので production/graphs を汚さない。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from cc_orchestrator import analysis, pipeline, qa
from cc_orchestrator.chat import _print_summary
from cc_orchestrator.routing import ANSWER_ROUTES, ROUTES, route
from cc_store import SessionStore, corpus

PRODUCTION = Path(__file__).resolve().parents[1]
STATIC = PRODUCTION / "src" / "cc_web" / "static"


# --------------------------------------------------------------- 補助


def write_session(graphs: Path, session: str, nodes: list[dict],
                  edges: list[dict]) -> None:
    graphs.mkdir(parents=True, exist_ok=True)
    (graphs / f"kg_session_{session}.json").write_text(
        json.dumps({"graph_version": "kg_qa_test", "nodes": nodes, "edges": edges,
                    "communities": [{"id": "comm_001", "name": "テーマ",
                                     "is_gap": False}]}, ensure_ascii=False),
        encoding="utf-8")


@pytest.fixture()
def graphs(tmp_path: Path) -> Path:
    """3 セッション。SuperPCA が 2 つに跨り、月面の話は独立した島になる。"""
    root = tmp_path / "graphs"
    write_session(
        root, "20260101_000000",
        nodes=[{"id": "c001", "label": "SuperPCA"},
               {"id": "c002", "label": "スーパーピクセル"},
               {"id": "c003", "label": "主成分分析"}],
        edges=[{"id": "r001", "from": "c001", "to": "c002", "glyph": "arrow",
                "label": "領域単位で適用",
                "evidence_span": [{"document_id": "superpca.pdf",
                                   "surface": "SuperPCA はスーパーピクセル単位で"
                                              "主成分分析を行う"}]},
               {"id": "r002", "from": "c001", "to": "c003", "glyph": "wave",
                "label": "拡張である",
                "evidence_span": [{"document_id": "superpca.pdf",
                                   "surface": "主成分分析の空間拡張にあたる"}]}])
    write_session(
        root, "20260202_000000",
        nodes=[{"id": "n01", "label": "ハイパースペクトル画像"},
               {"id": "n02", "label": "スペクトル次元削減"},
               {"id": "n03", "label": "SuperPCA"}],
        edges=[{"id": "e01", "from": "n01", "to": "n02", "glyph": "arrow",
                "label": "計算量を減らす"},
               {"id": "e02", "from": "n02", "to": "n03", "glyph": "double",
                "label": "手法の一つ"}])
    write_session(
        root, "20260303_000000",
        nodes=[{"id": "m01", "label": "月面データ"},
               {"id": "m02", "label": "連続体除去"},
               {"id": "m03", "label": "スペクトル吸収特性"}],
        edges=[{"id": "g01", "from": "m01", "to": "m02", "glyph": "arrow",
                "label": "前処理する"},
               {"id": "g02", "from": "m02", "to": "m03", "glyph": "arrow",
                "label": "際立たせる"}])
    return root


@pytest.fixture()
def store(graphs: Path) -> SessionStore:
    return SessionStore(graphs)


class FakeClient:
    """cc-analysis の応答を差し替える最小のスタブ。

    予定より多く呼ばれたら**その場で失敗する** — 「上限を守る」テストで、
    呼び出し回数の取り違えを黙って通さないため。
    """

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def run(self, agent: str, prompt: str, **_: Any) -> str:
        self.calls.append((agent, prompt))
        if not self.replies:
            raise AssertionError(f"予定より多く LLM を呼んだ ({len(self.calls)} 回目)")
        reply = self.replies.pop(0)
        return reply if isinstance(reply, str) else json.dumps(reply,
                                                               ensure_ascii=False)

    def payloads(self) -> list[dict[str, Any]]:
        """送った JSON (prompt の 2 行目以降) を読み直す。"""
        return [json.loads(prompt.split("\n", 1)[1]) for _, prompt in self.calls]


# ====================================================== 経路判定 (裁定 N)

# 既存 3 経路の判定表。**この表は R1 の test_r1_features と同じ内容**で、
# 1 行でも動いたら裁定 N の違反になる (新 cue は既存の入口を奪わない)
EXISTING_ROUTES = [
    ("今週の研究を概念地図として整理して", "map"),
    ("先月の成果を図にして", "map"),
    ("NV中心とは何ですか", "vector"),
    ("実験は何件ありましたか", "vector"),
    ("こんにちは", "basic"),
    ("ありがとう", "basic"),
    # 期間だけの依頼は従来どおり map (手がかりが無い入力の既定は変えない)
    ("今週の資料をお願いします", "map"),
    ("ふわっとした要求", "map"),
]


@pytest.mark.parametrize("message,expected", EXISTING_ROUTES)
def test_existing_three_routes_are_unchanged(message: str, expected: str) -> None:
    assert route(message).route == expected


@pytest.mark.parametrize("message,expected", [
    ("SuperPCAとスーパーピクセルの関係は?", "local"),
    ("なぜ計算量が減るのですか", "local"),
    ("この結果に至った経緯を知りたい", "local"),
    ("ノイズの原因を知りたい", "local"),
    ("研究の全体像を知りたい", "global"),
    ("最近のテーマは", "global"),
    ("セッションを横断して何が分かっていますか", "global"),
    ("全体像を踏まえた上で個別の関係を見たい", "hybrid"),
    ("原因と全体像をあわせて知りたい", "hybrid"),
    ("2つの手法を比較して", "hybrid"),
])
def test_new_cues_pick_qa_routes(message: str, expected: str) -> None:
    assert route(message).route == expected


def test_map_cue_still_wins_over_qa_cues() -> None:
    """地図の明示語があれば QA の手がかりがあっても map (裁定 N の肝)。"""
    assert route("なぜそうなるのかも含めて概念地図にして").route == "map"
    assert route("全体像を図にして").route == "map"
    assert route("原因を整理して").route == "map"


def test_routes_and_dispatch_table_agree() -> None:
    """ROUTES / ANSWER_ROUTES / ディスパッチ表 / qa.ANSWERERS が食い違わない。"""
    assert set(ROUTES) == set(ANSWER_ROUTES) | {"map"}
    assert set(pipeline.ROUTE_HANDLERS) == set(ANSWER_ROUTES)
    assert set(qa.ANSWERERS) == set(qa.QA_ROUTES) <= set(ROUTES)


def test_basic_and_vector_prompts_are_unchanged() -> None:
    """R1 の直答 2 経路は、投げる相手も文面も 1 文字も変えていない。"""
    client = FakeClient("こんにちは。", "答えです。")
    assert pipeline.ROUTE_HANDLERS["basic"](
        "こんにちは", client=client) == {"answer": "こんにちは。"}
    pipeline.ROUTE_HANDLERS["vector"]("NV中心とは何ですか", client=client)
    assert [agent for agent, _ in client.calls] == ["cc-extraction"] * 2
    assert client.calls[0][1] == "こんにちは"
    assert client.calls[1][1] == (
        "次の質問に、必要なら Work IQ / KB で調べて簡潔に答えてください:\n"
        "NV中心とは何ですか")


# ================================================== 検索語の取り出し (§2)


def test_known_terms_prefers_the_longest_label() -> None:
    """長い一致に含まれる短い一致は落とす (材料を薄めない)。"""
    vocab = ["superpca", "スーパーピクセル", "ピクセル", "主成分分析"]
    # 「ピクセル」は「スーパーピクセル」に含まれるので落ちる。並びは
    # (長さ降順, 文字列昇順) で完全に決定的 — 同じ問いに毎回同じ材料を渡す
    assert qa.known_terms("SuperPCAとスーパーピクセルの関係は?", vocab) == [
        "superpca", "スーパーピクセル"]


def test_known_terms_uses_the_index_vocabulary(store: SessionStore) -> None:
    assert qa.question_terms("SuperPCAとスーパーピクセルの関係は?", store) == [
        "superpca", "スーパーピクセル"]


def test_split_terms_is_the_fallback_when_nothing_is_known() -> None:
    """既知ラベルが 0 件のときだけ助詞で割る。問いの骨組みは落とす。"""
    terms = qa.split_terms("量子センサと磁場計測の関係は?")
    assert "量子センサ" in terms and "磁場計測" in terms
    assert "関係" not in terms


# ============================================== local の梱包と出典 (§2)


def test_local_material_packs_the_two_hop_neighbourhood(store: SessionStore) -> None:
    """索引の当たり → 出自セッションの近傍 → ref つきの材料 (設計 §2)。"""
    material = qa.local_material(store, ["superpca"])
    refs = {row["ref"] for row in material.context["concepts"]}
    assert "n:20260101_000000:c001" in refs      # 起点そのもの
    assert "n:20260101_000000:c002" in refs      # 1 hop
    assert material.seeds and all(r in refs for r in material.seeds)
    relations = {row["ref"]: row for row in material.context["relations"]}
    edge = relations["e:20260101_000000:r001"]
    assert edge["from"] == "SuperPCA" and edge["to"] == "スーパーピクセル"
    assert edge["type"] == "因果"                 # glyph は日本語名で渡す
    assert "スーパーピクセル単位" in edge["evidence"]
    # 出典は根拠スパンの document_id から引く (セッション代表ではない)
    assert material.sources["e:20260101_000000:r001"]["document_id"] == "superpca.pdf"
    assert len(material.context["concepts"]) <= qa.MAX_CONTEXT_NODES


def test_local_answer_keeps_only_citations_that_exist(store: SessionStore) -> None:
    """LLM が返した出典は索引に実在するものだけ残す (裁定 M)。"""
    client = FakeClient({"answer": "SuperPCA はスーパーピクセル単位で主成分分析を行います。",
                         "cited": ["e:20260101_000000:r001", "n:99999999_000000:zzz"]})
    result = qa.answer_local("SuperPCAとスーパーピクセルの関係は?", store, client)
    assert result["qa"]["llm_calls"] == 1 and result["qa"]["cited"] == 1
    assert [s["kind"] for s in result["sources"]] == ["edge"]
    assert result["sources"][0]["session"] == "20260101_000000"
    assert result["sources"][0]["document_id"] == "superpca.pdf"
    payload = client.payloads()[0]
    assert payload["task"] == "qa"
    sent = {row["ref"] for row in payload["context"]["concepts"]}
    assert "n:20260101_000000:c001" in sent


def test_local_sources_fall_back_to_the_seeds(store: SessionStore) -> None:
    """LLM が何も引かなくても「何を見て答えたか」は示す。"""
    client = FakeClient({"answer": "資料からは判断できません。", "cited": []})
    result = qa.answer_local("SuperPCAとスーパーピクセルの関係は?", store, client)
    assert result["qa"]["cited"] == 0
    assert result["sources"] and all(s["kind"] == "node" for s in result["sources"])


def test_local_without_any_hit_says_so_without_calling_the_llm(
        store: SessionStore) -> None:
    client = FakeClient()          # 1 回でも呼んだら AssertionError
    result = qa.answer_local("ニュートリノ振動の関係は?", store, client)
    assert result["qa"]["insufficient"] and result["sources"] == []
    assert "--reindex" in result["answer"]


# =========================================== global の要約とキャッシュ (裁定 L)


def test_global_second_call_hits_the_summary_cache(store: SessionStore) -> None:
    """2 回目は要約ぶんが 0 call (受け入れ基準 3)。"""
    top = qa.rank_communities(store.corpus_communities(), [])
    assert len(top) >= 2, "テスト用コーパスに島が 2 つ以上要る"

    first = FakeClient(*([{"title": "T", "summary": "この島は分光の話です。"}]
                         * len(top)),
                       {"answer": "研究の全体像です。", "cited": []})
    result = qa.answer_global("私の研究の全体像をまとめて", store, first)
    assert result["qa"]["llm_calls"] == len(top) + 1
    assert result["qa"]["cache_hits"] == 0
    assert [s["kind"] for s in result["sources"]] == ["community"] * len(top)

    second = FakeClient({"answer": "研究の全体像です (2 回目)。", "cited": []})
    again = qa.answer_global("私の研究の全体像をまとめて", store, second)
    assert again["qa"]["llm_calls"] == 1            # 統合の 1 call だけ
    assert again["qa"]["cache_hits"] == len(top)
    assert (store.corpus_dir / corpus.SUMMARIES).exists()


def test_global_falls_back_to_the_biggest_islands(store: SessionStore) -> None:
    """概念名を含まない問いでは大きい島から採る (「該当なし」で終わらせない)。"""
    meta = store.corpus_communities()
    picks = qa.rank_communities(meta, [])
    sizes = [len(members) for _, members in picks]
    assert sizes == sorted(sizes, reverse=True) and sizes[0] >= 2


def test_qa_call_budget_is_capped(store: SessionStore, monkeypatch) -> None:
    """CC_QA_MAX_CALLS を超えたら材料を削り、その旨を答えに書く (設計 §2)。"""
    monkeypatch.setenv("CC_QA_MAX_CALLS", "2")
    client = FakeClient({"title": "T", "summary": "1 つ目の島の要約。"},
                        {"answer": "部分的な全体像です。", "cited": []})
    result = qa.answer_global("私の研究の全体像をまとめて", store, client)
    assert result["qa"]["llm_calls"] == 2
    assert result["qa"]["budget_exceeded"] >= 1
    assert "CC_QA_MAX_CALLS=2" in result["answer"]


# ========================================================== hybrid (§2)


def test_hybrid_merges_neighbourhood_and_summaries(store: SessionStore) -> None:
    top = qa.rank_communities(store.corpus_communities(), ["superpca"])
    client = FakeClient(*([{"title": "T", "summary": "島の要約。"}] * len(top)),
                        {"answer": "統合した答えです。",
                         "cited": ["n:20260101_000000:c001"]})
    result = qa.answer_hybrid("SuperPCAの原因と全体像をあわせて", store, client)
    payload = client.payloads()[-1]
    assert payload["context"]["concepts"] and payload["context"]["summaries"]
    assert result["qa"]["route"] == "hybrid"
    assert result["sources"][0]["label"] == "SuperPCA"


# ========================================================= offline (§2)


def test_offline_local_is_a_deterministic_listing(store: SessionStore) -> None:
    """LLM なしでも関係を列挙して返す。**そう明記する** (設計 §2)。"""
    first = qa.answer_local("SuperPCAとスーパーピクセルの関係は?", store, None,
                            offline=True)
    second = qa.answer_local("SuperPCAとスーパーピクセルの関係は?", store, None,
                             offline=True)
    assert first["answer"] == second["answer"]          # 決定的
    assert "LLM なし要約" in first["answer"]
    assert "SuperPCA —[因果: 領域単位で適用]→ スーパーピクセル" in first["answer"]
    assert first["qa"]["offline"] and first["qa"]["llm_calls"] == 0
    assert first["sources"]


def test_offline_global_asks_for_an_online_run(store: SessionStore) -> None:
    """global / hybrid はエラーにせず案内文を返す (設計 §2)。"""
    result = qa.answer_global("私の研究の全体像をまとめて", store, None, offline=True)
    assert "オンライン実行が必要" in result["answer"]
    assert result["qa"]["llm_calls"] == 0 and result["qa"]["offline"]
    hybrid = qa.answer_hybrid("原因と全体像を", store, None, offline=True)
    assert "オンライン実行が必要" in hybrid["answer"]


# ================================================ 応答の修復 (analysis.py)


def test_repair_qa_drops_unknown_refs_and_reports() -> None:
    report = analysis.AnalysisReport()
    out = analysis.repair_qa(
        {"answer": " 答え ", "cited": ["a", {"ref": "b"}, "zzz", "a"],
         "insufficient": True}, ["a", "b"], report)
    assert out == {"answer": "答え", "cited": ["a", "b"], "insufficient": True}
    assert report.repairs["qa: context に無い出典を破棄"] == 1


def test_repair_community_summary_fills_a_missing_title() -> None:
    report = analysis.AnalysisReport()
    out = analysis.repair_community_summary(
        {"summary": "分光の話。"}, ["月面データ", "連続体除去", "他"], report)
    assert out["summary"] == "分光の話。"
    assert out["title"] == "月面データ・連続体除去"


# ================================================ pipeline / CLI / Web


def test_pipeline_routes_a_question_to_qa(graphs: Path, monkeypatch) -> None:
    """ディスパッチ表経由で summary に answer / sources / qa が載る。"""
    monkeypatch.chdir(graphs.parent)
    summary = pipeline.run_pipeline("SuperPCAとスーパーピクセルの関係は?",
                                    offline=True, target="file")
    assert summary["status"] == "answered"
    assert summary["routing"]["route"] == "local"
    assert "LLM なし要約" in summary["answer"]
    assert summary["sources"] and summary["qa"]["offline"]


def test_pipeline_still_requires_kg_file_for_offline_maps(graphs: Path,
                                                          monkeypatch) -> None:
    """offline の地図生成は従来どおり kg_file 必須 (QA だけが例外)。"""
    monkeypatch.chdir(graphs.parent)
    with pytest.raises(ValueError, match="kg_file"):
        pipeline.run_pipeline("今週の研究を概念地図として整理して", offline=True)


def test_cli_prints_the_answer_with_its_sources(capsys) -> None:
    _print_summary({
        "status": "answered",
        "routing": {"route": "local", "rationale": "テスト"},
        "answer": "SuperPCA は…",
        "sources": [{"kind": "edge", "label": "A →（因果）→ B",
                     "session": "20260101_000000", "document_id": "superpca.pdf"},
                    {"kind": "community", "label": "分光 ほか 3 概念",
                     "session": "", "document_id": ""}],
        "qa": {"llm_calls": 2, "cache_hits": 1, "sessions": ["20260101_000000"],
               "communities": ["corpus_coarse_000"]},
    })
    out = capsys.readouterr().out
    assert "📚 出典 (2 件)" in out
    assert "→ A →（因果）→ B  [20260101_000000 / superpca.pdf]" in out
    assert "◆ 分光 ほか 3 概念  [コーパス全体]" in out
    assert "LLM 2 call / 要約キャッシュ命中 1" in out


def test_web_job_returns_the_answer_and_sources(graphs: Path, monkeypatch) -> None:
    """Web も同じ summary を返す (app.js の出典チップが読む形)。"""
    from fastapi.testclient import TestClient

    from cc_web import account
    from cc_web.app import create_app

    monkeypatch.chdir(graphs.parent)
    monkeypatch.setattr(account, "_az_upn", lambda: "tester@example.ac.jp")
    account.clear_cache()
    with TestClient(create_app()) as client:
        res = client.post("/api/jobs", json={
            "message": "SuperPCAとスーパーピクセルの関係は?", "offline": True,
            "target": "file"})
        assert res.status_code == 202, res.text
        job_id = res.json()["job_id"]
        for _ in range(600):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in ("done", "error"):
                break
        assert job["status"] == "done", job.get("error")
    account.clear_cache()
    summary = job["summary"]
    assert summary["answer"] and summary["sources"]
    assert {"kind", "label", "session", "document_id"} <= set(summary["sources"][0])
    assert summary["qa"]["route"] == "local"


def test_app_js_renders_the_qa_sources() -> None:
    """出典チップの配線が app.js に残っていること (表示の取りこぼし防止)。"""
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "answerBubble(summary)" in source
    block = re.search(r"var SOURCE_KIND = \{(.+?)\};", source, re.S)
    assert block, "app.js の SOURCE_KIND が見つからない"
    kinds = set(re.findall(r"(\w+):\s*\{ label:", block.group(1)))
    assert kinds == {"node", "edge", "community"}
