"""Web アプリ (cc_web) の回帰テスト — 設計書 §7。

Foundry (Azure AI Foundry) にも Excalidraw MCP にも依存しない。地図生成は
**offline ジョブ** (保存済み KG から詳細度計算以降だけを回す) と
target="file" (MCP なしでシーンを作る) の組み合わせで通す。

各テストは tmp_path を作業ディレクトリにするので、production/ 配下の
graphs / exports / logs を汚さない。
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

import pytest

from cc_core.evaluation import EvaluationStore
from cc_web import account
from cc_web.app import create_app

PRODUCTION = Path(__file__).resolve().parents[1]
KG_FIXTURE = PRODUCTION / "graphs" / "kg_session_20260807_010128.json"
KG_NAME = "kg_web_test.json"
JOB_TIMEOUT_SEC = 60


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """本物の KG を置いた作業ディレクトリ。az CLI は呼ばせない。"""
    (tmp_path / "graphs").mkdir()
    shutil.copy(KG_FIXTURE, tmp_path / "graphs" / KG_NAME)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(account, "_az_upn", lambda: "nakamura.zen@example.ac.jp")
    account.clear_cache()
    yield tmp_path
    account.clear_cache()


@pytest.fixture
def client(workdir):
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as test_client:
        yield test_client


def run_offline_job(client, message: str = "今週の研究を概念地図として整理して") -> dict:
    """offline ジョブを投入し done まで待つ (Foundry / MCP を使わない経路)。"""
    res = client.post("/api/jobs", json={
        "message": message, "kg_file": KG_NAME, "offline": True,
        "causal_verify": False, "target": "file",
    })
    assert res.status_code == 202, res.text
    job_id = res.json()["job_id"]

    deadline = time.time() + JOB_TIMEOUT_SEC
    job = {}
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert job.get("status") == "done", job.get("error")
    return job


@pytest.fixture
def session(client) -> str:
    return run_offline_job(client)["summary"]["session"]


# ------------------------------------------------------------------ ① 基本

def test_healthz(client) -> None:
    assert client.get("/healthz").json() == {"ok": True}


def test_index_html_is_served(client) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    # モックの必須要素 (グラデタイトル・免責文言・入力欄) が入っていること
    assert "Concept Cartographer" in res.text
    assert "研究の断片をつなぎ、意味ある全体像へ" in res.text
    assert "研究について何でも聞いてください" in res.text


def test_static_assets_are_served(client) -> None:
    assert "--indigo-900:#26215C" in client.get("/static/app.css").text
    assert client.get("/static/app.js").status_code == 200


def test_templates_are_four(client) -> None:
    items = client.get("/api/templates").json()["templates"]
    assert len(items) == 4
    assert [t["id"] for t in items] == ["weekly", "prior", "ideas", "causal"]
    for tpl in items:
        assert tpl["title"] and tpl["description"] and tpl["message"]


def test_me_has_expected_shape(client) -> None:
    me = client.get("/api/me").json()
    assert me == {"name": "Nakamura Zen", "upn": "nakamura.zen@example.ac.jp",
                  "initials": "NZ", "signed_in": True, "mode": "personal"}


def test_me_falls_back_when_az_is_unavailable(client, monkeypatch) -> None:
    monkeypatch.setattr(account, "_az_upn", lambda: None)
    account.clear_cache()
    me = client.get("/api/me").json()
    assert me["signed_in"] is False and me["name"] == "ローカル ユーザー"


# ------------------------------------------------------- ② offline ジョブ E2E

def test_offline_job_runs_without_foundry(client) -> None:
    job = run_offline_job(client)
    summary = job["summary"]
    assert summary["offline"] is True
    assert set(summary["levels"]) == {"overview", "standard", "detailed"}
    assert summary["band_check"] == "OK"
    assert summary["verification"]["verdict"] == "PASS"
    assert job["stages_done"][0] == "routing" and "export" in job["stages_done"]
    assert job["stage"] is None and job["finished_at"]


def test_offline_job_requires_kg_file(client) -> None:
    res = client.post("/api/jobs", json={"message": "地図にして", "offline": True})
    assert res.status_code == 400
    assert "kg_file" in res.json()["error"]["message"]


def test_job_is_written_to_history(client, session) -> None:
    items = client.get("/api/history").json()["items"]
    assert items and items[0]["session"] == session
    assert items[0]["status"] == "done" and items[0]["route"] == "map"


# ---------------------------------------------------------- ③ セッション API

def test_session_is_listed(client, session) -> None:
    listed = client.get("/api/sessions").json()["sessions"]
    entry = next(s for s in listed if s["session"] == session)
    assert entry["title"] == "今週の研究を概念地図として整理して"
    assert set(entry["levels"]) == {"overview", "standard", "detailed"}


def test_session_detail_has_kpi(client, session) -> None:
    detail = client.get(f"/api/sessions/{session}").json()
    assert detail["default_level"] in ("overview", "standard", "detailed")
    assert detail["gaps_usefulness"]["total_candidates"] > 0
    assert "evidence_display" in detail["kpi"] and "causal" in detail["kpi"]


@pytest.mark.parametrize("level", ["overview", "standard", "detailed"])
def test_svg_has_click_targets(client, session, level) -> None:
    res = client.get(f"/api/sessions/{session}/svg?level={level}")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/svg+xml")
    assert res.text.startswith("<svg")
    assert 'data-node-id="' in res.text
    assert 'class="cc-node"' in res.text
    assert 'data-island-id="' in res.text


def test_view_json_has_nodes_edges_gaps(client, session) -> None:
    view = client.get(f"/api/sessions/{session}/view?level=standard").json()
    assert view["nodes"] and view["edges"] and view["gaps"]
    assert "_level_plans" not in view          # 重いので返さない
    assert {"id", "label", "kind", "community_id"} <= set(view["nodes"][0])
    assert {"id", "from", "to", "glyph"} <= set(view["edges"][0])


def test_excalidraw_is_downloadable(client, session) -> None:
    res = client.get(f"/api/sessions/{session}/excalidraw")
    assert res.status_code == 200
    assert json.loads(res.text)["type"] == "excalidraw"
    Path(f"exports/session_{session}.excalidraw").unlink()
    assert client.get(f"/api/sessions/{session}/excalidraw").status_code == 404


def test_unknown_session_is_404(client) -> None:
    assert client.get("/api/sessions/nope").status_code == 404
    # パストラバーサル形のセッション ID も 404 (ファイルを読ませない)
    assert client.get("/api/sessions/..%2F..%2Fetc/view").status_code == 404


def test_unknown_level_is_400(client, session) -> None:
    res = client.get(f"/api/sessions/{session}/svg?level=huge")
    assert res.status_code == 400
    assert "詳細度" in res.json()["error"]["message"]


# -------------------------------------------------------- ④ 詳細度ごとの差

def test_levels_differ_in_node_count(client, session) -> None:
    overview = client.get(f"/api/sessions/{session}/view?level=overview").json()
    detailed = client.get(f"/api/sessions/{session}/view?level=detailed").json()
    assert len(overview["nodes"]) < len(detailed["nodes"])
    svg_a = client.get(f"/api/sessions/{session}/svg?level=overview").text
    svg_b = client.get(f"/api/sessions/{session}/svg?level=detailed").text
    assert svg_a != svg_b
    # キャッシュから返しても中身は同じ (決定的)
    assert client.get(f"/api/sessions/{session}/svg?level=overview").text == svg_a


# ------------------------------------------------------------- ⑤ ギャップ

def test_gap_confirm_updates_status_and_usefulness(client, session) -> None:
    gaps = client.get(f"/api/sessions/{session}/view?level=standard").json()["gaps"]
    gap_id = gaps[0]["gap_id"]
    res = client.post(f"/api/sessions/{session}/gaps/{gap_id}",
                      json={"decision": "confirm"})
    assert res.status_code == 200
    body = res.json()
    assert body["gap"]["status"] == "confirmed"
    assert body["gap"]["confirmed_by"] == "nakamura.zen@example.ac.jp"
    assert body["gap"]["confirmed_at"]
    assert body["usefulness"]["confirmed"] == 1
    assert body["usefulness"]["usefulness_rate"] == 1.0
    # plan ファイルへ保存されている (プロセスを跨いでも残る)
    plan = json.loads((Path("graphs") / f"layout_plan_session_{session}.json")
                      .read_text(encoding="utf-8"))
    assert next(g for g in plan["gaps"] if g["gap_id"] == gap_id)["status"] == "confirmed"


def test_double_decision_is_409(client, session) -> None:
    gap_id = client.get(f"/api/sessions/{session}/view").json()["gaps"][0]["gap_id"]
    client.post(f"/api/sessions/{session}/gaps/{gap_id}", json={"decision": "confirm"})
    res = client.post(f"/api/sessions/{session}/gaps/{gap_id}", json={"decision": "dismiss"})
    assert res.status_code == 409


def test_unknown_gap_is_404(client, session) -> None:
    res = client.post(f"/api/sessions/{session}/gaps/gap-nope",
                      json={"decision": "confirm"})
    assert res.status_code == 404


def test_invalid_decision_is_400(client, session) -> None:
    gap_id = client.get(f"/api/sessions/{session}/view").json()["gaps"][0]["gap_id"]
    res = client.post(f"/api/sessions/{session}/gaps/{gap_id}", json={"decision": "maybe"})
    assert res.status_code == 400


# --------------------------------------------------------------- ⑥ 展開

def test_expand_returns_members(client, session) -> None:
    view = client.get(f"/api/sessions/{session}/view?level=overview").json()
    aggregates = [n for n in view["nodes"] if n["kind"] == "aggregate"]
    assert aggregates, "overview に集約ノードが無い KG では展開を検証できない"
    agg_id = aggregates[0].get("aggregate_id") or aggregates[0]["id"]
    body = client.post(f"/api/sessions/{session}/expand/{agg_id}").json()
    assert body["aggregate"]["id"] == agg_id
    assert body["members"]
    for member in body["members"]:
        assert member["id"] and member["label"]
        assert member["label"] != member["id"]  # detailed からラベルを引けている


def test_expand_unknown_is_404(client, session) -> None:
    assert client.post(f"/api/sessions/{session}/expand/agg-nope").status_code == 404


# --------------------------------------------------------------- ⑦ 評価

def test_evaluation_accepts_three_shapes(client, session) -> None:
    base = f"/api/sessions/{session}/evaluation"
    edge_id = client.get(f"/api/sessions/{session}/view").json()["edges"][0]["id"]
    assert client.post(base, json={"satisfaction": 5}).json() == {"ok": True}
    assert client.post(base, json={"edge_id": edge_id,
                                   "verdict": "correct"}).json() == {"ok": True}
    assert client.post(base, json={"operation": "level_switch",
                                   "to": "overview"}).json() == {"ok": True}

    records = EvaluationStore("logs/evaluation.jsonl").load()
    assert len(records) == 3
    assert all(r["map_id"] == session for r in records)
    assert records[0]["satisfaction"] == 5
    assert records[1]["relation_verdicts"][edge_id] == "correct"
    assert records[2]["operations"][0]["op"] == "level_switch"


def test_evaluation_rejects_unknown_labels(client, session) -> None:
    base = f"/api/sessions/{session}/evaluation"
    assert client.post(base, json={"satisfaction": 9}).status_code == 400
    assert client.post(base, json={"edge_id": "r1", "verdict": "useful"}).status_code == 400
    assert client.post(base, json={"operation": "nope"}).status_code == 400
    assert client.post(base, json={}).status_code == 400


# ------------------------------------------------------------- ⑧ ファイル

def test_upload_and_list(client, workdir) -> None:
    res = client.post("/api/files",
                      files=[("files", ("note.md", b"# memo", "text/markdown"))])
    assert res.status_code == 200 and res.json()["saved"] == ["note.md"]
    files = client.get("/api/files").json()["files"]
    assert files[0]["name"] == "note.md" and files[0]["ext"] == "md"
    assert (workdir / "inbox" / "note.md").exists()


def test_upload_rejects_unsupported_extension(client, workdir) -> None:
    res = client.post("/api/files", files=[("files", ("evil.exe", b"MZ", "application/exe"))])
    assert res.status_code == 400
    assert not (workdir / "inbox" / "evil.exe").exists()


def test_upload_strips_path_traversal(client, workdir) -> None:
    res = client.post("/api/files",
                      files=[("files", ("../../evil.md", b"x", "text/markdown"))])
    assert res.status_code == 200 and res.json()["saved"] == ["evil.md"]
    assert (workdir / "inbox" / "evil.md").exists()
    assert not (workdir.parent / "evil.md").exists()


def test_kg_file_outside_graphs_is_rejected(client) -> None:
    res = client.post("/api/jobs", json={"message": "地図にして", "offline": True,
                                         "kg_file": "../../etc/passwd"})
    assert res.status_code == 400


# --------------------------------------------------------------- ⑨ ジョブ

def test_unknown_job_is_404(client) -> None:
    res = client.get("/api/jobs/deadbeef")
    assert res.status_code == 404
    assert "error" in res.json()


def test_jobs_are_serialized(client, monkeypatch) -> None:
    """キャンバスが共有状態なのでジョブは 1 本ずつ (max_workers=1)。"""
    started = threading.Event()

    def slow_pipeline(message, **kwargs):
        started.set()
        time.sleep(0.8)
        return {"session": "dummy", "status": "success"}

    monkeypatch.setattr("cc_web.jobs.run_pipeline", slow_pipeline)
    first = client.post("/api/jobs", json={"message": "A"}).json()["job_id"]
    second = client.post("/api/jobs", json={"message": "B"}).json()["job_id"]

    assert started.wait(5), "1 件目が動き出さない"
    assert client.get(f"/api/jobs/{first}").json()["status"] == "running"
    assert client.get(f"/api/jobs/{second}").json()["status"] == "queued"

    deadline = time.time() + 10
    while time.time() < deadline:
        if client.get(f"/api/jobs/{second}").json()["status"] == "done":
            break
        time.sleep(0.1)
    assert client.get(f"/api/jobs/{second}").json()["status"] == "done"
    assert client.get(f"/api/jobs/{first}").json()["status"] == "done"


def test_job_error_is_reported(client, monkeypatch) -> None:
    def boom(message, **kwargs):
        raise RuntimeError("extraction returned no nodes")

    monkeypatch.setattr("cc_web.jobs.run_pipeline", boom)
    job_id = client.post("/api/jobs", json={"message": "壊れる依頼"}).json()["job_id"]
    deadline = time.time() + 10
    job = {}
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert job["status"] == "error"
    assert "extraction returned no nodes" in job["error"]
    assert client.get("/api/history").json()["items"][0]["status"] == "error"
