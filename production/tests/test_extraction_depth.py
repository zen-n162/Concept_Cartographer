"""抽出粒度 — Detailed で本当にノードが増えること (裁定 AD / AE)。

設計書: production/docs/detailed-extraction-design.md

ユーザー報告の症状は「Standard 19 / Detailed 19 と同数」。原因は抽出指示の
上限 (PoC 由来の「ノード 8〜20 個」) で、概念が 20 個以下しか出ないため
Standard の帯 (20-50) に全量が収まり、Detailed と差が出なかった。

守っているのは 3 点:
  裁定 AD  抽出指示が Detailed 粒度 (30〜80) を求めること。**同時に創作禁止**
           の一文が残っていること — 粒度を上げる指示だけが残って歯止めが
           消えると、増えたノードが資料に無い概念になる
  裁定 AE  それでも薄いときの深掘り 1 call と、その統合 (merge_extraction)
  記録     深掘りが起きたことは summary["extraction"]["expanded"] に必ず出る
           (黙ってノードを増やさない)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cc_core.detail import build_multilevel_plan
from cc_core.normalize import (
    EXTRACT_MAX_DEFAULT,
    ENV_EXTRACT_MAX,
    extract_max,
    merge_extraction,
)
from cc_core.validate import validate_layout_plan
from cc_orchestrator import pipeline
from cc_orchestrator.agents_def import EXTRACTION_INSTRUCTIONS
from cc_orchestrator.ingest import Doc


# --------------------------------------------------------------- 素材


def chain_kg(n: int, *, label: str = "概念", start: int = 1) -> dict:
    """鎖状に繋がった n 概念の KG (レイアウトが成立する最小形)。"""
    nodes = [{"id": f"c{i:03d}", "label": f"{label}{i}", "community_id": "comm_001"}
             for i in range(start, start + n)]
    edges = [{"id": f"r{i:03d}", "from": nodes[i]["id"], "to": nodes[i + 1]["id"],
              "label": "関連", "glyph": "wave",
              "evidence_span": [{"document_id": "d1", "surface": "原文"}]}
             for i in range(len(nodes) - 1)]
    return {"graph_version": "kg_t", "nodes": nodes, "edges": edges,
            "communities": [{"id": "comm_001", "name": "テーマ", "is_gap": False}]}


BASE = {
    "graph_version": "kg_base",
    "source_files": ["A.docx"],
    "nodes": [{"id": "c001", "label": "AI手法", "community_id": "comm_001"},
              {"id": "c002", "label": "評価指標", "community_id": "comm_001"},
              {"id": "c003", "label": "被験者実験", "community_id": "comm_002"}],
    "edges": [{"id": "r001", "from": "c001", "to": "c002", "label": "評価される",
               "glyph": "wave"}],
    "communities": [{"id": "comm_001", "name": "手法", "is_gap": False},
                    {"id": "comm_002", "name": "実験", "is_gap": False}],
}


# ================================================== 裁定 AD (抽出指示の粒度)


def test_extraction_instructions_ask_for_detailed_granularity() -> None:
    """PoC 由来の上限「8〜20 個」が復活していないこと (症状の直接原因)。"""
    assert "30〜80" in EXTRACTION_INSTRUCTIONS
    assert "8〜20" not in EXTRACTION_INSTRUCTIONS
    assert "3〜7" not in EXTRACTION_INSTRUCTIONS      # コミュニティは 4〜10 へ
    assert "4〜10" in EXTRACTION_INSTRUCTIONS


def test_extraction_instructions_keep_the_no_invention_guard() -> None:
    """粒度だけ上げて歯止めを外さない (裁定 AD の「不変」の部分)。"""
    assert "創作しない" in EXTRACTION_INSTRUCTIONS
    assert "資料に無いものを足すことではない" in EXTRACTION_INSTRUCTIONS
    assert "evidence_span" in EXTRACTION_INSTRUCTIONS


# ============================================ 裁定 AE (merge_extraction)


def test_duplicate_labels_are_merged_by_normalized_form() -> None:
    """NFKC + 大小無視で既出の概念は増やさない (id が違っても同じ概念)。"""
    fragment = {"nodes": [{"id": "c001", "label": "ＡＩ手法"},      # 全角
                          {"id": "c002", "label": "評価指標"},      # 完全一致
                          {"id": "c003", "label": "学習率0.001"}],  # 新規
                "edges": [], "communities": []}
    merged, report = merge_extraction(BASE, fragment)

    assert report.duplicate_nodes == 2 and report.added_nodes == 1
    assert [n["label"] for n in merged["nodes"]] == [
        "AI手法", "評価指標", "被験者実験", "学習率0.001"]


def test_fragment_ids_are_renumbered_without_collision() -> None:
    """断片の c001 と既存の c001 は別物。採番し直して既存を壊さない。"""
    fragment = {"nodes": [{"id": "c001", "label": "新概念A"},
                          {"id": "c002", "label": "新概念B"}],
                "edges": [], "communities": []}
    merged, report = merge_extraction(BASE, fragment)

    ids = [n["id"] for n in merged["nodes"]]
    assert len(ids) == len(set(ids)) == 5
    by_id = {n["id"]: n["label"] for n in merged["nodes"]}
    assert by_id["c001"] == "AI手法" and by_id["c002"] == "評価指標"
    assert report.added_nodes == 2


def test_edge_endpoints_are_resolved_by_label() -> None:
    """断片は既存概念を**ラベル**で指す (指示でそう書かせている)。"""
    fragment = {
        "nodes": [{"id": "c001", "label": "学習率0.001"}],
        "edges": [{"id": "r001", "from": "c001", "to": "AI手法",
                   "label": "の設定値", "glyph": "double",
                   "evidence_span": [{"surface": "学習率は 0.001 とした"}]}],
        "communities": []}
    merged, report = merge_extraction(BASE, fragment)

    assert report.added_edges == 1 and report.label_resolved == 1
    added = merged["edges"][-1]
    labels = {n["id"]: n["label"] for n in merged["nodes"]}
    assert labels[added["from"]] == "学習率0.001"
    assert added["to"] == "c001" and labels[added["to"]] == "AI手法"
    assert added["id"] != "r001"                     # 既存 r001 と衝突させない
    assert not report.dropped_edges


def test_unresolvable_edges_are_dropped_and_reported() -> None:
    """解決できない端点は normalize と同じ流儀で破棄 + 報告 (黙って捨てない)。"""
    fragment = {"nodes": [{"id": "c001", "label": "新概念"}],
                "edges": [{"id": "r009", "from": "c001", "to": "存在しない概念",
                           "label": "?", "glyph": "wave"}],
                "communities": []}
    merged, report = merge_extraction(BASE, fragment)

    assert report.added_edges == 0
    assert report.dropped_edges == ["r009"]
    assert len(merged["edges"]) == len(BASE["edges"])


def test_merge_caps_nodes_by_arrival_order() -> None:
    """上限超過は後着順で切る (この段に重要度はまだ無い)。"""
    fragment = {"nodes": [{"id": f"x{i}", "label": f"追加{i}"} for i in range(5)],
                "edges": [{"id": "r1", "from": "x3", "to": "x4", "label": "x",
                           "glyph": "wave"}],
                "communities": []}
    merged, report = merge_extraction(BASE, fragment, max_nodes=5)

    assert len(merged["nodes"]) == 5 and report.capped_nodes == 3
    assert [n["label"] for n in merged["nodes"]][:3] == [
        "AI手法", "評価指標", "被験者実験"]        # 先着 (既存) は残る
    assert report.capped_edges == 1                # 切られたノードのエッジ


def test_max_nodes_knob_is_read_at_call_time(monkeypatch) -> None:
    """CC_EXTRACT_MAX は呼び出しのたびに読む (常駐 Web を再起動させない)。"""
    monkeypatch.delenv(ENV_EXTRACT_MAX, raising=False)
    assert extract_max() == EXTRACT_MAX_DEFAULT == 100
    monkeypatch.setenv(ENV_EXTRACT_MAX, "4")
    assert extract_max() == 4
    monkeypatch.setenv(ENV_EXTRACT_MAX, "むちゃくちゃ")
    assert extract_max() == EXTRACT_MAX_DEFAULT     # 読めない値で 0 にしない

    monkeypatch.setenv(ENV_EXTRACT_MAX, "4")
    fragment = {"nodes": [{"label": f"追加{i}"} for i in range(5)],
                "edges": [], "communities": []}
    merged, report = merge_extraction(BASE, fragment)
    assert len(merged["nodes"]) == 4 and report.capped_nodes == 4


def test_merge_repairs_the_fragment_shape() -> None:
    """深掘り応答も指示どおりの形とは限らない (モック契約テスト)。

    実測済みの崩れ方を断片側で再現する: ノードが文字列 / evidence_span が
    単一オブジェクト / 未知の glyph。統合後は契約形になっていること。
    """
    fragment = {
        "nodes": ["裸のラベル", {"id": "n1", "label": "数値指標 F1=0.82"}],
        "edges": [{"id": "e1", "from": "n1", "to": "評価指標", "label": "の実測",
                   "glyph": "sparkle",           # 未知 -> wave (安全側)
                   "evidence_span": {"document_id": "d1",
                                     "surface": "F1 は 0.82 であった",
                                     "char_start": None, "char_end": None}}],
        "communities": [{"id": "comm_001", "name": "結果", "is_gap": False}],
        "source_files": ["B.pptx"],
    }
    merged, report = merge_extraction(BASE, fragment)

    added = merged["edges"][-1]
    assert added["glyph"] == "wave"
    assert isinstance(added["evidence_span"], list)
    assert "char_start" not in added["evidence_span"][0]
    assert any(k.startswith("normalize: ") for k in report.notes)
    assert "裸のラベル" in [n["label"] for n in merged["nodes"]]
    # 断片側のテーマは新しい島として足す (既存 comm_001「手法」に混ぜない)
    names = {c["name"] for c in merged["communities"]}
    assert {"手法", "実験", "結果"} <= names
    assert merged["source_files"] == ["A.docx", "B.pptx"]


def test_merged_graph_differentiates_the_three_levels() -> None:
    """統合後の 60 概念で 3 レベルが**三段に分化**する (受け入れ基準 2 の核)。

    症状の裏返し: 20 概念では overview=standard=detailed になっていた。
    """
    thin = chain_kg(20)
    thin_levels = build_multilevel_plan(thin)["levels"]
    assert thin_levels["standard"]["nodes"] == thin_levels["detailed"]["nodes"]

    fragment = chain_kg(40, label="下位概念", start=100)
    merged, report = merge_extraction(thin, fragment)
    assert report.added_nodes == 40 and len(merged["nodes"]) == 60

    plan = build_multilevel_plan(merged)
    assert validate_layout_plan(plan).valid
    counts = [plan["levels"][lv]["nodes"] for lv in ("overview", "standard", "detailed")]
    assert counts[0] < counts[1] < counts[2] == 60


def test_merge_rejects_non_dict_input() -> None:
    with pytest.raises(TypeError):
        merge_extraction("KG ではない", {"nodes": []})
    with pytest.raises(TypeError):
        merge_extraction(BASE, "断片ではない")


# ==================================== 裁定 AE (パイプラインでの発動条件)


class FakeFoundry:
    """cc-extraction が薄い KG を返す代役 (ネットワークに出ない)。"""

    def __init__(self, first: dict, fragment: dict | None = None) -> None:
        self.first, self.fragment = first, fragment
        self.prompts: dict[str, list[str]] = {}

    def ensure_agent(self, name: str, *a: object, **k: object) -> str:
        return name

    def calls(self, agent: str) -> int:
        return len(self.prompts.get(agent, []))

    def run(self, agent: str, prompt: str, tool_executor: object = None,
            **kwargs: object) -> str:
        self.prompts.setdefault(agent, []).append(prompt)
        if agent == "cc-extraction":
            if self.calls(agent) == 1:
                return json.dumps(self.first, ensure_ascii=False)
            if self.fragment is None:
                raise AssertionError("深掘りが呼ばれてはいけない run です")
            return json.dumps(self.fragment, ensure_ascii=False)
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
        return {"success": True, "created": [], "mode": self.target,
                "passed": True}

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
    monkeypatch.delenv(ENV_EXTRACT_MAX, raising=False)
    monkeypatch.delenv(pipeline.ENV_EXTRACT_MIN, raising=False)
    import datetime as dt

    doc = Doc(name="研究メモ.md", source="local", modified=dt.datetime.now(),
              text="学習率は 0.001 とした。F1 は 0.82 であった。")
    monkeypatch.setattr(pipeline, "ingest", lambda message, paths: ([doc], "今週"))
    monkeypatch.setattr(pipeline, "ToolExecutor", FakeExecutor)

    def _run(client: FakeFoundry, **extra):
        monkeypatch.setattr(pipeline, "FoundryAgentsV2", lambda *a, **k: client)
        summary = pipeline.run_pipeline(
            "今週の研究を概念地図として整理して", target="file", local_only=True,
            layers=False, verify_causal=False, export_svg=False, learned=False,
            **extra)
        return summary
    return _run


def test_shallow_extraction_triggers_one_deep_dive(online_run) -> None:
    """概念が閾値未満なら深掘りを **1 call だけ**足し、統合して記録する。"""
    client = FakeFoundry(chain_kg(12), fragment=chain_kg(20, label="下位", start=100))
    summary = online_run(client)

    info = summary["extraction"]
    assert info["expanded"] is True and info["before"] == 12
    assert info["nodes"] == 32 and info["min"] == 25
    assert info["merge"]["added_nodes"] == 20
    assert summary["knowledge_graph"]["nodes"] == 32
    assert client.calls("cc-extraction") == 2          # 初回 + 深掘り 1 回だけ
    levels = summary["levels"]
    assert levels["overview"]["nodes"] < levels["standard"]["nodes"] \
        < levels["detailed"]["nodes"]


def test_deep_dive_prompt_carries_labels_and_the_no_invention_line(online_run) -> None:
    """深掘りプロンプトの契約: 既出ラベルを渡し、創作を禁じる。"""
    client = FakeFoundry(chain_kg(12), fragment=chain_kg(20, label="下位", start=100))
    online_run(client)

    prompt = client.prompts["cc-extraction"][1]
    assert "概念1" in prompt and "概念12" in prompt      # 既出は渡す
    assert "資料に無いものを足さないでください" in prompt
    assert "20〜40" in prompt and "evidence_span" in prompt
    assert "研究メモ.md" in prompt                        # 資料も一緒に渡す


def test_sufficient_extraction_does_not_deepen(online_run) -> None:
    """十分に細かい抽出は 1 call のまま (常に 2 call にはしない)。"""
    client = FakeFoundry(chain_kg(30))                  # fragment なし
    summary = online_run(client)

    assert summary["extraction"] == {"mode": "llm", "nodes": 30, "min": 25,
                                     "expanded": False}
    assert client.calls("cc-extraction") == 1


def test_deep_dive_threshold_is_a_knob(online_run, monkeypatch) -> None:
    """CC_EXTRACT_MIN で閾値を下げれば発動しない (呼び出し時に読む)。"""
    monkeypatch.setenv(pipeline.ENV_EXTRACT_MIN, "10")
    client = FakeFoundry(chain_kg(12))                  # fragment なし
    summary = online_run(client)

    assert summary["extraction"]["expanded"] is False
    assert summary["extraction"]["min"] == 10
    assert client.calls("cc-extraction") == 1


def test_failed_deep_dive_still_produces_a_map(online_run) -> None:
    """深掘りが壊れても地図は作る (粒度の底上げは地図の前提ではない)。"""
    client = FakeFoundry(chain_kg(12), fragment={"nodes": []})   # 空の断片
    summary = online_run(client)

    assert summary["extraction"]["expanded"] is False
    assert "error" in summary["extraction"]
    assert summary["knowledge_graph"]["nodes"] == 12
    assert summary["status"] == "success"


def test_kg_file_path_never_deepens(tmp_path, monkeypatch) -> None:
    """offline / kg_file では発動しない (資料が手元に無く、LLM も呼べない)。"""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "kg.json"
    path.write_text(json.dumps(chain_kg(12), ensure_ascii=False), encoding="utf-8")

    summary = pipeline.run_pipeline(
        "今週の研究を概念地図として整理して", target="file", kg_file=str(path),
        offline=True, layers=False, verify_causal=False, export_svg=False)

    assert summary["extraction"] == {"mode": "kg_file", "nodes": 12,
                                     "expanded": False}
    assert summary["knowledge_graph"]["nodes"] == 12
