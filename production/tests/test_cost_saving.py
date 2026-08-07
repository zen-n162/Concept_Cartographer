"""トークン節約 — テストモードと恒久削減 (裁定 W / X / Y / Z)。

設計書: production/docs/cost-saving-design.md

読み方の軸は 2 つある:

  恒久削減 (裁定 W)  描画・検証プロンプトから plan 本文を外す。**通常実行が
                     そのぶん安くなる**話で、モードに依らず常に効く
  テストモード (X/Y) 同じ依頼の再実行で LLM を 1 回も呼ばない。**既定 OFF** で、
                     明示的に入れたときだけ働く

このファイルがいちばん強く守っているのは「既定 OFF」と「黙って再利用しない」の
2 点。前者が崩れると本番が古い結果を返し、後者が崩れると直したはずの挙動が
直っていないように見えて、キャッシュの存在ごと信用を失う。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from cc_core import test_cache, token_usage
from cc_orchestrator import pipeline as pipeline_mod

KG_FIXTURE = Path(__file__).parent / "fixtures" / "kg_sample.json"


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """相対パス (graphs/ logs/ exports/) を tmp へ寄せる。

    テストモードは環境変数でも入るので、**外側の CC_TEST_MODE を必ず消す** —
    開発者の shell 設定でテストの結論が変わらないようにするため。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(test_cache.ENV_FLAG, raising=False)
    monkeypatch.delenv(test_cache.ENV_TTL, raising=False)
    return tmp_path


# ============================================================ 裁定 W (恒久)


class RecordingClient:
    """cc-projection / cc-verification へ渡ったプロンプトを控えるだけの代役。"""

    def __init__(self) -> None:
        self.prompts: dict[str, list[str]] = {}
        self.tool_results: list[dict] = []
        self.usage = token_usage.blank()

    def ensure_agent(self, name: str, *args: object, **kwargs: object) -> str:
        return name

    def run(self, agent: str, prompt: str, tool_executor: object = None,
            **kwargs: object) -> str:
        self.prompts.setdefault(agent, []).append(prompt)
        if agent == "cc-projection":
            # エージェントは引数 {} で呼ぶ契約 (裁定 W)。実物と同じように
            # ツールを叩かせて、plan 無しでも描画が成立することを確かめる
            result = tool_executor("render_layout_plan", {})  # type: ignore[misc]
            self.tool_results.append(result)
            return json.dumps({"status": "RENDER_OK",
                               "created": len(result.get("created", []) or [])})
        if agent == "cc-verification":
            report = tool_executor("verify_scene", {})        # type: ignore[misc]
            self.tool_results.append(report)
            return json.dumps({"verdict": "PASS" if report.get("passed") else "FAIL",
                               "summary": "一致"}, ensure_ascii=False)
        raise AssertionError(f"予期しないエージェント呼び出し: {agent}")


@pytest.fixture
def render_run(workdir, monkeypatch):
    """⑧Project と検証だけを実物のツール実行系で回す (target=file)。"""
    from cc_orchestrator import pipeline

    def _run(**extra):
        client = RecordingClient()
        monkeypatch.setattr(pipeline, "FoundryAgentsV2", lambda *a, **k: client)
        summary = pipeline.run_pipeline(
            "今週の研究を概念地図として整理して", target="file",
            kg_file=str(KG_FIXTURE), verify_causal=False, export_svg=False,
            layers=False, **extra)
        return summary, client
    return _run


def test_projection_prompt_carries_no_plan(render_run) -> None:
    """描画プロンプトに plan 本文が載らない (裁定 W)。

    往路 (プロンプト) と復路 (ツール引数への復唱) で同じ JSON を 2 回課金する
    のをやめるのが裁定 W の中身。載っていないことを字面で確かめる。
    """
    _, client = render_run()
    prompt = client.prompts["cc-projection"][0]

    assert '"nodes"' not in prompt
    assert '"islands"' not in prompt
    assert "render_layout_plan" in prompt        # 何をすべきかは伝わっている
    assert len(prompt) < 300                     # 指示 1 つぶんの長さに収まる


def test_verification_prompt_carries_no_plan(render_run) -> None:
    """描画検証プロンプトにも plan 本文が載らない (裁定 W)。"""
    _, client = render_run()
    prompt = client.prompts["cc-verification"][0]

    assert '"nodes"' not in prompt
    assert '"islands"' not in prompt
    assert "verify_scene" in prompt
    assert len(prompt) < 300


def test_render_and_verify_work_without_any_echo(render_run) -> None:
    """復唱ゼロ (引数 {}) でも描画・検証が通る (裁定 W の前提)。

    描画対象は executor.authoritative_plan が持っているので、エージェントが
    plan を 1 バイトも返さなくても地図は出る。
    """
    summary, client = render_run()

    assert summary["projection"]["status"] == "RENDER_OK"
    assert summary["verification"]["verdict"] == "PASS"
    assert summary["status"] == "success"
    assert client.tool_results[0]["success"] is True
    assert client.tool_results[1]["passed"] is True


def test_projection_tool_does_not_require_a_plan_argument() -> None:
    """ツール宣言が plan を必須にしていない (裁定 W)。

    `required: ["plan"]` のままだと、モデルは plan 全体をツール引数へ書き写す
    **義務**を負う。指示文をいくら短くしても、ここが必須なら課金は消えない。
    """
    from cc_orchestrator.agents_def import PROJECTION_TOOLS, VERIFICATION_TOOLS

    render = next(t for t in PROJECTION_TOOLS if t["name"] == "render_layout_plan")
    assert render["parameters"]["required"] == []
    assert "plan" not in render["parameters"]["properties"]

    verify = next(t for t in VERIFICATION_TOOLS if t["name"] == "verify_scene")
    assert verify["parameters"]["required"] == []
    assert "plan" not in verify["parameters"]["properties"]


def test_agent_instructions_no_longer_expect_a_plan() -> None:
    """指示文が「plan を受け取る」前提でなくなっている (裁定 W)。"""
    from cc_orchestrator.agents_def import (
        PROJECTION_INSTRUCTIONS, VERIFICATION_INSTRUCTIONS)

    assert "確定済み" in PROJECTION_INSTRUCTIONS
    assert "引数 {} で" in PROJECTION_INSTRUCTIONS
    assert "入力の layout_plan を" not in PROJECTION_INSTRUCTIONS
    assert "確定済み" in VERIFICATION_INSTRUCTIONS
    # task 分岐 (NLI 等) の契約は壊さない
    assert '# task: "nli"' in VERIFICATION_INSTRUCTIONS


# ====================================================== 裁定 X: キーと期限


def test_key_ignores_whitespace_and_width() -> None:
    """NFKC + trim + 連続空白の畳み込みで同一視する (設計 §1)。

    畳み込むのは空白の**連なり**であって、空白そのものを消すわけではない
    (消すと「概念 地図」と「概念地図」が同じ依頼になってしまう)。
    """
    base = test_cache.make_key("今週の研究を 概念地図として整理して")

    assert test_cache.make_key("  今週の研究を 概念地図として整理して  ") == base
    assert test_cache.make_key("今週の研究を   概念地図として整理して") == base
    assert test_cache.make_key("今週の研究を　概念地図として整理して") == base  # 全角
    assert test_cache.make_key("今週の研究を\t概念地図として整理して") == base
    # 空白の有無そのものは意味の違いなので、別キーのままにする
    assert test_cache.make_key("今週の研究を概念地図として整理して") != base


def test_key_separates_meaningfully_different_requests() -> None:
    """設定が違えば別キー (同じ文言でも別の地図になるため)。"""
    base = test_cache.make_key("今週の研究を整理して", level="standard")

    assert test_cache.make_key("今週の研究を整理して", level="overview") != base
    assert test_cache.make_key("今週の研究を整理して", level="standard",
                               target="file") != base
    assert test_cache.make_key("今週の研究を整理して", level="standard",
                               layers=False) != base
    assert test_cache.make_key("今週の研究を整理して", level="standard",
                               learned=False) != base
    assert test_cache.make_key("今週の研究を整理して", level="standard",
                               local_only=True) != base
    assert test_cache.make_key("今週の研究を整理して", level="standard",
                               paths=["inbox"]) != base
    assert test_cache.make_key("先週の研究を整理して", level="standard") != base


def test_key_separates_different_kg_files() -> None:
    """--kg だけ違う 2 本は別キー (設計書のキー一覧への追加ぶん)。

    同じ文言・同じ設定でも読む KG が違えば別の地図になる。ここが同じキーだと
    2 本目に 1 本目の地図が返り、索引が嘘をつく。
    """
    base = test_cache.make_key("整理して", kg_file="graphs/kg_session_A.json")
    other = test_cache.make_key("整理して", kg_file="graphs/kg_session_B.json")
    assert base != other


def test_lookup_respects_the_ttl(workdir) -> None:
    """期限切れは「無い」として扱う (設計 §1: 既定 360 分)。"""
    key = test_cache.make_key("今週の研究")
    old = dt.datetime.now() - dt.timedelta(minutes=400)
    test_cache.record(key, "map", message="今週の研究", session="S1", now=old)

    assert test_cache.lookup(key, ttl_min=360) is None       # 400 > 360
    hit = test_cache.lookup(key, ttl_min=500)                # 400 < 500
    assert hit is not None and hit.session == "S1"
    assert 395 <= hit.age_min <= 405


def test_ttl_comes_from_the_environment(workdir, monkeypatch) -> None:
    """CC_TEST_CACHE_TTL_MIN を読む。壊れた値は既定へ落として実行を止めない。"""
    assert test_cache.ttl_minutes() == test_cache.DEFAULT_TTL_MIN
    monkeypatch.setenv(test_cache.ENV_TTL, "30")
    assert test_cache.ttl_minutes() == 30
    monkeypatch.setenv(test_cache.ENV_TTL, "まいなす")
    assert test_cache.ttl_minutes() == test_cache.DEFAULT_TTL_MIN


def test_broken_index_is_treated_as_empty(workdir) -> None:
    """索引が壊れていても実行を殺さない (キャッシュは派生物)。"""
    path = test_cache.index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ これは JSON ではない", encoding="utf-8")

    assert test_cache.load_index() == {}
    assert test_cache.lookup(test_cache.make_key("なにか")) is None


def test_env_flag_enables_test_mode(monkeypatch) -> None:
    """CC_TEST_MODE=1 で入る。既定 OFF は崩さない。"""
    monkeypatch.delenv(test_cache.ENV_FLAG, raising=False)
    assert test_cache.enabled() is False
    assert test_cache.enabled(True) is True
    monkeypatch.setenv(test_cache.ENV_FLAG, "1")
    assert test_cache.enabled() is True
    monkeypatch.setenv(test_cache.ENV_FLAG, "0")
    assert test_cache.enabled() is False


# ============================================ 裁定 X: パイプラインでの再利用


@pytest.fixture
def offline_map(workdir):
    """offline で地図を 1 枚作る (LLM を呼ばずに素材を用意する)。"""
    from cc_orchestrator import pipeline

    def _run(**extra):
        return pipeline.run_pipeline(
            "今週の研究を概念地図として整理して", target="file",
            kg_file=str(KG_FIXTURE), offline=True, export_svg=False,
            layers=False, **extra)
    return _run


def test_summary_is_persisted_on_every_run(offline_map, workdir) -> None:
    """summary の保存は**常時** (テストモードに依らない・設計 §1)。"""
    summary = offline_map()
    path = workdir / "graphs" / f"summary_session_{summary['session']}.json"

    assert path.exists()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["session"] == summary["session"]
    assert saved["status"] == "success"
    assert saved["knowledge_graph"]["nodes"] > 0


def test_default_off_writes_no_index(offline_map, workdir) -> None:
    """フラグ無しでは索引を作らない (既定 OFF の担保・設計 §1)。"""
    offline_map()
    offline_map()

    assert not (workdir / "logs" / "test_cache").exists()


def test_second_run_reuses_the_first_map(offline_map, workdir) -> None:
    """同じ文言の 2 回目が前回の summary を返す (設計 §1)。"""
    first = offline_map(test_cache_mode=True)
    second = offline_map(test_cache_mode=True)

    assert second["session"] == first["session"]      # 新しい地図を作っていない
    assert second["cache"]["hit"] is True
    assert second["cache"]["from"] == first["session"]
    assert "♻" in second["cache"]["note"]
    assert second["tokens"]["calls"] == 0


def test_cache_hit_replays_the_render_deterministically(offline_map) -> None:
    """ヒット時も plan から描き直す (`--render` と同じ決定的経路・設計 §1)。

    target=local の canvas 再描画と同じ経路。ここでは file モードで代わりに
    確かめる (canvas を立てずに、plan がいまも描ける形かまで見る)。
    """
    offline_map(test_cache_mode=True)
    second = offline_map(test_cache_mode=True)

    render = second["cache"]["render"]
    assert render["state"] == pipeline_mod.RENDER_REUSED
    assert render["elements"] > 0
    assert render["target"] == "file"


def test_cache_hit_survives_a_dead_canvas(offline_map, monkeypatch) -> None:
    """canvas が落ちていても再利用そのものは成立する (設計 §1)。

    描き直せなかったことは `cache.render.ok=False` として**残す** — 黙って
    成功に見せると、古い絵を見ながら新しい結果だと思い込むことになる。
    """
    from cc_orchestrator import pipeline

    offline_map(test_cache_mode=True)

    def dead(*args: object, **kwargs: object) -> dict:
        raise ConnectionError("canvas に繋がりません")

    monkeypatch.setattr(pipeline.ToolExecutor, "tool_render_layout_plan", dead)
    second = offline_map(test_cache_mode=True)

    assert second["cache"]["hit"] is True          # 再利用は成立している
    assert second["cache"]["render"]["state"] == pipeline_mod.RENDER_FAILED
    assert "ConnectionError" in second["cache"]["render"]["error"]


def test_cache_hit_never_constructs_a_foundry_client(offline_map, monkeypatch) -> None:
    """ヒット時に FoundryAgentsV2 を生成しない (設計 §1「LLM ゼロを保証」)。

    生成した時点で Azure 認証と ensure_agents (ネットワーク) が走る。
    「LLM を呼ばない」は「クライアントを作らない」まで含めて初めて成立する。
    """
    from cc_orchestrator import foundry_v2

    offline_map(test_cache_mode=True)         # 1 回目で登録

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("キャッシュヒット時に FoundryAgentsV2 を作ってはいけない")

    monkeypatch.setattr(foundry_v2.FoundryAgentsV2, "__init__", boom)
    from cc_orchestrator import pipeline
    monkeypatch.setattr(pipeline, "FoundryAgentsV2", foundry_v2.FoundryAgentsV2)

    second = pipeline.run_pipeline(
        "今週の研究を概念地図として整理して", target="file",
        kg_file=str(KG_FIXTURE), export_svg=False, layers=False,
        test_cache_mode=True)                 # offline=False でも呼ばれない

    assert second["cache"]["hit"] is True


# 通常実行と再利用の見分けは **summary["cache"] の有無**で行う。session ID は
# `strftime("%Y%m%d_%H%M%S")` なので、同じ秒に走った 2 本は同じ ID になり、
# 「別の実行だったか」の判定には使えない (テストは 1 秒かからず終わる)。


def _ran_normally(summary: dict) -> bool:
    """再利用ではなく通常実行だったか (地図を作り直したか)。"""
    return "cache" not in summary and "knowledge_graph" in summary


def test_different_wording_is_not_reused(offline_map) -> None:
    """文言が変われば通常実行 (別キー)。"""
    from cc_orchestrator import pipeline

    offline_map(test_cache_mode=True)
    other = pipeline.run_pipeline(
        "先月の実験結果を概念地図にして", target="file",
        kg_file=str(KG_FIXTURE), offline=True, export_svg=False,
        layers=False, test_cache_mode=True)

    assert _ran_normally(other)


def test_expired_entry_falls_through_to_a_normal_run(offline_map, monkeypatch) -> None:
    """期限切れは通常実行 + 上書き (設計 §1)。"""
    offline_map(test_cache_mode=True)
    monkeypatch.setattr(test_cache, "ttl_minutes", lambda: -1)  # 必ず期限切れ

    second = offline_map(test_cache_mode=True)
    assert _ran_normally(second)


def test_missing_material_falls_back_to_a_normal_run(offline_map, workdir) -> None:
    """索引にあっても素材が消えていれば通常実行 (「無い地図」を返さない)。"""
    first = offline_map(test_cache_mode=True)
    (workdir / "graphs" / f"summary_session_{first['session']}.json").unlink()

    second = offline_map(test_cache_mode=True)
    assert _ran_normally(second)


def test_failed_runs_are_not_recorded(workdir, monkeypatch) -> None:
    """失敗した実行は登録しない (設計 §1「ミス時」)。"""
    from cc_orchestrator import pipeline

    def explode(*args: object, **kwargs: object) -> dict:
        raise RuntimeError("描画に失敗しました")

    monkeypatch.setattr(pipeline.ToolExecutor, "tool_render_layout_plan", explode)
    with pytest.raises(RuntimeError):
        pipeline.run_pipeline(
            "今週の研究を概念地図として整理して", target="file",
            kg_file=str(KG_FIXTURE), offline=True, export_svg=False,
            layers=False, test_cache_mode=True)

    assert test_cache.load_index() == {}


# ================================================= 裁定 X: QA 経路の再利用


class FakeQAClient:
    """cc-analysis に QA を答えさせる代役 (llm_calls > 0 を作る)。"""

    def __init__(self) -> None:
        self.calls = 0
        self.usage = token_usage.blank()

    def ensure_agent(self, name: str, *args: object, **kwargs: object) -> str:
        return name

    def run(self, agent: str, prompt: str, **kwargs: object) -> str:
        self.calls += 1
        token_usage.add_response(self.usage,
                                 {"input_tokens": 100, "output_tokens": 20})
        return json.dumps({"answer": "SuperPCA はスーパーピクセルを使います。",
                           "cited": [], "insufficient": False},
                          ensure_ascii=False)


def test_qa_answer_is_reused_with_its_sources(offline_map, monkeypatch) -> None:
    """QA の answer / sources を丸ごと再利用する (設計 §1)。

    先に地図を 1 枚作って索引に材料を入れる — 材料ゼロだと QA は LLM を
    呼ばずに「見つかりません」を返し、そもそも登録対象にならないため。
    """
    from cc_orchestrator import pipeline

    offline_map()                              # 材料づくり (LLM なし)
    client = FakeQAClient()
    monkeypatch.setattr(pipeline, "FoundryAgentsV2", lambda *a, **k: client)

    def ask() -> dict:
        return pipeline.run_pipeline("概念マップと動的グラフの関係は?",
                                     target="file", test_cache_mode=True)

    first = ask()
    assert first["status"] == "answered"
    assert client.calls == 1, "材料が集まらず LLM が呼ばれていない"
    assert first["qa"]["llm_calls"] == 1

    second = ask()
    assert second["status"] == "answered"
    assert second["answer"] == first["answer"]
    assert second["sources"] == first["sources"]
    assert second["cache"]["hit"] is True
    assert client.calls == 1                   # 2 回目は LLM を呼んでいない
    assert second["tokens"]["calls"] == 0


def test_answers_without_llm_calls_are_not_cached(workdir) -> None:
    """材料不足の答えは登録しない (索引を直した直後も古い答えを返さないため)。"""
    from cc_orchestrator import pipeline

    summary = pipeline.run_pipeline("存在しない概念XYZについて教えて",
                                    target="file", offline=True,
                                    test_cache_mode=True)
    assert summary["status"] == "answered"
    assert test_cache.load_index() == {}


# ================================================= 裁定 Y: 取込キャッシュ


def test_ingest_key_separates_paths_and_scope() -> None:
    """期間 + paths + local_only で分ける (設計 §2)。"""
    base = test_cache.ingest_key("今週 (月曜以降)", paths=["inbox"])

    assert test_cache.ingest_key("今週 (月曜以降)", paths=["inbox"]) == base
    assert test_cache.ingest_key("今週 (月曜以降)", paths=["other"]) != base
    assert test_cache.ingest_key("先週以降", paths=["inbox"]) != base
    assert test_cache.ingest_key("今週 (月曜以降)", paths=["inbox"],
                                 local_only=True) != base
    # 並び順は結論を変えない (sorted(paths))
    assert (test_cache.ingest_key("今週 (月曜以降)", paths=["b", "a"])
            == test_cache.ingest_key("今週 (月曜以降)", paths=["a", "b"]))


def test_ingest_is_reused_in_test_mode(workdir, monkeypatch) -> None:
    """同じ期間・同じ paths なら取込をやり直さない (裁定 Y)。"""
    from cc_orchestrator import pipeline

    seen = {"n": 0}

    def fake_ingest(message: str, paths: list[str]):
        seen["n"] += 1
        return ([pipeline.Doc(name="a.md", source="local",
                              modified=dt.datetime(2026, 8, 1), text="本文")],
                "今週 (月曜以降)")

    monkeypatch.setattr(pipeline, "ingest", fake_ingest)

    docs, window, info = pipeline._ingest_stage(
        "今週の研究", [], local_only=False, use_cache=True)
    assert seen["n"] == 1 and info is None and len(docs) == 1

    docs2, window2, info2 = pipeline._ingest_stage(
        "今週の研究", [], local_only=False, use_cache=True)
    assert seen["n"] == 1                       # 2 回目は取りに行っていない
    assert info2["hit"] is True and "♻" in info2["note"]
    assert [d.name for d in docs2] == ["a.md"]
    assert docs2[0].text == "本文" and window2 == window


def test_ingest_cache_is_never_used_in_normal_mode(workdir, monkeypatch) -> None:
    """通常モードでは読みも書きもしない (裁定 Y)。"""
    from cc_orchestrator import pipeline

    seen = {"n": 0}

    def fake_ingest(message: str, paths: list[str]):
        seen["n"] += 1
        return ([], "今週 (月曜以降)")

    monkeypatch.setattr(pipeline, "ingest", fake_ingest)
    pipeline._ingest_stage("今週の研究", [], local_only=False, use_cache=False)
    pipeline._ingest_stage("今週の研究", [], local_only=False, use_cache=False)

    assert seen["n"] == 2                       # 毎回取りに行く
    assert not (workdir / "logs" / "test_cache").exists()


# ============================================== 裁定 Z: 使用量の可視化


def test_usage_accumulates_from_real_field_names() -> None:
    """実応答のフィールド名で積む (2026-08-07 実測で確認済み)。"""
    totals = token_usage.blank()
    token_usage.add_response(totals, {
        "input_tokens": 989, "output_tokens": 31, "total_tokens": 1020,
        "input_tokens_details": {"cached_tokens": 128},
        "output_tokens_details": {"reasoning_tokens": 16}})
    token_usage.add_response(totals, {"input_tokens": 11, "output_tokens": 2})

    assert totals["input"] == 1000 and totals["output"] == 33
    assert totals["calls"] == 2 and totals["unknown"] == 0
    assert totals["cached_input"] == 128
    assert "reasoning_output" not in totals      # output の内数なので持たない


def test_missing_usage_is_reported_as_unknown() -> None:
    """usage が無い応答は「不明」。**0 を足して薄めない** (裁定 Z)。"""
    totals = token_usage.blank()
    token_usage.add_response(totals, None)
    token_usage.add_response(totals, {"prompt_tokens": 5})   # 別名は認めない

    assert totals["calls"] == 2 and totals["unknown"] == 2
    assert totals["input"] == 0 and totals["output"] == 0
    assert token_usage.is_unknown(totals) is True
    assert "不明" in token_usage.format_line(totals)


def test_format_line_distinguishes_zero_calls_from_unknown() -> None:
    """「呼んでいない」と「測れなかった」を別の文にする。"""
    assert "呼び出しなし" in token_usage.format_line(token_usage.blank())
    partial = {"input": 100, "output": 10, "calls": 3, "unknown": 1}
    line = token_usage.format_line(partial)
    assert "入力 100" in line and "1 call は 不明" in line


def test_client_counts_usage_per_response(monkeypatch) -> None:
    """_run_once が応答ごとに usage を積む (裁定 Z)。"""
    from cc_orchestrator import foundry_v2

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {"id": "r1", "status": "completed",
                    "output": [{"type": "message",
                                "content": [{"type": "output_text", "text": "OK"}]}],
                    "usage": {"input_tokens": 500, "output_tokens": 25}}

    client = foundry_v2.FoundryAgentsV2.__new__(foundry_v2.FoundryAgentsV2)
    client.responses_url = "https://example.invalid/openai/v1/responses"
    client.usage = token_usage.blank()
    client._http = type("H", (), {"post": lambda *a, **k: FakeResponse()})()
    monkeypatch.setattr(client, "_headers", lambda: {})

    assert client._run_once("cc-verification", "こんにちは") == "OK"
    assert client.usage == {**token_usage.blank(),
                            "input": 500, "output": 25, "calls": 1}


def test_summary_carries_token_counts(workdir, monkeypatch) -> None:
    """summary["tokens"] に実数が入る (受け入れ基準 3)。"""
    from cc_orchestrator import pipeline

    client = RecordingClient()
    token_usage.add_response(client.usage,
                             {"input_tokens": 4321, "output_tokens": 120})
    monkeypatch.setattr(pipeline, "FoundryAgentsV2", lambda *a, **k: client)

    summary = pipeline.run_pipeline(
        "今週の研究を概念地図として整理して", target="file",
        kg_file=str(KG_FIXTURE), verify_causal=False, export_svg=False,
        layers=False)

    assert summary["tokens"]["input"] == 4321
    assert summary["tokens"]["output"] == 120
    assert "🔢" in token_usage.format_line(summary["tokens"])


def test_usage_log_is_appended_and_aggregated(workdir) -> None:
    """logs/token_usage.jsonl に積んで日別に集計する (設計 §3)。"""
    token_usage.append_log("map", {"input": 100, "output": 10, "calls": 2},
                           session="S1", now=dt.datetime(2026, 8, 7, 10, 0))
    token_usage.append_log("map", {"input": 0, "output": 0, "calls": 0},
                           session="S1", cached=True,
                           now=dt.datetime(2026, 8, 7, 11, 0))
    token_usage.append_log("local", {"input": 50, "output": 5, "calls": 1},
                           now=dt.datetime(2026, 8, 8, 9, 0))

    rows = token_usage.read_log()
    assert len(rows) == 3 and rows[1]["cached"] is True

    report = token_usage.daily_report()
    assert [d["day"] for d in report["days"]] == ["2026-08-07", "2026-08-08"]
    assert report["days"][0]["input"] == 100
    assert report["days"][0]["cached_runs"] == 1
    assert report["total"]["input"] == 150 and report["total"]["calls"] == 3

    text = token_usage.format_report(report)
    assert "2026-08-07" in text and "150" in text
    assert "単価未設定" in text                   # 掛け算はしない


def test_report_converts_to_yen_only_when_both_prices_are_set(
        workdir, monkeypatch) -> None:
    """単価が揃ったときだけ円換算する (片方だけの推計はしない)。"""
    token_usage.append_log("map", {"input": 1000, "output": 1000, "calls": 1})

    monkeypatch.setenv(token_usage.ENV_PRICE_IN, "0.5")
    half = token_usage.format_report(token_usage.daily_report())
    assert "≈" not in half                      # 片方だけでは推計しない
    assert "単価未設定" in half

    monkeypatch.setenv(token_usage.ENV_PRICE_OUT, "2.0")
    text = token_usage.format_report(token_usage.daily_report())
    assert "≈ 2.5 円" in text                   # 1000/1000 × (0.5 + 2.0)


def test_cached_runs_are_logged_as_zero_calls(offline_map) -> None:
    """再利用も 0 call の行として残す (「使わずに済んだ回数」を測るため)。"""
    offline_map(test_cache_mode=True)
    offline_map(test_cache_mode=True)

    rows = token_usage.read_log()
    assert any(r.get("cached") for r in rows)
    assert all(r["calls"] == 0 for r in rows)       # offline なので全部 0 call


# ======================================================= CLI / Web の口


def test_cli_prints_the_reuse_banner_and_tokens(capsys) -> None:
    """CLI が「♻ 再利用」を冒頭に、使用量を最終行に出す (設計 §1・§3)。"""
    from cc_orchestrator import chat

    chat._print_summary({
        "status": "answered", "answer": "前回の答え",
        "routing": {"route": "local", "rationale": "近傍検索"},
        "cache": {"hit": True, "age_min": 3, "from": "S1",
                  "note": "♻ 前回の結果を再利用 (テストモード / 3 分前 / session S1)"},
        "tokens": token_usage.blank(),
    })
    out = capsys.readouterr().out

    assert "♻ 前回の結果を再利用" in out
    assert out.index("♻") < out.index("前回の答え")     # 答えより前に出る
    assert "LLM 呼び出しなし" in out


def test_cli_shows_the_ingest_reuse_line(capsys) -> None:
    """取込の再利用も黙って行わない (裁定 Y)。"""
    from cc_orchestrator import chat

    chat._print_summary({
        "status": "success", "session": "S1",
        "routing": {"route": "map"},
        "ingest": {"window": "今週 (月曜以降)", "workiq": "enabled",
                   "local_files": [],
                   "cache": {"hit": True, "age_min": 5,
                             "note": "♻ 資料は 5 分前の取込を再利用 (2 件)"}},
        "verification": {"verdict": "PASS"},
        "export": {}, "view": {},
    })
    assert "♻ 資料は 5 分前の取込を再利用" in capsys.readouterr().out


def test_web_job_body_carries_the_test_cache_flag(workdir, monkeypatch) -> None:
    """Web の jobs ボディが test_cache を run_pipeline まで運ぶ (設計 §1)。"""
    from fastapi.testclient import TestClient
    from cc_web import account, jobs as jobs_mod
    from cc_web.app import create_app

    monkeypatch.setattr(account, "_az_upn", lambda: "tester@example.com")
    account.clear_cache()
    seen: list[bool] = []

    def fake_pipeline(message: str, **kwargs):
        seen.append(kwargs.get("test_cache_mode"))
        return {"session": "S1", "status": "answered", "answer": "ok"}

    monkeypatch.setattr(jobs_mod, "run_pipeline", fake_pipeline)

    with TestClient(create_app()) as client:
        def submit(**extra) -> None:
            body = {"message": "今週の研究を整理して", "target": "file"}
            body.update(extra)
            res = client.post("/api/jobs", json=body)
            assert res.status_code == 202
            job_id = res.json()["job_id"]
            for _ in range(200):
                job = client.get(f"/api/jobs/{job_id}").json()
                if job["status"] in ("done", "error"):
                    return
            raise AssertionError("ジョブが終わりませんでした")

        submit()                        # 既定 = OFF
        submit(test_cache=True)

    assert seen == [False, True]
    account.clear_cache()
