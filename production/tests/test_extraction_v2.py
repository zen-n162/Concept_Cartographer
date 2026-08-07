"""抽出 v2 — 粒度を「指示」ではなく「仕組み」で保証する (裁定 AM/AN/AO/AP/AQ)。

設計書: production/docs/extraction-v2-design.md

v1 の問題は粒度そのものではなく**再現性**だった: 同じ依頼で概念 64 のときと
20 のときがある。指示 (「30〜80 個」) は守られないことがあり、指示を強くしても
再現性は買えない。v2 は初回抽出のあと**資料を 1 件ずつ回して追加抽出**する。

ここで固定するのは 4 点:
  裁定 AM  ループの停止条件 (target / dry 2 連続 / max_calls / cap)、
           資料 1 件ずつのプロンプト契約、**決定的な巡回順**
  裁定 AN  統合の 3 つの穴 (重複ノードの根拠 / エッジの意味的重複 / 上限カット)
  裁定 AO  水増しせずに「これ以上は増やせない」と注記する条件と、**出さない**条件
  裁定 AP  summary["extraction"] に何をしたかが全部残ること (予算 ≤6 call)
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from cc_core.detail import project
from cc_core.normalize import ENV_EXTRACT_MAX, merge_extraction
from cc_orchestrator import pipeline
from cc_orchestrator.ingest import Doc

FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------- 素材


def chain_kg(n: int, *, label: str = "概念", start: int = 1,
             source_files: list[str] | None = None) -> dict:
    """鎖状に繋がった n 概念の KG (レイアウトが成立する最小形)。"""
    nodes = [{"id": f"c{i:03d}", "label": f"{label}{i}", "community_id": "comm_001"}
             for i in range(start, start + n)]
    edges = [{"id": f"r{i:03d}", "from": nodes[i]["id"], "to": nodes[i + 1]["id"],
              "label": "関連", "glyph": "wave",
              "evidence_span": [{"document_id": "d1", "surface": "原文"}]}
             for i in range(len(nodes) - 1)]
    kg = {"graph_version": "kg_t", "nodes": nodes, "edges": edges,
          "communities": [{"id": "comm_001", "name": "テーマ", "is_gap": False}]}
    if source_files is not None:
        kg["source_files"] = source_files
    return kg


EMPTY = {"nodes": [], "edges": [], "communities": []}

BASE = {
    "graph_version": "kg_base",
    "source_files": ["A.docx"],
    "nodes": [{"id": "c001", "label": "AI手法", "community_id": "comm_001",
               "evidence_span": [{"document_id": "A", "surface": "手法A を用いた"}]},
              {"id": "c002", "label": "評価指標", "community_id": "comm_001"},
              {"id": "c003", "label": "被験者実験", "community_id": "comm_002"}],
    "edges": [{"id": "r001", "from": "c001", "to": "c002", "label": "評価される",
               "glyph": "wave",
               "evidence_span": [{"document_id": "A", "surface": "手法を指標で評価"}]}],
    "communities": [{"id": "comm_001", "name": "手法", "is_gap": False},
                    {"id": "comm_002", "name": "実験", "is_gap": False}],
}


def local_doc(name: str, text: str = "学習率は 0.001 とした。") -> Doc:
    return Doc(name=name, source="local", modified=dt.datetime.now(), text=text)


# ============================================ 裁定 AN (統合の 3 つの穴)


def test_duplicate_node_keeps_its_evidence(monkeypatch) -> None:
    """(a) 重複ラベルでもノードは増やさないが、**根拠は既存へ寄せる**。

    v1 は断片ノードを丸ごと捨てていたので、2 資料目以降が同じ概念に付けた
    原文が失われていた (資料ごとに回す v2 では毎回起きる)。
    """
    monkeypatch.delenv(ENV_EXTRACT_MAX, raising=False)
    fragment = {"nodes": [{"id": "x1", "label": "ＡＩ手法",       # 全角 = 同じ概念
                           "evidence_span": [
                               {"document_id": "B", "surface": "手法A を再現した"},
                               {"document_id": "A", "surface": "手法A を用いた"}]}],
                "edges": [], "communities": []}
    merged, report = merge_extraction(BASE, fragment)

    assert len(merged["nodes"]) == 3 and report.duplicate_nodes == 1
    assert report.added_nodes == 0
    spans = merged["nodes"][0]["evidence_span"]
    # 既存 1 本 + 新規 1 本。既出の原文は**重複させない**
    assert [s["document_id"] for s in spans] == ["A", "B"]
    assert report.merged_evidence == 1


def test_duplicate_node_without_evidence_changes_nothing() -> None:
    """根拠を持たない重複ノードは、既存ノードに余計なキーを生やさない。"""
    fragment = {"nodes": [{"id": "x1", "label": "評価指標"}], "edges": []}
    merged, report = merge_extraction(BASE, fragment)

    assert report.duplicate_nodes == 1 and report.merged_evidence == 0
    assert "evidence_span" not in merged["nodes"][1]


def test_same_relation_is_folded_instead_of_duplicated() -> None:
    """(b) (from, to, glyph, 正規化ラベル) が同じなら平行エッジを増やさない。

    資料ごとに回すと同じ関係が何度も返る。v1 には意味的な重複排除が無く、
    同じ矢印が資料の数だけ重なって描かれていた。
    """
    fragment = {
        "nodes": [],
        "edges": [{"id": "e1", "from": "AI手法", "to": "評価指標",
                   "label": " 評価される ", "glyph": "wave",   # 前後空白だけ違う
                   "evidence_span": [{"document_id": "B", "surface": "B でも評価"}]}],
    }
    merged, report = merge_extraction(BASE, fragment)

    assert len(merged["edges"]) == 1              # 増えない
    assert report.merged_edges == 1 and report.added_edges == 0
    assert report.merged_evidence == 1
    docs = [s["document_id"] for s in merged["edges"][0]["evidence_span"]]
    assert docs == ["A", "B"]                      # 根拠は両方残る


def test_a_different_glyph_stays_a_separate_edge() -> None:
    """同じ 2 概念でも glyph が違えば**別の主張**なので畳まない。

    arrow=因果 / wave=相関 は層タグとして意味が違う。畳むと「因果とも相関とも
    言われている」という状態が消えて、後段の 3 点セット検査が空振りする。
    """
    fragment = {"nodes": [],
                "edges": [{"id": "e1", "from": "AI手法", "to": "評価指標",
                           "label": "評価される", "glyph": "arrow",
                           "evidence_span": [{"surface": "によって改善する"}]}]}
    merged, report = merge_extraction(BASE, fragment)

    assert len(merged["edges"]) == 2
    assert report.added_edges == 1 and report.merged_edges == 0
    assert {e["glyph"] for e in merged["edges"]} == {"wave", "arrow"}


def test_unknown_glyphs_fold_onto_the_same_wave_edge() -> None:
    """未知の glyph は normalize と同じく wave 扱いで突き合わせる。

    突き合わせだけ生値で行うと、'sparkle' が後で wave へ丸められた瞬間に
    同じエッジが 2 本になる (丸めの前後で判定が変わる穴)。
    """
    fragment = {"nodes": [],
                "edges": [{"id": "e1", "from": "AI手法", "to": "評価指標",
                           "label": "評価される", "glyph": "sparkle"}]}
    merged, report = merge_extraction(BASE, fragment)

    assert len(merged["edges"]) == 1 and report.merged_edges == 1


def test_fragment_internal_duplicate_edges_are_folded_too() -> None:
    """断片が同じ関係を 2 回書いてきても 1 本にする (索引に採用ぶんも入れる)。"""
    fragment = {"nodes": [{"id": "n1", "label": "学習率0.001"}],
                "edges": [{"id": "e1", "from": "n1", "to": "AI手法",
                           "label": "の設定値", "glyph": "double"},
                          {"id": "e2", "from": "n1", "to": "AI手法",
                           "label": "の設定値", "glyph": "double",
                           "evidence_span": [{"surface": "学習率は 0.001"}]}]}
    merged, report = merge_extraction(BASE, fragment)

    assert report.added_edges == 1 and report.merged_edges == 1
    assert len(merged["edges"]) == 2               # 既存 1 + 新規 1


def test_truncation_is_reported_not_silent() -> None:
    """(c) 上限カットは後着順のままでよいが、**切った事実を必ず出す**。"""
    fragment = {"nodes": [{"id": f"x{i}", "label": f"追加{i}"} for i in range(5)],
                "edges": []}
    merged, report = merge_extraction(BASE, fragment, max_nodes=5)

    assert len(merged["nodes"]) == 5
    assert report.truncated is True and report.capped_nodes == 3
    assert report.notes.get("上限超過で後着順にカット") == 3
    assert report.to_dict()["truncated"] is True


def test_no_truncation_leaves_the_flag_down() -> None:
    """上限に触れていないのに truncated が立たない (誤検知しない)。"""
    _, report = merge_extraction(BASE, {"nodes": [{"label": "追加"}], "edges": []})
    assert report.truncated is False and report.capped_nodes == 0


def test_existing_ids_survive_and_self_loops_still_die() -> None:
    """既存 id は保持し、自己ループは normalize の流儀で落として数も合わせる。"""
    fragment = {"nodes": [{"id": "n1", "label": "新概念"}],
                "edges": [{"id": "e1", "from": "n1", "to": "新概念",   # 自己ループ
                           "label": "自分", "glyph": "wave"}]}
    merged, report = merge_extraction(BASE, fragment)

    assert [n["id"] for n in merged["nodes"]][:3] == ["c001", "c002", "c003"]
    assert report.added_nodes == 1
    assert report.added_edges == 0                 # 足した数から引かれている
    assert len(merged["edges"]) == len(BASE["edges"])


# ================================================ 裁定 AM (ループの土台)


def test_document_roster_is_deterministic_and_deduplicated() -> None:
    """巡回順は「本文のある資料 → Work IQ 資料」で、重複は正規化ラベルで除く。

    `source_files` にはローカル添付も載るので、素の文字列一致だと同じ資料に
    2 call 使うことになる。全角/半角違いも同じ資料。
    """
    docs = [local_doc("研究メモ.md"), local_doc("実験ログ.md")]
    kg = chain_kg(3, source_files=["実験ログ.md", "提案書.docx",
                                   "ＡＩ資料.pdf", "AI資料.pdf"])

    roster = pipeline._document_roster(kg, docs, local_only=False)
    assert [s.name for s in roster] == [
        "研究メモ.md", "実験ログ.md", "提案書.docx", "ＡＩ資料.pdf"]
    assert [s.kind for s in roster] == ["local", "local", "workiq", "workiq"]
    # 何度呼んでも同じ (決定的)
    assert [s.name for s in pipeline._document_roster(kg, docs, local_only=False)] \
        == [s.name for s in roster]


def test_local_only_drops_documents_it_cannot_read() -> None:
    """local_only では本文の無い資料を回さない (読む手段が無いので空振りする)。"""
    kg = chain_kg(3, source_files=["提案書.docx"])
    roster = pipeline._document_roster(kg, [local_doc("研究メモ.md")],
                                       local_only=True)
    assert [s.name for s in roster] == ["研究メモ.md"]


def test_knobs_are_read_at_call_time(monkeypatch) -> None:
    """CC_DETAILED_TARGET / CC_EXPAND_MAX_CALLS は毎回読む (常駐 Web のため)。"""
    for env, fn, default in ((pipeline.ENV_DETAILED_TARGET,
                              pipeline.detailed_target, 45),
                             (pipeline.ENV_EXPAND_MAX_CALLS,
                              pipeline.expand_max_calls, 5)):
        monkeypatch.delenv(env, raising=False)
        assert fn() == default
        monkeypatch.setenv(env, "3")
        assert fn() == 3
        monkeypatch.setenv(env, "でたらめ")
        assert fn() == default              # 読めない値で 0 にしない


# ============================================ 裁定 AM (パイプラインのループ)


class FakeFoundry:
    """cc-extraction が台本どおりに返す代役 (ネットワークに出ない)。

    台本の 0 番目が初回抽出、以降が追加抽出 1 call ぶん。使い切ったあとは
    `default` (既定は「新規なし」) を返し続ける。要素に例外を置けばその call
    だけ失敗させられる。
    """

    def __init__(self, first: object, *fragments: object,
                 default: object = None) -> None:
        self.script: list[object] = [first, *fragments]
        self.default = EMPTY if default is None else default
        self.prompts: dict[str, list[str]] = {}

    def ensure_agent(self, name: str, *a: object, **k: object) -> str:
        return name

    def calls(self, agent: str) -> int:
        return len(self.prompts.get(agent, []))

    def run(self, agent: str, prompt: str, tool_executor: object = None,
            **kwargs: object) -> str:
        self.prompts.setdefault(agent, []).append(prompt)
        if agent == "cc-extraction":
            i = self.calls(agent) - 1
            payload = self.script[i] if i < len(self.script) else self.default
            if isinstance(payload, Exception):
                raise payload
            return json.dumps(payload, ensure_ascii=False)
        if agent == "cc-projection":
            return json.dumps({"status": "RENDER_OK", "created": 1})
        if agent == "cc-verification":
            return json.dumps({"verdict": "PASS", "missing": 0, "mismatched": 0,
                               "summary": "一致"}, ensure_ascii=False)
        raise AssertionError(f"予期しないエージェント呼び出し: {agent}")


class FakeExecutor:
    def __init__(self, target: str = "local") -> None:
        self.target = target
        self.authoritative_plan: dict | None = None

    def __call__(self, name: str, args: dict) -> dict:
        return {"success": True, "created": [], "mode": self.target, "passed": True}

    def tool_render_layout_plan(self, args: dict) -> dict:
        return {"success": True, "created": []}

    def export_excalidraw(self, out_path: str) -> str:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text("{}", encoding="utf-8")
        return out_path


@pytest.fixture
def online_run(tmp_path, monkeypatch):
    """online の map 経路をモックで 1 回まわす (Foundry / canvas を使わない)。"""
    monkeypatch.chdir(tmp_path)
    for env in (ENV_EXTRACT_MAX, pipeline.ENV_DETAILED_TARGET,
                pipeline.ENV_EXPAND_MAX_CALLS):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(pipeline, "ToolExecutor", FakeExecutor)

    def _run(client: FakeFoundry, *, docs: list[Doc] | None = None, **extra):
        docs = [local_doc("研究メモ.md")] if docs is None else docs
        monkeypatch.setattr(pipeline, "ingest", lambda message, paths: (docs, "今週"))
        monkeypatch.setattr(pipeline, "FoundryAgentsV2", lambda *a, **k: client)
        kwargs = {"target": "file", "local_only": True, "layers": False,
                  "verify_causal": False, "export_svg": False, "learned": False}
        kwargs.update(extra)
        return pipeline.run_pipeline(
            "今週の研究を概念地図として整理して", **kwargs)
    return _run


def test_loop_stops_when_the_target_is_reached(online_run, monkeypatch) -> None:
    """目標概念数に届いたら止まる (余分な call を投げない)。"""
    monkeypatch.setenv(pipeline.ENV_DETAILED_TARGET, "20")
    client = FakeFoundry(chain_kg(12),
                         chain_kg(10, label="下位", start=100))
    summary = online_run(client, docs=[local_doc("A.md"), local_doc("B.md")])

    info = summary["extraction"]
    assert info["stopped_by"] == "target"
    assert info["before"] == 12 and info["nodes"] == 22
    assert info["calls"] == 1 and info["rounds"] == 1
    assert client.calls("cc-extraction") == 2          # 初回 + 追加 1
    assert info["expanded"] is True


def test_loop_stops_after_two_dry_documents(online_run) -> None:
    """新規ゼロが 2 資料続いたら「この資料束からはもう出ない」と見なす。"""
    client = FakeFoundry(chain_kg(12), EMPTY, EMPTY)
    summary = online_run(client, docs=[local_doc("A.md"), local_doc("B.md")])

    info = summary["extraction"]
    assert info["stopped_by"] == "dry"
    assert info["calls"] == 2 and info["added_nodes"] == 0
    assert client.calls("cc-extraction") == 3
    assert [e["note"] for e in info["per_document"]] == ["新規なし", "新規なし"]


def test_a_productive_document_resets_the_dry_streak(online_run) -> None:
    """連続でなければ止めない (1 件空 → 当たり → 空 2 連続でようやく停止)。"""
    client = FakeFoundry(chain_kg(12), EMPTY,
                         chain_kg(5, label="下位", start=100), EMPTY, EMPTY)
    summary = online_run(client, docs=[local_doc("A.md"), local_doc("B.md"),
                                       local_doc("C.md")])

    info = summary["extraction"]
    assert info["stopped_by"] == "dry"
    assert info["calls"] == 4 and info["rounds"] == 2      # 2 周目へ入っている
    assert info["added_nodes"] == 5 and info["nodes"] == 17
    assert [e["added_nodes"] for e in info["per_document"]] == [0, 5, 0, 0]


def test_call_budget_caps_the_loop(online_run, monkeypatch) -> None:
    """CC_EXPAND_MAX_CALLS を超えて回さない。"""
    monkeypatch.setenv(pipeline.ENV_EXPAND_MAX_CALLS, "2")
    client = FakeFoundry(chain_kg(12), default=chain_kg(1, label="増", start=200))
    summary = online_run(client, docs=[local_doc("A.md"), local_doc("B.md"),
                                       local_doc("C.md")])

    info = summary["extraction"]
    assert info["stopped_by"] == "max_calls"
    assert info["calls"] == 2 and client.calls("cc-extraction") == 3


def test_zero_budget_disables_expansion_entirely(online_run, monkeypatch) -> None:
    """CC_EXPAND_MAX_CALLS=0 は費用の緊急ブレーキ (追加抽出を完全に止める)。"""
    monkeypatch.setenv(pipeline.ENV_EXPAND_MAX_CALLS, "0")
    client = FakeFoundry(chain_kg(12))
    summary = online_run(client)

    info = summary["extraction"]
    assert info["stopped_by"] == "max_calls" and info["calls"] == 0
    assert info["expanded"] is False and info["per_document"] == []
    assert client.calls("cc-extraction") == 1


def test_extraction_calls_never_exceed_six(online_run) -> None:
    """裁定 AP の予算: 抽出系は初回 1 + 追加 5 = 最大 6 call。

    目標に永遠に届かない (1 資料 1 概念しか出ない) 最悪ケースでも越えない。
    """
    client = FakeFoundry(chain_kg(12),
                         *[chain_kg(1, label=f"増{i}", start=200 + i)
                           for i in range(9)])
    summary = online_run(client, docs=[local_doc(f"{c}.md") for c in "ABC"])

    info = summary["extraction"]
    assert info["stopped_by"] == "max_calls"
    assert info["calls"] == 5 and client.calls("cc-extraction") == 6
    assert info["rounds"] == 2                       # 3 資料 x 2 周目の途中


def test_loop_stops_at_the_hard_node_cap(online_run, monkeypatch) -> None:
    """CC_EXTRACT_MAX に達したら止める (統合が切り始めたら回しても無駄)。"""
    monkeypatch.setenv(ENV_EXTRACT_MAX, "15")
    monkeypatch.setenv(pipeline.ENV_DETAILED_TARGET, "80")
    client = FakeFoundry(chain_kg(12), chain_kg(10, label="下位", start=100))
    summary = online_run(client, docs=[local_doc("A.md"), local_doc("B.md")])

    info = summary["extraction"]
    assert info["stopped_by"] == "cap" and info["nodes"] == 15
    assert info["per_document"][0]["merge"]["truncated"] is True
    assert client.calls("cc-extraction") == 2


def test_no_documents_is_not_a_crash(online_run) -> None:
    """資料 0 件でも地図は作る (回す先が無いだけ)。"""
    client = FakeFoundry(chain_kg(12))
    summary = online_run(client, docs=[])

    info = summary["extraction"]
    assert info["stopped_by"] == "no_documents"
    assert info["calls"] == 0 and info["documents"] == []
    assert summary["status"] == "success"
    assert summary["knowledge_graph"]["nodes"] == 12


def test_one_broken_document_does_not_lose_the_map(online_run, monkeypatch) -> None:
    """1 資料の事故で run を落とさず、落ちた事実は per_document に残す。"""
    monkeypatch.setenv(pipeline.ENV_DETAILED_TARGET, "20")
    client = FakeFoundry(chain_kg(12), ValueError("壊れた応答"),
                         chain_kg(10, label="下位", start=100))
    summary = online_run(client, docs=[local_doc("A.md"), local_doc("B.md")])

    info = summary["extraction"]
    assert summary["status"] == "success" and info["nodes"] == 22
    assert "ValueError" in info["per_document"][0]["error"]
    assert info["per_document"][0]["added_nodes"] == 0
    assert info["stopped_by"] == "target"


def test_per_document_records_every_call(online_run) -> None:
    """裁定 AP: 何を・どの順で・どれだけ増やしたかが全部残る。"""
    client = FakeFoundry(chain_kg(12), chain_kg(4, label="下位", start=100), EMPTY,
                         EMPTY)
    summary = online_run(client, docs=[local_doc("A.md"), local_doc("B.md")])

    info = summary["extraction"]
    assert set(info) >= {"rounds", "calls", "added_nodes", "added_edges",
                         "stopped_by", "per_document", "documents", "target"}
    first = info["per_document"][0]
    assert first["document"] == "A.md" and first["source"] == "local"
    assert first["round"] == 1 and first["added_nodes"] == 4
    assert first["nodes_after"] == 16
    assert first["merge"]["added_nodes"] == 4
    assert info["added_nodes"] == 4 and info["added_edges"] == first["added_edges"]


def test_the_prompt_targets_exactly_one_local_document(online_run) -> None:
    """ローカル資料は本文をそのまま渡し、対象を 1 件に絞る。"""
    client = FakeFoundry(chain_kg(12), EMPTY, EMPTY)
    online_run(client, docs=[local_doc("研究メモ.md", "学習率は 0.001 とした。"),
                             local_doc("実験ログ.md", "F1 は 0.82 であった。")])

    prompt = client.prompts["cc-extraction"][1]
    assert "資料「研究メモ.md」1 件だけ" in prompt
    assert "学習率は 0.001 とした。" in prompt         # 本文を渡す
    assert "F1 は 0.82 であった。" not in prompt       # 他の資料は混ぜない
    assert "概念1" in prompt and "概念12" in prompt    # 既存ラベルは全部渡す
    assert "この資料に無いものを足さないでください" in prompt
    assert "evidence_span" in prompt


def test_the_prompt_asks_work_iq_to_reread_one_named_file(online_run) -> None:
    """Work IQ 資料は本文が届かないので、名前で 1 件だけ読み直させる。"""
    client = FakeFoundry(chain_kg(12, source_files=["提案書.docx"]), EMPTY, EMPTY)
    online_run(client, docs=[], local_only=False)

    prompt = client.prompts["cc-extraction"][1]
    assert "資料「提案書.docx」1 件だけ" in prompt
    assert "Work IQ ツールで**「提案書.docx」だけ**を読み直し" in prompt
    assert "ほかの資料は読まないでください" in prompt


def test_kg_file_never_expands(tmp_path, monkeypatch) -> None:
    """裁定 AQ: offline / kg_file では発動しない (資料が手元に無い)。"""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "kg.json"
    path.write_text(json.dumps(chain_kg(12), ensure_ascii=False), encoding="utf-8")

    summary = pipeline.run_pipeline(
        "今週の研究を概念地図として整理して", target="file", kg_file=str(path),
        offline=True, layers=False, verify_causal=False, export_svg=False)

    info = summary["extraction"]
    assert info["mode"] == "kg_file" and info["stopped_by"] == "kg_file"
    assert info["calls"] == 0 and info["per_document"] == []
    assert summary["knowledge_graph"]["nodes"] == 12


# ================================================ 裁定 AO (正直な上限表示)


def test_detail_note_appears_for_a_thin_document_set(tmp_path, monkeypatch) -> None:
    """資料が薄くて Standard == Detailed なら、水増しせず注記を出す。

    受け入れ基準 4: 概念 19 のセッション (kg_sample) を kg_file で流す。
    """
    monkeypatch.chdir(tmp_path)
    src = json.loads((FIXTURES / "kg_sample.json").read_text(encoding="utf-8"))
    path = tmp_path / "kg.json"
    path.write_text(json.dumps(src, ensure_ascii=False), encoding="utf-8")

    summary = pipeline.run_pipeline(
        "今週の研究を概念地図として整理して", target="file", kg_file=str(path),
        offline=True, layers=False, verify_causal=False, export_svg=False)

    levels = summary["levels"]
    assert levels["standard"]["nodes"] == levels["detailed"]["nodes"] == 19
    assert summary["detail_note"] == pipeline.DETAIL_NOTE

    # plan にも載り、どのレベルの view にも付いてくる (Web が読むのは view)
    plan = json.loads(
        (tmp_path / "graphs" /
         f"layout_plan_session_{summary['session']}.json").read_text(encoding="utf-8"))
    assert plan["detail_note"] == pipeline.DETAIL_NOTE
    assert project(plan, "overview")["detail_note"] == pipeline.DETAIL_NOTE


def test_detail_note_reaches_the_web_view_and_the_level_card(tmp_path,
                                                             monkeypatch) -> None:
    """Web は plan から作った view しか見ない — そこまで届いて初めて表示になる。"""
    monkeypatch.chdir(tmp_path)
    src = (FIXTURES / "kg_sample.json").read_text(encoding="utf-8")
    (tmp_path / "kg.json").write_text(src, encoding="utf-8")
    summary = pipeline.run_pipeline(
        "今週の研究を概念地図として整理して", target="file",
        kg_file=str(tmp_path / "kg.json"), offline=True, layers=False,
        verify_causal=False, export_svg=False)

    from cc_web import sessions
    view = sessions.view_of(summary["session"], "standard")
    assert view["detail_note"] == pipeline.DETAIL_NOTE

    # 詳細度カードが view の値をそのまま出す口を持っていること
    static = Path(__file__).resolve().parents[1] / "src" / "cc_web" / "static"
    app_js = (static / "app.js").read_text(encoding="utf-8")
    assert "state.view.detail_note" in app_js
    assert 'span.className = "lv-note"' in app_js
    assert ".lv-note {" in (static / "app.css").read_text(encoding="utf-8")


def test_view_without_a_note_returns_an_empty_string(tmp_path, monkeypatch) -> None:
    """注記が無いときもキーは返す (表示側にキーの有無の分岐を作らせない)。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "kg.json").write_text(
        json.dumps(chain_kg(60), ensure_ascii=False), encoding="utf-8")
    summary = pipeline.run_pipeline(
        "今週の研究を概念地図として整理して", target="file",
        kg_file=str(tmp_path / "kg.json"), offline=True, layers=False,
        verify_causal=False, export_svg=False)

    from cc_web import sessions
    assert "detail_note" not in summary
    assert sessions.view_of(summary["session"], "standard")["detail_note"] == ""


def test_no_detail_note_when_the_levels_actually_differ(online_run, monkeypatch) -> None:
    """三段に分化していれば注記は出ない (説明する必要が無い)。"""
    monkeypatch.setenv(pipeline.ENV_DETAILED_TARGET, "20")
    client = FakeFoundry(chain_kg(12), chain_kg(30, label="下位", start=100), EMPTY,
                         EMPTY)
    summary = online_run(client)

    levels = summary["levels"]
    assert levels["standard"]["nodes"] < levels["detailed"]["nodes"]
    assert "detail_note" not in summary


def test_no_detail_note_when_the_budget_ran_out(online_run, monkeypatch) -> None:
    """予算切れで止まったときは黙る — まだ資料に残っているかもしれない。

    ここで「上限です」と書くのは嘘になる (裁定 AO の「水増ししない」は
    数字だけでなく説明にも掛かる)。
    """
    monkeypatch.setenv(pipeline.ENV_EXPAND_MAX_CALLS, "1")
    client = FakeFoundry(chain_kg(12), EMPTY)
    summary = online_run(client)

    levels = summary["levels"]
    assert levels["standard"]["nodes"] == levels["detailed"]["nodes"] == 12
    assert summary["extraction"]["stopped_by"] == "max_calls"
    assert "detail_note" not in summary
