"""編集 API / 学習 API / 編集 CLI の回帰テスト — 編集/学習設計書 §10。

Foundry にも MCP にも依存しない。地図は offline ジョブ (保存済み KG から
詳細度計算以降だけを回す) で作り、その上で編集 API を往復させる。

CLI 側も同じ cc_core.editing / cc_core.learning を通ることを確認する
(リポジトリ規約: 片方にしかない機能を作らない)。
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from cc_core import editing
from cc_core.learning import load_learned
from cc_web import account
from cc_web.app import create_app

PRODUCTION = Path(__file__).resolve().parents[1]
KG_FIXTURE = PRODUCTION / "tests" / "fixtures" / "kg_sample.json"
KG_NAME = "kg_web_test.json"
JOB_TIMEOUT_SEC = 60


@pytest.fixture
def workdir(tmp_path, monkeypatch):
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


def run_offline_job(client, **extra) -> dict:
    body = {"message": "今週の研究を概念地図として整理して", "kg_file": KG_NAME,
            "offline": True, "causal_verify": False, "target": "file"}
    body.update(extra)
    res = client.post("/api/jobs", json=body)
    assert res.status_code == 202, res.text
    job_id = res.json()["job_id"]
    deadline = time.time() + JOB_TIMEOUT_SEC
    job: dict = {}
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


def first_node(client, session: str) -> dict:
    view = client.get(f"/api/sessions/{session}/view?level=detailed").json()
    return next(n for n in view["nodes"] if n.get("kind") != "aggregate")


def first_edge(client, session: str) -> dict:
    view = client.get(f"/api/sessions/{session}/view?level=detailed").json()
    return view["edges"][0]


# ------------------------------------------------------------ 編集 API

def test_offline_job_saves_base_kg_so_the_session_is_editable(client, session) -> None:
    """kg_file 経由でも原本が残る = どのセッションでも編集できる。"""
    assert editing.kg_file(session).exists()
    assert client.get(f"/api/sessions/{session}/edits").json()["editable"] is True


def test_post_edit_updates_view_and_purges_svg_cache(client, session) -> None:
    node = first_node(client, session)
    cache = Path("exports/web") / f"{session}_standard.svg"
    assert client.get(f"/api/sessions/{session}/svg?level=standard").status_code == 200
    assert cache.exists()

    res = client.post(f"/api/sessions/{session}/edits?level=standard", json={
        "op": "rename_node", "target": node["id"], "payload": {"label": "改名しました"},
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["edit"]["op"] == "rename_node"
    assert body["edit"]["user"] == "nakamura.zen@example.ac.jp"
    assert body["edit"]["before"]["label"] == node["label"]
    assert not cache.exists(), "編集後は SVG キャッシュが消えていること"

    labels = {n["label"] for n in body["view"]["nodes"]}
    assert "改名しました" in labels
    # 取り直した SVG にも反映される
    svg = client.get(f"/api/sessions/{session}/svg?level=detailed").text
    assert "改名しました" in svg


def test_user_elements_are_marked_in_svg_and_view(client, session) -> None:
    node = first_node(client, session)
    client.post(f"/api/sessions/{session}/edits", json={
        "op": "rename_node", "target": node["id"], "payload": {"label": "手で直した"}})
    view = client.get(f"/api/sessions/{session}/view?level=detailed").json()
    edited = next(n for n in view["nodes"] if n["label"] == "手で直した")
    assert edited["origin"] == "user_edited"
    svg = client.get(f"/api/sessions/{session}/svg?level=detailed").text
    assert 'data-origin="user"' in svg


def test_add_edge_is_excluded_from_causal_precision(client, session) -> None:
    """手動追加の関係は AI の因果精度 KPI の分母に入れない (§2)。"""
    view = client.get(f"/api/sessions/{session}/view?level=detailed").json()
    a, b = view["nodes"][0]["id"], view["nodes"][-1]["id"]
    before = client.get(f"/api/sessions/{session}").json()["kpi"]["causal"]

    res = client.post(f"/api/sessions/{session}/edits", json={
        "op": "add_edge", "payload": {"from": a, "to": b, "label": "手動",
                                      "glyph": "arrow"}})
    assert res.status_code == 200, res.text
    after = client.get(f"/api/sessions/{session}").json()["kpi"]["causal"]
    assert after["causal_candidates"] == before["causal_candidates"]
    assert after["user_edges_excluded"] >= 1


def test_validation_errors_map_to_400_404_409(client, session) -> None:
    node = first_node(client, session)
    # 400: 空ラベル
    res = client.post(f"/api/sessions/{session}/edits", json={
        "op": "rename_node", "target": node["id"], "payload": {"label": "  "}})
    assert res.status_code == 400 and "ラベル" in res.json()["error"]["message"]
    # 404: 未知のノード
    res = client.post(f"/api/sessions/{session}/edits", json={
        "op": "rename_node", "target": "nope", "payload": {"label": "x"}})
    assert res.status_code == 404
    # 404: 未知のセッション
    assert client.post("/api/sessions/nosuch/edits", json={
        "op": "rename_node", "target": "c001", "payload": {"label": "x"}}
    ).status_code == 404
    # 400: 未知の操作
    assert client.post(f"/api/sessions/{session}/edits", json={
        "op": "explode", "payload": {}}).status_code == 400


def test_revert_round_trip_and_double_revert_conflicts(client, session) -> None:
    node = first_node(client, session)
    edit_id = client.post(f"/api/sessions/{session}/edits", json={
        "op": "rename_node", "target": node["id"], "payload": {"label": "取り消す前"},
    }).json()["edit"]["edit_id"]

    res = client.post(f"/api/sessions/{session}/edits/{edit_id}/revert")
    assert res.status_code == 200, res.text
    labels = {n["label"] for n in res.json()["view"]["nodes"]}
    assert "取り消す前" not in labels and node["label"] in labels

    assert client.post(f"/api/sessions/{session}/edits/{edit_id}/revert"
                       ).status_code == 409
    assert client.post(f"/api/sessions/{session}/edits/e-19990101-001/revert"
                       ).status_code == 404


def test_edit_list_marks_reverted_rows(client, session) -> None:
    node = first_node(client, session)
    first = client.post(f"/api/sessions/{session}/edits", json={
        "op": "rename_node", "target": node["id"], "payload": {"label": "一度目"},
    }).json()["edit"]
    client.post(f"/api/sessions/{session}/edits/{first['edit_id']}/revert")

    data = client.get(f"/api/sessions/{session}/edits").json()
    assert [e["op"] for e in data["edits"]] == ["rename_node", "revert"]
    assert data["edits"][0]["reverted"] is True
    assert data["edits"][0]["reverted_by"] == data["edits"][1]["edit_id"]
    assert data["warnings"] == []


def test_edits_persist_across_app_restart(client, session, workdir) -> None:
    """リロード後も編集が残る (状態はファイルが正)。"""
    node = first_node(client, session)
    client.post(f"/api/sessions/{session}/edits", json={
        "op": "rename_node", "target": node["id"], "payload": {"label": "永続化"}})

    from fastapi.testclient import TestClient
    with TestClient(create_app()) as fresh:
        view = fresh.get(f"/api/sessions/{session}/view?level=detailed").json()
        assert "永続化" in {n["label"] for n in view["nodes"]}
        assert len(fresh.get(f"/api/sessions/{session}/edits").json()["edits"]) == 1


def test_edit_applies_to_every_level(client, session) -> None:
    """編集が全レベルに反映され、ピン留めで消えない (§11-3)。"""
    view = client.get(f"/api/sessions/{session}/view?level=overview").json()
    detailed = client.get(f"/api/sessions/{session}/view?level=detailed").json()
    shown = {n["id"] for n in view["nodes"]}
    hidden = next((n for n in detailed["nodes"]
                   if n["id"] not in shown and n.get("kind") != "aggregate"), None)
    target = hidden or detailed["nodes"][0]

    client.post(f"/api/sessions/{session}/edits", json={
        "op": "rename_node", "target": target["id"], "payload": {"label": "全レベル確認"}})
    for level in ("overview", "standard", "detailed"):
        got = client.get(f"/api/sessions/{session}/view?level={level}").json()
        assert "全レベル確認" in {n["label"] for n in got["nodes"]}, level


def test_edits_are_serialized_with_generation_jobs(client, session) -> None:
    """編集は JobManager と同じワーカーを通る = 生成と競合しない (§8.1)。"""
    node = first_node(client, session)
    job = client.post("/api/jobs", json={
        "message": "2 本目", "kg_file": KG_NAME, "offline": True,
        "causal_verify": False, "target": "file"}).json()
    # 生成中に編集を投げても、キューで待って必ず 200 で返る
    res = client.post(f"/api/sessions/{session}/edits", json={
        "op": "rename_node", "target": node["id"], "payload": {"label": "直列化"}})
    assert res.status_code == 200, res.text
    deadline = time.time() + JOB_TIMEOUT_SEC
    while time.time() < deadline:
        if client.get(f"/api/jobs/{job['job_id']}").json()["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert "直列化" in {n["label"] for n in res.json()["view"]["nodes"]}


# ------------------------------------------------------------ 学習 API

def test_learned_api_reflects_edits(client, session) -> None:
    assert client.get("/api/learned").json()["summary"]["lexicon"] == 0
    node = first_node(client, session)
    res = client.post(f"/api/sessions/{session}/edits", json={
        "op": "rename_node", "target": node["id"], "payload": {"label": "学習される名前"}})
    assert res.json()["learned_delta"]["changed"]["lexicon"] == 1

    learned = client.get("/api/learned").json()
    assert learned["summary"]["lexicon_auto"] == 1
    assert learned["lexicon"][0]["to"] == "学習される名前"


def test_job_learned_flag_controls_auto_application(client, session) -> None:
    """learned:false で自動適用が止まる (§11-4)。"""
    node = first_node(client, session)
    client.post(f"/api/sessions/{session}/edits", json={
        "op": "rename_node", "target": node["id"], "payload": {"label": "学習後の名前"}})

    on = run_offline_job(client)["summary"]
    assert on["learned"]["enabled"] is True
    assert on["learned"]["renames"] == 1
    assert on["learned"]["details"][0]["to"] == "学習後の名前"
    new_session = on["session"]
    view = client.get(f"/api/sessions/{new_session}/view?level=detailed").json()
    assert "学習後の名前" in {n["label"] for n in view["nodes"]}

    off = run_offline_job(client, learned=False)["summary"]
    assert off["learned"] == {"enabled": False, "renames": 0, "stoplisted": 0,
                              "causal_allow": 0, "causal_deny": 0, "reversed": 0,
                              "details": []}
    view = client.get(f"/api/sessions/{off['session']}/view?level=detailed").json()
    assert "学習後の名前" not in {n["label"] for n in view["nodes"]}


def test_offline_pipeline_reports_learned_in_summary(client, session) -> None:
    """offline パイプラインでも summary["learned"] に内訳が出る (黙って直さない)。"""
    summary = run_offline_job(client)["summary"]
    assert "learned" in summary and summary["learned"]["enabled"] is True
    assert set(summary["learned"]) >= {"renames", "stoplisted", "causal_allow",
                                       "causal_deny", "reversed", "details"}


# ------------------------------------------------------------ 添付ファイル

def test_upload_then_delete_file(client) -> None:
    client.post("/api/files", files=[("files", ("note.md", b"# hi", "text/markdown"))])
    assert "note.md" in [f["name"] for f in client.get("/api/files").json()["files"]]

    assert client.delete("/api/files/note.md").json() == {"deleted": "note.md"}
    assert client.get("/api/files").json()["files"] == []
    assert client.delete("/api/files/note.md").status_code == 404


def test_delete_file_cannot_escape_inbox(client, workdir) -> None:
    outside = workdir / "secret.md"
    outside.write_text("keep me", encoding="utf-8")
    (workdir / "inbox").mkdir(exist_ok=True)
    # ../secret.md は basename 化され inbox/secret.md を探すので 404 になる
    assert client.delete("/api/files/..%2Fsecret.md").status_code == 404
    assert outside.exists()


# ------------------------------------------------------------ CLI (§7)

def run_cli(monkeypatch, capsys, *argv: str) -> str:
    from cc_orchestrator import chat

    monkeypatch.setattr("sys.argv", ["chat", *argv])
    chat.main()
    return capsys.readouterr().out


def test_cli_edit_round_trip(client, session, monkeypatch, capsys) -> None:
    plan = str(editing.plan_file(session))
    node = first_node(client, session)
    op = json.dumps({"op": "rename_node", "target": node["id"],
                     "payload": {"label": "CLI から改名"}}, ensure_ascii=False)

    out = run_cli(monkeypatch, capsys, "--plan", plan, "--edit", op, "--user", "cli-user")
    assert "rename_node" in out and "再構成" in out

    view = client.get(f"/api/sessions/{session}/view?level=detailed").json()
    assert "CLI から改名" in {n["label"] for n in view["nodes"]}

    out = run_cli(monkeypatch, capsys, "--plan", plan, "--list-edits")
    edit_id = editing.load_edits(session)[0]["edit_id"]
    assert edit_id in out and "rename_node" in out

    out = run_cli(monkeypatch, capsys, "--plan", plan, "--revert-edit", edit_id)
    assert "revert" in out
    view = client.get(f"/api/sessions/{session}/view?level=detailed").json()
    assert "CLI から改名" not in {n["label"] for n in view["nodes"]}


def test_cli_edit_file_batch(client, session, monkeypatch, capsys) -> None:
    plan = str(editing.plan_file(session))
    view = client.get(f"/api/sessions/{session}/view?level=detailed").json()
    ops = [
        {"op": "add_node", "payload": {"label": "バッチ概念", "new_island": True}},
        {"op": "relabel_edge", "target": view["edges"][0]["id"],
         "payload": {"label": "バッチで変更"}},
    ]
    Path("ops.json").write_text(json.dumps(ops, ensure_ascii=False), encoding="utf-8")
    out = run_cli(monkeypatch, capsys, "--plan", plan, "--edit-file", "ops.json")
    assert "add_node" in out and "relabel_edge" in out
    assert len(editing.load_edits(session)) == 2


def test_cli_show_learned_and_relearn(client, session, monkeypatch, capsys) -> None:
    plan = str(editing.plan_file(session))
    node = first_node(client, session)
    run_cli(monkeypatch, capsys, "--plan", plan, "--edit", json.dumps(
        {"op": "rename_node", "target": node["id"], "payload": {"label": "学習表示"}},
        ensure_ascii=False))

    out = run_cli(monkeypatch, capsys, "--show-learned")
    assert "学習ストア" in out and "「学習表示」" in out
    assert "モデルの再学習ではありません" in out

    Path("logs/feedback/learned.json").unlink()
    out = run_cli(monkeypatch, capsys, "--relearn")
    assert "再構成" in out
    assert load_learned()["lexicon"][0]["to"] == "学習表示"
