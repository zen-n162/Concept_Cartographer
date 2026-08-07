"""R2c-2: ギャップレポートと閉世界推薦 (設計書 §2・裁定 R/V)。

主眼は 5 つ:

  1. **finding は決定的**。3 型それぞれが store の中身だけから同じ文を出す。
     LLM を 1 回も呼ばずに成立することがレポートの前提 (受け入れ基準 3)。
  2. **suggestion は任意**。付かないときはキーごと無い (空文字を置かない) —
     「提案が無い」と「提案が空」を UI が区別できるようにするため。
  3. **既定で外部アクセスがゼロ** (受け入れ基準 4)。env を立てない限り
     socket が 1 本も開かないことを socket 層で見る。裁定 R の「①のみ」。
  4. 裁定 V の対応表は**当たった id だけ**を載せる。不透明な M365 ID を
     当てずっぽうでファイル名に結びつけない (出典の誤りは生 id より悪い)。
  5. 裁定 U の疑問形拡充で既存の経路判定が 1 件も動かない。

各テストは tmp_path に graphs/ と logs/ と exports/ を作るので production/
を汚さない。LLM は素の lambda を渡して差し替える (このリポジトリの流儀)。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from cc_core import gap_report, layers_store
from cc_core.gap_report import build_gap_report, save_report, to_markdown
from cc_store import SessionStore

PRODUCTION = Path(__file__).resolve().parents[1]

TARGET = "20260301_000000"
OTHER = "20260101_000000"


# --------------------------------------------------------------- 補助


def write_session(graphs: Path, session: str, *, nodes: list[dict],
                  edges: list[dict], gaps: list[dict] | None = None,
                  layers: dict | None = None,
                  communities: list[dict] | None = None) -> None:
    """kg + plan (+ layers サイドカー) を直に書く。"""
    graphs.mkdir(parents=True, exist_ok=True)
    kg = {"graph_version": "kg_r2c_test", "nodes": nodes, "edges": edges,
          "communities": communities or [{"id": "comm_a", "name": "A", "is_gap": False}]}
    (graphs / f"kg_session_{session}.json").write_text(
        json.dumps(kg, ensure_ascii=False), encoding="utf-8")
    plan = {"detail_level": "standard", "gaps": gaps or [],
            "nodes": [{"id": n["id"], "kind": "concept"} for n in nodes]}
    (graphs / f"layout_plan_session_{session}.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    if layers is not None:
        doc = layers_store.new_document(session)
        doc.update(layers)
        layers_store.save(session, doc, graphs_dir=graphs)


def gap(gap_id: str, kind: str, **extra) -> dict:
    row = {"gap_id": gap_id, "status": "candidate", "confidence": 0.7,
           "presumed_type": "unknown", "gap_type": kind,
           "detection_signal": "", "reason": f"{kind} のギャップ",
           "toulmin": {"grounds": "", "warrant": ""},
           "evidence_links": [], "related_node_ids": []}
    row.update(extra)
    return row


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    """2 セッション。

    TARGET は 3 型のギャップを 1 件ずつ持つ。OTHER は同じ概念「次元削減」を
    扱っていて、手法 (Method) の文と機序を述べた根拠スパンを持っている
    = 「答えはもう自分の中にある」側の資料。
    """
    graphs = tmp_path / "graphs"
    write_session(
        graphs, TARGET,
        nodes=[
            {"id": "c1", "label": "次元削減", "community_id": "comm_a",
             "evidence_span": [{"document_id": "新報告.pdf", "surface": "次元削減を試した"}]},
            {"id": "c2", "label": "計算負荷", "community_id": "comm_a"},
            {"id": "c3", "label": "知識グラフ", "community_id": "comm_b"},
        ],
        edges=[{"id": "r1", "from": "c1", "to": "c2", "glyph": "wave",
                "label": "減らす",
                "evidence_span": [{"document_id": "新報告.pdf",
                                   "surface": "次元削減と計算負荷は関連する"}]}],
        communities=[{"id": "comm_a", "name": "A", "is_gap": False},
                     {"id": "comm_b", "name": "B", "is_gap": False}],
        gaps=[
            gap("gap-bridge-comm_a-comm_b", "structural",
                related_node_ids=["c1", "c3"], community_id="comm_a"),
            gap("gap-discourse-c1", "discourse", related_node_ids=["c1"]),
            gap("gap-causal-r1", "causal", related_node_ids=["c1", "c2"],
                evidence_links=[{"rejection_log": str(tmp_path / "rej.jsonl"),
                                 "target_id": "r1"}]),
        ],
        layers={"zones": [{"sentence_id": "s1", "text": "結果を示す",
                           "zone_label": "Result", "document_id": "新報告.pdf"}],
                "documents": {"新報告.pdf": "新報告.pdf"}})
    write_session(
        graphs, OTHER,
        nodes=[
            {"id": "n1", "label": "次元削減", "community_id": "comm_a",
             "evidence_span": [{"document_id": "doc-9911",
                                "surface": "次元削減は計算負荷を機序として下げる"}]},
            {"id": "n2", "label": "知識グラフ", "community_id": "comm_a"},
        ],
        edges=[{"id": "e1", "from": "n1", "to": "n2", "glyph": "arrow",
                "label": "支える",
                "evidence_span": [{"document_id": "doc-9911",
                                   "surface": "次元削減と知識グラフを同じ枠で扱う"}]}],
        layers={"zones": [{"sentence_id": "z1", "text": "PCA で次元を落とす",
                           "zone_label": "Method", "document_id": "doc-9911"}],
                # 裁定 V: 不透明な id -> ファイル名の対応表がある側
                "documents": {"doc-9911": "旧レポート.pdf"}})
    (tmp_path / "rej.jsonl").write_text(json.dumps(
        {"target_id": "r1", "kind": "causal_edge",
         "scores": {"nli": 0.4, "llm": 0.01}}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    return graphs


@pytest.fixture()
def store(corpus: Path) -> SessionStore:
    return SessionStore(corpus)


@pytest.fixture()
def no_network(monkeypatch: pytest.MonkeyPatch) -> list:
    """socket 層で外へ出る道を塞ぐ (受け入れ基準 4 の担保)。

    httpx / urllib のどの層を通っても最後は socket に落ちるので、ここを
    見張れば「外部アクセスが無い」を漏れなく言える。呼ばれた事実を残して
    テスト側が件数 0 を主張できるようにしておく。
    """
    attempts: list[str] = []

    def blocked(*args, **kwargs):
        attempts.append("network")
        raise AssertionError("既定実行でネットワークへ出ようとしました")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    return attempts


# =============================================== 1. 型別 finding の決定性


def test_structural_finding_lists_documents_touching_both_sides(store) -> None:
    """構造: 両側の代表概念に**両方**触れている資料を挙げる (設計 §2.1)。"""
    report = build_gap_report(TARGET, store)
    item = next(i for i in report["items"] if i["gap_type"] == "structural")
    assert item["anchors"] == ["次元削減", "知識グラフ"]
    assert "両方" in item["finding"]
    # OTHER が両方に触れているので、その資料が根拠として出る
    assert OTHER in item["finding"]
    assert any(s["session"] == OTHER for s in item["sources"])


def test_discourse_finding_points_at_other_session_with_method(store) -> None:
    """言説: 欠けている Method を**持っている他セッション**を教える。"""
    report = build_gap_report(TARGET, store)
    item = next(i for i in report["items"] if i["gap_type"] == "discourse")
    assert item["anchors"] == ["次元削減"]
    assert OTHER in item["finding"]
    assert "Method" in item["finding"]
    zones = [s for s in item["sources"] if s["kind"] == "zone"]
    assert zones and zones[0]["label"] == "Method"


def test_discourse_finding_does_not_contradict_itself(tmp_path: Path) -> None:
    """自セッションに Method 文が**ある**のに「無い」と書かない。

    このギャップは「その文がこの概念を根拠づけていない」の意味なので、
    セッションの zone 一覧を素で出すと自己矛盾する (実測で踏んだ)。
    """
    graphs = tmp_path / "graphs"
    write_session(
        graphs, TARGET,
        nodes=[{"id": "c1", "label": "孤高概念", "community_id": "comm_a"}],
        edges=[],
        gaps=[gap("gap-discourse-c1", "discourse", related_node_ids=["c1"])],
        layers={"zones": [{"sentence_id": "s1", "text": "PCA を使った",
                           "zone_label": "Method", "document_id": "只一つ.pdf"}]})
    item = build_gap_report(TARGET, SessionStore(graphs))["items"][0]
    assert "使われていません" in item["finding"]
    assert "文自体は 1 件あります" in item["finding"]


def test_causal_finding_uses_rejection_reason_and_causal_cues(store) -> None:
    """因果: 却下スコア + 機序手がかり語で根拠スパンを横断検索する。"""
    report = build_gap_report(TARGET, store)
    item = next(i for i in report["items"] if i["gap_type"] == "causal")
    assert item["anchors"] == ["次元削減", "計算負荷"]
    # OTHER の「機序として」を拾えている
    assert "機序を述べた記述が 1 件" in item["finding"]
    assert "nli=0.4" in item["finding"]          # 却下ログの理由が出ている
    assert any(s["session"] == OTHER for s in item["sources"])


def test_causal_finding_when_no_mechanism_exists(tmp_path: Path) -> None:
    """機序記述がどこにも無ければ「ありません」と言い切る (曖昧に濁さない)。"""
    graphs = tmp_path / "graphs"
    write_session(
        graphs, TARGET,
        nodes=[{"id": "c1", "label": "甲", "community_id": "comm_a"},
               {"id": "c2", "label": "乙", "community_id": "comm_a"}],
        edges=[{"id": "r1", "from": "c1", "to": "c2", "glyph": "wave",
                "evidence_span": [{"document_id": "x.pdf", "surface": "甲と乙は相関する"}]}],
        gaps=[gap("gap-causal-r1", "causal", related_node_ids=["c1", "c2"],
                  evidence_links=[{"rejection_log": "missing.jsonl",
                                   "target_id": "r1"}])])
    item = build_gap_report(TARGET, SessionStore(graphs))["items"][0]
    assert "**ありません**" in item["finding"]
    assert item["sources"] == []


def test_findings_are_deterministic(store) -> None:
    """同じ入力なら同じ finding (generated_at 以外は完全一致)。"""
    a = build_gap_report(TARGET, store)
    b = build_gap_report(TARGET, SessionStore(store.graphs_dir))
    assert [i["finding"] for i in a["items"]] == [i["finding"] for i in b["items"]]
    assert a["counts"] == b["counts"] == {"structural": 1, "discourse": 1, "causal": 1}


# =============================================== 2. LLM (任意・上限つき)


def test_report_without_client_has_no_suggestion_key(store) -> None:
    """LLM 無しでもレポートは成立し、suggestion は**キーごと**現れない。"""
    report = build_gap_report(TARGET, store)
    assert report["llm_calls"] == 0 and report["suggestions"] == 0
    assert all("suggestion" not in i for i in report["items"])
    assert all(i["finding"] for i in report["items"])       # finding は全件ある


def test_llm_call_budget_is_capped(store) -> None:
    """max_llm_calls を超えて呼ばない (上限 5 が既定)。"""
    calls: list[str] = []

    def run(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps({"suggestion": "次の資料を確かめる"})

    report = build_gap_report(TARGET, store, client=run, max_llm_calls=2)
    assert len(calls) == 2
    assert report["llm_calls"] == 2 and report["suggestions"] == 2
    assert sum("suggestion" in i for i in report["items"]) == 2
    assert json.loads(calls[0].split("\n", 1)[1])["task"] == gap_report.SUGGEST_TASK


def test_llm_failure_leaves_finding_intact(store) -> None:
    """提案が取れなくても finding は残る (LLM の事故でレポートを失わない)。"""
    def run(prompt: str) -> str:
        raise RuntimeError("token expired")

    report = build_gap_report(TARGET, store, client=run, max_llm_calls=3)
    assert report["llm_calls"] == 3 and report["suggestions"] == 0
    assert all("suggestion" not in i for i in report["items"])
    assert all(i["finding"] for i in report["items"])


def test_llm_budget_is_spread_across_the_three_kinds(tmp_path: Path) -> None:
    """予算は型を回して配る。同じ型で使い切らない (実測で踏んだ配分の偏り)。

    構造 4 / 言説 2 / 因果 1 のセッションで 3 call だけ許すと、確度順の素朴な
    並べ方なら構造 3 件に全部行く。型ごとに 1 件ずつ配れば 3 型に届く。
    """
    graphs = tmp_path / "graphs"
    gaps = [gap(f"gap-weak-c{i}", "structural", confidence=0.9,
                related_node_ids=["c1"]) for i in range(4)]
    gaps += [gap(f"gap-discourse-d{i}", "discourse", confidence=0.5,
                 related_node_ids=["c1"]) for i in range(2)]
    gaps += [gap("gap-causal-r1", "causal", confidence=0.1,
                 related_node_ids=["c1", "c2"])]
    write_session(graphs, TARGET,
                  nodes=[{"id": "c1", "label": "甲", "community_id": "comm_a"},
                         {"id": "c2", "label": "乙", "community_id": "comm_a"}],
                  edges=[], gaps=gaps)

    report = build_gap_report(TARGET, SessionStore(graphs),
                              client=lambda p: json.dumps({"suggestion": "確かめる"}),
                              max_llm_calls=3)
    got = {i["gap_type"] for i in report["items"] if i.get("suggestion")}
    assert got == {"structural", "discourse", "causal"}
    assert report["suggestions"] == 3


def test_client_object_with_run_method_is_accepted(store) -> None:
    """`.run(agent, prompt)` を持つクライアントでも呼べる (Foundry の形)。"""
    seen: list[str] = []

    class FakeClient:
        def run(self, agent: str, prompt: str) -> str:
            seen.append(agent)
            return json.dumps({"suggestion": "橋渡しを確かめる"})

    report = build_gap_report(TARGET, store, client=FakeClient(), max_llm_calls=1)
    assert seen == [gap_report.AGENT]
    assert report["suggestions"] == 1


# =============================================== 3. 裁定 R: 情報源


def test_default_run_touches_no_network(store, no_network) -> None:
    """**受け入れ基準 4**: 既定設定で socket が 1 本も開かない。"""
    report = build_gap_report(TARGET, store)
    assert no_network == []
    assert report["external_used"] is False
    assert all("external" not in i for i in report["items"])


def test_kb_is_not_attempted_when_unset(store, monkeypatch, no_network) -> None:
    """②KB は CC_KB_AGENT が無ければ試みず、レポートに未接続と書く。"""
    monkeypatch.delenv(gap_report.KB_AGENT_ENV, raising=False)
    report = build_gap_report(TARGET, store)
    assert report["kb"] == {"connected": False, "note": "kb: 未接続"}
    assert "kb: 未接続" in to_markdown(report)


def test_kb_agent_is_recorded_when_set(store, monkeypatch, no_network) -> None:
    monkeypatch.setenv(gap_report.KB_AGENT_ENV, "cc-kb")
    report = build_gap_report(TARGET, store)
    assert report["kb"]["connected"] is True
    assert report["kb"]["agent"] == "cc-kb"


def test_external_env_default_off(store, monkeypatch, no_network) -> None:
    """CC_EXTERNAL_RECS が未設定/0 のときは外部照会をしない。"""
    for value in (None, "0", "", "true"):     # "1" 以外はすべて OFF
        if value is None:
            monkeypatch.delenv(gap_report.EXTERNAL_ENV, raising=False)
        else:
            monkeypatch.setenv(gap_report.EXTERNAL_ENV, value)
        assert build_gap_report(TARGET, store)["external_used"] is False
    assert no_network == []


def test_external_on_logs_every_query(store, tmp_path, monkeypatch) -> None:
    """③ON のとき: 送信は概念ラベルだけ + 全件を jsonl に残す (裁定 R)。"""
    import httpx

    sent: list[str] = []

    def fake_get(url, **kwargs):
        sent.append(url)
        assert kwargs["headers"]["User-Agent"].startswith("ConceptCartographer/")
        return httpx.Response(200, text=(
            "<feed><title>ArXiv Query</title>"
            "<entry><title>Dimensionality reduction survey</title></entry></feed>"))

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setenv(gap_report.EXTERNAL_ENV, "1")
    log = tmp_path / "logs" / "external_queries.jsonl"

    report = build_gap_report(TARGET, store, external_log=log)
    assert report["external_used"] is True

    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == len(sent) == 3          # ギャップ 3 件ぶん
    assert all(set(r) == {"ts", "url", "query"} for r in rows)
    # 送ったのは概念ラベルだけ。セッション ID も問い文も混ざっていない
    assert {r["query"] for r in rows} <= {"次元削減"}
    assert all(TARGET not in r["url"] for r in rows)


def test_external_query_is_not_sent_when_it_cannot_be_logged(store, monkeypatch,
                                                             tmp_path) -> None:
    """記録できないなら送らない。レポート自体は finding だけで残り、理由が付く。

    「何を外に出したか復元できない状態」を作らないのが第一。とはいえ
    ログの事故でレポートまで失うのは筋が違うので、外部照会だけ止める。
    """
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("送信された"))
    monkeypatch.setenv(gap_report.EXTERNAL_ENV, "1")
    blocked = tmp_path / "logs"
    blocked.write_text("ファイルなのでディレクトリを作れない", encoding="utf-8")

    report = build_gap_report(TARGET, store,
                              external_log=blocked / "external_queries.jsonl")
    assert report["external_used"] is False
    assert "外部照会を中止" in report["external_note"]
    assert len(report["items"]) == 3                 # finding は全部ある
    assert "⚠" in to_markdown(report)


# =============================================== 4. 出力 (Markdown / 保存)


def test_markdown_groups_by_kind_and_marks_llm_free(store, no_network) -> None:
    md = to_markdown(build_gap_report(TARGET, store))
    assert "# ギャップレポート — セッション " + TARGET in md
    for heading in ("## 構造ギャップ (1 件)", "## 言説ギャップ (1 件)",
                    "## 因果ギャップ (1 件)"):
        assert heading in md
    assert "LLM 提案: 0 件 (finding のみで成立)" in md
    assert "外部照会: なし (既定)" in md
    assert "**わかっていること**:" in md


def test_markdown_shows_resolved_file_names(store, no_network) -> None:
    """裁定 V: 出典は対応表を通した**ファイル名**で出る。"""
    md = to_markdown(build_gap_report(TARGET, store))
    assert "旧レポート.pdf" in md            # doc-9911 が解決されている
    assert "doc-9911" not in md


def test_save_report_writes_json_and_markdown(store, tmp_path, no_network) -> None:
    report = build_gap_report(TARGET, store)
    saved = save_report(report, out_dir=tmp_path / "exports")
    assert saved["json"].name == f"gap_report_{TARGET}.json"
    assert saved["md"].name == f"gap_report_{TARGET}.md"
    assert json.loads(saved["json"].read_text(encoding="utf-8"))["session"] == TARGET
    assert saved["md"].read_text(encoding="utf-8").startswith("# ギャップレポート")


def test_report_of_session_without_gaps_is_still_valid(tmp_path, no_network) -> None:
    graphs = tmp_path / "graphs"
    write_session(graphs, TARGET, nodes=[], edges=[], gaps=[])
    report = build_gap_report(TARGET, SessionStore(graphs))
    assert report["items"] == [] and report["counts"] == {}
    assert "ギャップ候補はありません" in to_markdown(report)


# =============================================== 5. 裁定 V: documents 対応表


@pytest.mark.parametrize("doc_id,names,expected", [
    ("報告.pdf", ["報告.pdf"], "報告.pdf"),                       # 完全一致
    ("drive/abc/報告.pdf", ["報告.pdf"], "報告.pdf"),             # basename
    ("報告", ["報告.pdf"], "報告.pdf"),                           # 拡張子なし
    ("01ABC!123-報告.pdf", ["報告.pdf"], "報告.pdf"),             # 埋め込み
    ("409", ["報告.pdf"], None),                                  # 不透明 ID は載せない
    ("01ABC!123", ["報告.pdf", "別紙.pdf"], None),                # 手がかりなし
])
def test_build_documents_only_maps_what_it_can_prove(doc_id, names, expected) -> None:
    table = layers_store.build_documents([doc_id], names)
    assert table.get(doc_id) == expected


def test_build_documents_skips_ambiguous_substring_matches() -> None:
    """候補が 2 件以上ある部分一致は**捨てる** (誤った出典より生 id が良い)。"""
    table = layers_store.build_documents(["まとめ_A.pdf_B.pdf"],
                                         ["A.pdf", "B.pdf"])
    assert table == {}


def test_resolve_document_falls_back_to_raw_id() -> None:
    assert layers_store.resolve_document("409", {}) == "409"
    assert layers_store.resolve_document("409", {"409": "報告.pdf"}) == "報告.pdf"


def test_documents_survives_save_and_load(tmp_path: Path) -> None:
    doc = layers_store.new_document("s1")
    doc["documents"] = {"doc-1": "報告.pdf"}
    layers_store.save("s1", doc, graphs_dir=tmp_path)
    assert layers_store.documents_of(
        layers_store.load("s1", graphs_dir=tmp_path)) == {"doc-1": "報告.pdf"}


def test_old_sidecar_without_documents_is_left_alone(tmp_path: Path) -> None:
    """過去セッションに対応表を**生やさない** (遡及変換はしない: 裁定 V)。"""
    (tmp_path / "layers_session_s1.json").write_text(
        json.dumps({"version": 1, "session": "s1", "zones": []}), encoding="utf-8")
    loaded = layers_store.load("s1", graphs_dir=tmp_path)
    assert "documents" not in loaded
    assert layers_store.documents_of(loaded) == {}


def test_collect_document_ids_covers_zones_claims_and_evidence() -> None:
    doc = {"zones": [{"document_id": "z.pdf"}],
           "claims": [{"pub_info": {"document_id": "c.pdf"}}]}
    kg = {"nodes": [{"evidence_span": [{"document_id": "n.pdf"}]}],
          "edges": [{"evidence_span": [{"document_id": "e.pdf"}]}]}
    assert layers_store.collect_document_ids(doc, kg) == [
        "z.pdf", "c.pdf", "n.pdf", "e.pdf"]


def test_qa_sources_show_file_names_not_opaque_ids(store) -> None:
    """裁定 V の狙いそのもの: QA の出典が不透明な id ではなくファイル名になる。

    OTHER の根拠は document_id="doc-9911" だが、対応表があるので
    「旧レポート.pdf」で出る。TARGET 側 (対応表に無い id) は生のまま。
    """
    from cc_orchestrator import qa

    material = qa.local_material(store, ["次元削減"])
    docs = {s["document_id"] for s in material.sources.values() if s["document_id"]}
    assert "旧レポート.pdf" in docs
    assert "doc-9911" not in docs


def test_qa_sources_keep_raw_id_when_no_table_exists(tmp_path: Path) -> None:
    """対応表の無い過去セッションは従来表示のまま (遡及変換はしない)。"""
    from cc_orchestrator import qa

    graphs = tmp_path / "graphs"
    write_session(graphs, OTHER,
                  nodes=[{"id": "n1", "label": "次元削減", "community_id": "comm_a"}],
                  edges=[{"id": "e1", "from": "n1", "to": "n1", "glyph": "wave",
                          "evidence_span": [{"document_id": "409",
                                             "surface": "次元削減の話"}]}])
    material = qa.local_material(SessionStore(graphs), ["次元削減"])
    docs = {s["document_id"] for s in material.sources.values() if s["document_id"]}
    assert docs == {"409"}


def test_analysis_attaches_documents_at_generation_time() -> None:
    """生成時に対応表が付く (裁定 V)。名前は取込 + source_files から引く。"""
    from cc_orchestrator import analysis

    kg = {"nodes": [{"id": "c1", "label": "甲",
                     "evidence_span": [{"document_id": "報告", "surface": "甲の話"}]}],
          "edges": [], "source_files": ["報告.pdf"]}
    doc, _ = analysis.analyze(lambda p: json.dumps({"labels": []}),
                              session="s1", kg=kg, docs=[])
    assert doc["documents"] == {"報告": "報告.pdf"}


# =============================================== 6. Web / CLI


@pytest.fixture()
def client(corpus: Path, tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient

    from cc_web.app import create_app

    monkeypatch.chdir(tmp_path)          # graphs/ を cwd 相対で見せる
    with TestClient(create_app()) as tc:
        yield tc


def test_web_gap_report_returns_json_and_saves_files(client, tmp_path,
                                                     monkeypatch) -> None:
    # LLM を呼ばせない (テストからネットワークへ出ない)
    monkeypatch.setattr("cc_web.sessions.gap_report.build_gap_report",
                        lambda s, store, **kw: build_gap_report(s, store))
    res = client.post(f"/api/sessions/{TARGET}/gap-report")
    assert res.status_code == 200
    body = res.json()
    assert body["session"] == TARGET
    assert body["counts"] == {"structural": 1, "discourse": 1, "causal": 1}
    assert (tmp_path / "exports" / f"gap_report_{TARGET}.json").exists()
    assert (tmp_path / "exports" / f"gap_report_{TARGET}.md").exists()


def test_web_gap_report_unknown_session_is_404(client) -> None:
    res = client.post("/api/sessions/20990101_000000/gap-report")
    assert res.status_code == 404
    assert "error" in res.json()


def test_web_gap_report_download_needs_generation_first(client, monkeypatch) -> None:
    """未生成なら 404 (作ってから落とす)。生成後は JSON が返る。"""
    assert client.get(f"/api/sessions/{TARGET}/gap-report").status_code == 404
    monkeypatch.setattr("cc_web.sessions.gap_report.build_gap_report",
                        lambda s, store, **kw: build_gap_report(s, store))
    client.post(f"/api/sessions/{TARGET}/gap-report")
    got = client.get(f"/api/sessions/{TARGET}/gap-report")
    assert got.status_code == 200 and got.json()["session"] == TARGET


def test_web_gap_report_runs_on_the_exclusive_worker(client, monkeypatch) -> None:
    """run_exclusive に載っている = 生成ジョブと同時に走らない (直列化)。"""
    seen: list[str] = []
    original = type(client.app.state.jobs).run_exclusive

    def spy(self, fn, *args, **kwargs):
        seen.append(getattr(fn, "__name__", ""))
        return original(self, fn, *args, **kwargs)

    monkeypatch.setattr(type(client.app.state.jobs), "run_exclusive", spy)
    monkeypatch.setattr("cc_web.sessions.gap_report.build_gap_report",
                        lambda s, store, **kw: build_gap_report(s, store))
    client.post(f"/api/sessions/{TARGET}/gap-report")
    assert seen == ["build_gap_report"]


def test_cli_gap_report_prints_markdown_and_saves(corpus: Path, tmp_path: Path) -> None:
    """CLI は Markdown を標準出力に出し、exports/ に 2 ファイル残す。"""
    res = subprocess.run(
        [sys.executable, "-m", "cc_orchestrator.chat", "--gap-report", TARGET,
         "--no-llm"],
        cwd=tmp_path, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(PRODUCTION / "src"),
             "HOME": str(tmp_path)})
    assert res.returncode == 0, res.stderr
    assert "# ギャップレポート" in res.stdout
    assert "## 構造ギャップ" in res.stdout
    assert (tmp_path / "exports" / f"gap_report_{TARGET}.md").exists()


# =============================================== 7. 裁定 U: 疑問形の拡充


@pytest.mark.parametrize("message,expected", [
    # 裁定 U が拾えるようにした語尾 (地図語 + QA cue + 疑問形 + 動詞なし)
    ("概念マップの全体像はどうなっていますか", "global"),
    ("概念地図の全体像はどうなっていますか", "global"),
    ("概念地図とNV中心の関係はどうなっていますか", "local"),
    ("概念地図の原因は何でしょうか", "local"),
    ("概念地図の関係はどう変わりましたか", "local"),
])
def test_arbitration_u_polite_question_forms_reach_qa(message, expected) -> None:
    from cc_orchestrator.routing import route

    assert route(message).route == expected


@pytest.mark.parametrize("message,expected", [
    # 3 条件ガードは不変: 地図語が無い / 疑問形でない / 生成動詞がある
    ("実験は何件ありましたか", "vector"),
    ("今週の研究を概念地図として整理して", "map"),
    ("概念地図の関係を整理してくれますか?", "map"),
    ("概念地図を作成していただけますでしょうか", "map"),
    # 「図にして」は生成動詞。丁寧に頼んでも地図依頼のまま
    ("全体像を図にしてもらえますか", "map"),
    ("概念地図とは何ですか", "map"),
])
def test_arbitration_u_keeps_the_three_condition_guard(message, expected) -> None:
    from cc_orchestrator.routing import route

    assert route(message).route == expected


def test_arbitration_u_question_cues_are_additive() -> None:
    """既存の合図を消していない (足しただけ)。"""
    from cc_orchestrator.routing import QUESTION_CUES, is_question

    assert {"教えて", "ですか"} <= set(QUESTION_CUES)
    assert {"ますか", "ましたか", "でしょうか"} <= set(QUESTION_CUES)
    assert is_question("どうなっていますか") and is_question("何が分かりましたか")
    assert not is_question("概念地図を作る")
