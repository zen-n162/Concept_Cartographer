"""描画ハングの恒久対処 — 無限待ちしない / 黙って倒れない。

事故 (実測 2026-08-07): ライブ canvas と MCP gateway が夜間に落ち、gateway が
「ポートは応答するが SSE を返さない」半死になった。ExcalidrawClient.__aenter__ に
タイムアウトが無かったため Web のジョブが数時間 running のまま固まった。

ここで固定するのは 4 点:
  B  接続 + initialize に connect_timeout。超えたら開きかけを閉じて ConnectionError
  C  描画前にゲートウェイのヘルスを見る。落ちていたら file 生成へ倒し、理由を残す
  D  エージェント経由の描画に壁時計デッドライン。超えたら同じく file へ倒す
  E  履歴に running のまま残った行は、サーバ起動時に interrupted へ直す
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import types
import urllib.error
from pathlib import Path

import pytest

from cc_core import mcp_client
from cc_orchestrator import pipeline
from cc_orchestrator.ingest import Doc

# ============================================================ 素材 (モック)


def chain_kg(n: int = 6) -> dict:
    nodes = [{"id": f"c{i:03d}", "label": f"概念{i}", "community_id": "comm_001"}
             for i in range(1, n + 1)]
    edges = [{"id": f"r{i:03d}", "from": nodes[i]["id"], "to": nodes[i + 1]["id"],
              "label": "関連", "glyph": "wave",
              "evidence_span": [{"document_id": "d1", "surface": "原文"}]}
             for i in range(len(nodes) - 1)]
    return {"graph_version": "kg_t", "nodes": nodes, "edges": edges,
            "communities": [{"id": "comm_001", "name": "テーマ", "is_gap": False}]}


class FakeFoundry:
    """cc-extraction / cc-projection / cc-verification の代役 (通信しない)。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def ensure_agent(self, name: str, *a: object, **k: object) -> str:
        return name

    def run(self, agent: str, prompt: str, tool_executor: object = None,
            **kwargs: object) -> str:
        self.calls.append(agent)
        if agent == "cc-extraction":
            return json.dumps(chain_kg(), ensure_ascii=False)
        if agent == "cc-projection":
            return json.dumps({"status": "RENDER_OK", "created": 6})
        if agent == "cc-verification":
            return json.dumps({"verdict": "PASS", "summary": "一致"},
                              ensure_ascii=False)
        raise AssertionError(f"予期しないエージェント呼び出し: {agent}")


class FakeExecutor:
    """ToolExecutor の代役。target を記録するので倒れ先が見える。"""

    made: list[str] = []

    def __init__(self, target: str = "local") -> None:
        self.target = target
        self.authoritative_plan: dict | None = None
        FakeExecutor.made.append(target)

    def __call__(self, name: str, args: dict) -> dict:
        return {"success": True, "created": ["a", "b"], "mode": self.target,
                "passed": True, "canvas_element_count": 2,
                "expected_element_count": 2, "missing_elements": [],
                "label_mismatches": []}

    def export_excalidraw(self, out_path: str) -> str:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text("{}", encoding="utf-8")
        return out_path


@pytest.fixture
def online_run(tmp_path, monkeypatch):
    """online の map 経路をモックで 1 回まわす (Foundry / canvas を使わない)。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(pipeline.RENDER_DEADLINE_ENV, raising=False)
    FakeExecutor.made = []
    monkeypatch.setattr(pipeline, "ToolExecutor", FakeExecutor)
    monkeypatch.setattr(
        pipeline, "ingest",
        lambda message, paths: ([Doc(name="研究メモ.md", source="local",
                                     modified=dt.datetime.now(),
                                     text="学習率は 0.001 とした。")], "今週"))

    def _run(client: FakeFoundry, **extra):
        monkeypatch.setattr(pipeline, "FoundryAgentsV2", lambda *a, **k: client)
        kwargs = {"target": "local", "local_only": True, "layers": False,
                  "verify_causal": False, "export_svg": False, "learned": False}
        kwargs.update(extra)
        return pipeline.run_pipeline("今週の研究を概念地図として整理して", **kwargs)
    return _run


# ============================================ C: プリフライトと file フォールバック


def test_dead_gateway_falls_back_to_file(online_run, monkeypatch) -> None:
    """ゲートウェイが落ちていたら、描きに行かずファイル生成へ倒す。"""
    monkeypatch.setattr(pipeline, "gateway_healthy", lambda **k: False)
    client = FakeFoundry()
    summary = online_run(client)

    assert summary["render_fallback"] is True
    assert summary["render_note"] == pipeline.GATEWAY_DOWN_NOTE
    assert summary["projection"] == {"status": "RENDER_OK", "created": 2,
                                     "mode": "file"}
    assert summary["verification"]["verdict"] == "PASS"
    assert summary["status"] == "success"
    # 落ちていると分かっている相手にエージェントを走らせない
    assert "cc-projection" not in client.calls
    assert "cc-verification" not in client.calls
    # 倒れ先は file の executor (ライブ MCP を一切触らない)
    assert FakeExecutor.made == ["local", "file"]


def test_preflight_uses_three_second_timeout(online_run, monkeypatch) -> None:
    """プリフライトは 3 秒で切る (ここで待たされたら本末転倒)。"""
    seen: list[dict] = []

    def fake_health(**kwargs: object) -> bool:
        seen.append(dict(kwargs))
        return False

    monkeypatch.setattr(pipeline, "gateway_healthy", fake_health)
    online_run(FakeFoundry())
    assert seen == [{"timeout": 3.0}]


def test_healthy_gateway_keeps_the_agent_path(online_run, monkeypatch) -> None:
    """canvas が生きていれば従来どおり。勝手にフォールバックしない。"""
    monkeypatch.setattr(pipeline, "gateway_healthy", lambda **k: True)
    client = FakeFoundry()
    summary = online_run(client)

    assert "render_fallback" not in summary and "render_note" not in summary
    assert summary["projection"] == {"status": "RENDER_OK", "created": 6}
    assert client.calls.count("cc-projection") == 1
    assert client.calls.count("cc-verification") == 1
    assert FakeExecutor.made == ["local"]


def test_file_target_does_not_probe_the_gateway(online_run, monkeypatch) -> None:
    """target=file はもともと MCP を使わない。ヘルスを見に行かない。"""
    probed: list[bool] = []
    monkeypatch.setattr(pipeline, "gateway_healthy",
                        lambda **k: probed.append(True) or True)
    client = FakeFoundry()
    summary = online_run(client, target="file")

    assert probed == []
    assert "render_fallback" not in summary
    assert client.calls.count("cc-projection") == 1


def test_offline_path_is_unchanged(tmp_path, monkeypatch) -> None:
    """offline は従来どおり直接呼び。プリフライトもフォールバックも挟まない。"""
    monkeypatch.chdir(tmp_path)
    FakeExecutor.made = []
    monkeypatch.setattr(pipeline, "ToolExecutor", FakeExecutor)
    probed: list[bool] = []
    monkeypatch.setattr(pipeline, "gateway_healthy",
                        lambda **k: probed.append(True) or False)
    kg_path = tmp_path / "kg_session_20260101_000000.json"
    kg_path.write_text(json.dumps(chain_kg(), ensure_ascii=False), encoding="utf-8")

    summary = pipeline.run_pipeline(
        "保存済みの地図を描き直して", target="local", offline=True,
        kg_file=str(kg_path), layers=False, verify_causal=False,
        export_svg=False, learned=False)

    assert probed == []
    assert "render_fallback" not in summary
    assert summary["projection"]["mode"] == "local"
    assert FakeExecutor.made == ["local"]


# ============================================ D: 壁時計デッドライン


def test_deadline_exceeded_falls_back_to_file(online_run, monkeypatch) -> None:
    """描画が長引いたら打ち切って file へ倒す (半死のゲートウェイ対策)。"""
    monkeypatch.setattr(pipeline, "gateway_healthy", lambda **k: True)
    # 開始 → 1 回目の点検は通る → 描画中に時計が飛ぶ
    clock = iter([0.0, 1.0, 10_000.0] + [20_000.0] * 20)
    monkeypatch.setattr(pipeline, "time",
                        types.SimpleNamespace(monotonic=lambda: next(clock)))
    client = FakeFoundry()
    summary = online_run(client)

    assert summary["render_fallback"] is True
    assert summary["render_note"] == pipeline.RENDER_DEADLINE_NOTE
    assert summary["projection"]["mode"] == "file"
    assert client.calls.count("cc-projection") == 1     # 打ち切りなので 1 回だけ
    assert client.calls.count("cc-verification") == 0
    assert FakeExecutor.made == ["local", "file"]


def test_render_deadline_env(monkeypatch) -> None:
    """CC_RENDER_DEADLINE_S: 既定 600 秒 / 上書き可 / 読めない値で 0 にしない。"""
    monkeypatch.delenv(pipeline.RENDER_DEADLINE_ENV, raising=False)
    assert pipeline._render_deadline_s() == 600.0
    monkeypatch.setenv(pipeline.RENDER_DEADLINE_ENV, "30")
    assert pipeline._render_deadline_s() == 30.0
    monkeypatch.setenv(pipeline.RENDER_DEADLINE_ENV, "でたらめ")
    assert pipeline._render_deadline_s() == 600.0


# ============================================ B: 接続タイムアウト


class HangingCM:
    """__aenter__ が返ってこない context manager (半死のゲートウェイの模型)。"""

    def __init__(self) -> None:
        self.exited = False

    async def __aenter__(self):
        await asyncio.sleep(3600)
        raise AssertionError("到達しない")

    async def __aexit__(self, *exc: object) -> None:
        self.exited = True


class OkTransportCM:
    def __init__(self) -> None:
        self.exited = False

    async def __aenter__(self):
        return ("read", "write", None)

    async def __aexit__(self, *exc: object) -> None:
        self.exited = True


class HangingSession:
    """接続は張れるが initialize が返らないセッション。"""

    def __init__(self, read: object, write: object) -> None:
        self.exited = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.exited = True

    async def initialize(self) -> None:
        await asyncio.sleep(3600)


def test_connect_timeout_raises_connection_error(monkeypatch) -> None:
    """接続が返らなくても connect_timeout で必ず諦める (無限待ちしない)。"""
    hung = HangingCM()
    monkeypatch.setattr(mcp_client, "streamablehttp_client",
                        lambda *a, **k: hung)

    async def _go() -> None:
        client = mcp_client.ExcalidrawClient("http://127.0.0.1:9/mcp",
                                             connect_timeout=0.05)
        with pytest.raises(ConnectionError) as err:
            await client.__aenter__()
        assert "127.0.0.1:9" in str(err.value)
        assert client.session is None

    asyncio.run(_go())
    assert hung.exited is True          # 開きかけを閉じてから諦める


def test_partial_connection_is_cleaned_up(monkeypatch) -> None:
    """initialize で固まった場合、transport とセッションの両方を閉じる。"""
    transport = OkTransportCM()
    sessions: list[HangingSession] = []

    def make_session(read: object, write: object) -> HangingSession:
        s = HangingSession(read, write)
        sessions.append(s)
        return s

    monkeypatch.setattr(mcp_client, "streamablehttp_client",
                        lambda *a, **k: transport)
    monkeypatch.setattr(mcp_client, "ClientSession", make_session)

    async def _go() -> None:
        client = mcp_client.ExcalidrawClient(connect_timeout=0.05)
        with pytest.raises(ConnectionError):
            await client.__aenter__()
        assert client._cm is None and client._session_cm is None

    asyncio.run(_go())
    assert transport.exited is True
    assert sessions and sessions[0].exited is True


class RefusedCM:
    """ゲートウェイ不在の模型。

    実測: 接続できないと initialize は anyio のスコープキャンセルとして現れ、
    **本当の理由は後始末 (__aexit__) の側で出てくる**。
    """

    async def __aenter__(self):
        raise asyncio.CancelledError("Cancelled via cancel scope 0x1")

    async def __aexit__(self, *exc: object) -> None:
        raise ConnectionRefusedError("All connection attempts failed")


def test_dead_gateway_surfaces_as_connection_error(monkeypatch) -> None:
    """CancelledError のまま投げない (BaseException は呼び出し側をすり抜ける)。"""
    monkeypatch.setattr(mcp_client, "streamablehttp_client",
                        lambda *a, **k: RefusedCM())

    async def _go() -> None:
        client = mcp_client.ExcalidrawClient("http://127.0.0.1:8000/mcp")
        with pytest.raises(ConnectionError) as err:
            await client.__aenter__()
        assert "All connection attempts failed" in str(err.value)

    asyncio.run(_go())


def test_cancellation_without_a_cause_is_not_swallowed(monkeypatch) -> None:
    """後始末が何も語らないなら、キャンセルはキャンセルのまま通す。"""

    class PlainCancelCM:
        async def __aenter__(self):
            raise asyncio.CancelledError()

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(mcp_client, "streamablehttp_client",
                        lambda *a, **k: PlainCancelCM())

    async def _go() -> None:
        client = mcp_client.ExcalidrawClient()
        with pytest.raises(asyncio.CancelledError):
            await client.__aenter__()

    asyncio.run(_go())


def test_anyio_cancel_noise_is_translated() -> None:
    """anyio の "Cancelled via cancel scope 0x…" をそのまま見せない。"""
    noisy = asyncio.CancelledError("Cancelled via cancel scope 0x10 by <Task-1>")
    assert mcp_client._brief(noisy) == "ゲートウェイが応答しません (接続が中断されました)"
    assert "ConnectionRefusedError" in mcp_client._brief(
        ConnectionRefusedError("refused"))


def test_default_connect_timeout_is_bounded() -> None:
    """既定でタイムアウトが効く (指定し忘れが無限待ちにならない)。"""
    client = mcp_client.ExcalidrawClient()
    assert client.connect_timeout == mcp_client.DEFAULT_CONNECT_TIMEOUT == 10.0


# ============================================ C: ヘルスチェック


def test_gateway_healthy_reads_env_at_call_time(monkeypatch) -> None:
    """CC_GATEWAY_HEALTH は呼ぶたびに読む (import 時に固めない)。"""
    monkeypatch.delenv(mcp_client.HEALTH_URL_ENV, raising=False)
    assert mcp_client.gateway_health_url() == mcp_client.DEFAULT_HEALTH_URL
    monkeypatch.setenv(mcp_client.HEALTH_URL_ENV, "http://127.0.0.1:9999/hz")
    assert mcp_client.gateway_health_url() == "http://127.0.0.1:9999/hz"


def test_gateway_healthy_true_and_false(monkeypatch) -> None:
    """200 なら True / 到達不能なら例外を投げずに False。"""

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(mcp_client.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp())
    assert mcp_client.gateway_healthy("http://x/healthz") is True

    def boom(*a: object, **k: object):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(mcp_client.urllib.request, "urlopen", boom)
    assert mcp_client.gateway_healthy("http://x/healthz") is False


# ============================================ E: 幽霊 running の補正


def test_stale_running_history_is_marked_interrupted(tmp_path) -> None:
    """サーバ起動時、running のまま残った履歴行を interrupted へ直す。"""
    from cc_web.jobs import JobManager, read_history

    path = tmp_path / "web_history.jsonl"
    path.write_text(
        json.dumps({"ts": "2026-08-07T20:41:06", "message": "済み",
                    "job_id": "a", "status": "done", "session": "s1"},
                   ensure_ascii=False) + "\n"
        + json.dumps({"ts": "2026-08-07T23:20:21", "message": "固まった",
                      "job_id": "b", "status": "running"},
                     ensure_ascii=False) + "\n"
        + "{壊れた行\n",
        encoding="utf-8")

    manager = JobManager(history_path=path)
    manager.shutdown()

    rows = {r["job_id"]: r for r in read_history(path)}
    assert rows["a"]["status"] == "done"          # 済んだ行は触らない
    assert rows["b"]["status"] == "interrupted"
    assert rows["b"]["note"] == "サーバ再起動により中断"
    assert "{壊れた行" in path.read_text(encoding="utf-8")   # 壊れた行も失わない


def test_history_untouched_when_nothing_is_stale(tmp_path) -> None:
    """直すものが無ければ書き換えない (履歴ファイルを毎回いじらない)。"""
    from cc_web.jobs import JobManager

    path = tmp_path / "web_history.jsonl"
    original = json.dumps({"ts": "t", "message": "m", "job_id": "a",
                           "status": "done"}, ensure_ascii=False) + "\n"
    path.write_text(original, encoding="utf-8")
    before = path.stat().st_mtime_ns

    JobManager(history_path=path).shutdown()

    assert path.read_text(encoding="utf-8") == original
    assert path.stat().st_mtime_ns == before


def test_missing_history_file_is_fine(tmp_path) -> None:
    """履歴がまだ無いだけの起動で落ちない。"""
    from cc_web.jobs import JobManager

    path = tmp_path / "logs" / "web_history.jsonl"
    JobManager(history_path=path).shutdown()
    assert not path.exists()
