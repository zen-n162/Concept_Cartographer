"""CLI --render の回帰テスト (「Excalidraw で開く」ミニ設計 §4)。

生成済み plan を再生成せず、そのままローカル canvas へ描画するだけの薄い
経路 (`--switch` と同じ形)。実 MCP には依存しない — ToolExecutor をモックする。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cc_core.detail import build_multilevel_plan
from cc_orchestrator import chat

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def plan_file(tmp_path) -> Path:
    kg = json.loads((FIXTURES / "kg_min.json").read_text(encoding="utf-8"))
    plan = build_multilevel_plan(kg, default_level="standard")
    out = tmp_path / "layout_plan_session_test001.json"
    out.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return out


def test_render_flag_projects_and_renders(plan_file, monkeypatch, capsys) -> None:
    """--render は project(plan, level) の結果を描画に渡し、canvas URL を出力する。"""
    calls: dict = {}

    class _FakeExecutor:
        def __init__(self, target: str = "local") -> None:
            calls["target"] = target

        def tool_render_layout_plan(self, args: dict) -> dict:
            calls["plan"] = args["plan"]
            return {"success": True, "created": ["a", "b", "c"], "errors": []}

    monkeypatch.setattr(chat, "ToolExecutor", _FakeExecutor)
    monkeypatch.setattr(sys, "argv",
                        ["chat.py", "--render", str(plan_file), "--level", "overview"])

    chat.main()

    assert calls["target"] == "local"
    rendered_plan = calls["plan"]
    assert rendered_plan["nodes"] and "x" in rendered_plan["nodes"][0]  # 投影済み layout

    out = capsys.readouterr().out
    assert "overview" in out
    assert "3 要素" in out
    assert "http://127.0.0.1:3000" in out


def test_render_missing_plan_file_exits_with_error(tmp_path, monkeypatch) -> None:
    missing = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(sys, "argv", ["chat.py", "--render", str(missing)])

    with pytest.raises(SystemExit) as excinfo:
        chat.main()

    assert "見つかりません" in str(excinfo.value)


def test_render_failure_result_exits_with_error(plan_file, monkeypatch) -> None:
    """canvas/MCP が応答しても描画自体が失敗したら (success=False) エラー終了する。"""

    class _FailingExecutor:
        def __init__(self, target: str = "local") -> None:
            pass

        def tool_render_layout_plan(self, args: dict) -> dict:
            return {"success": False, "errors": ["mock failure"]}

    monkeypatch.setattr(chat, "ToolExecutor", _FailingExecutor)
    monkeypatch.setattr(sys, "argv", ["chat.py", "--render", str(plan_file)])

    with pytest.raises(SystemExit) as excinfo:
        chat.main()
    assert "失敗" in str(excinfo.value)
