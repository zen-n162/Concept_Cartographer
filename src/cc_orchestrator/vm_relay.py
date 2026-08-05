"""VM-Excalidraw-MCP への描画中継 (az vm run-command 経由)。

Mac から VM の private IP (<VM の private IP>) へは経路がないため、Azure 制御プレーンの
run-command でスクリプトを VM 内で実行し、同 VM の localhost:8000/mcp に対して
vmexec.py (cc_core) を走らせる。ネットワーク公開ゼロのまま VM を描画先にできる。
"""

from __future__ import annotations

import base64
import json
import subprocess
import time
from typing import Any

from cc_core.logging_util import get_logger

logger = get_logger("cc_orchestrator.vm_relay")

RG = "prj-qst-ai"
VM = "VM-Excalidraw-MCP"
VMEXEC = "sudo -u azureuser /opt/cartographer/venv/bin/python /opt/cartographer/app/vmexec.py"


CONFLICT_MARK = "Run command extension execution is in progress"


def _invoke(script: str, timeout_s: int = 900,
            conflict_retries: int = 12, conflict_wait_s: float = 20.0) -> str:
    """VM 上でスクリプトを実行する。

    az vm run-command は VM 単位で排他のため、直前の実行が残っていると Conflict に
    なる。パイプライン全体を落とさずに解放を待って再試行する。
    """
    for attempt in range(conflict_retries + 1):
        proc = subprocess.run(
            ["az", "vm", "run-command", "invoke", "-g", RG, "-n", VM,
             "--command-id", "RunShellScript", "--scripts", script,
             "--query", "value[0].message", "-o", "tsv"],
            capture_output=True, text=True, timeout=timeout_s,
        )
        if proc.returncode == 0:
            return proc.stdout
        err = proc.stderr.strip()
        if CONFLICT_MARK in err and attempt < conflict_retries:
            logger.info("vm run-command busy; waiting %.0fs (%d/%d)",
                        conflict_wait_s, attempt + 1, conflict_retries)
            time.sleep(conflict_wait_s)
            continue
        raise RuntimeError(f"run-command failed: {err[:300]}")
    raise RuntimeError("run-command stayed busy; VM 側の実行が終わるのを待って再試行してください")


def _extract_json_line(message: str) -> dict[str, Any]:
    for line in message.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    raise RuntimeError(f"no JSON in run-command output: {message[:300]}")


def vm_call(command: str, payload: dict | None = None) -> dict[str, Any]:
    """vmexec.py <command> [b64(payload)] を VM 上で実行し JSON を返す。"""
    arg = ""
    if payload is not None:
        arg = " " + base64.b64encode(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
    logger.info("vm relay start cmd=%s payload=%dB", command, len(arg))
    out = _invoke(f"{VMEXEC} {command}{arg}")
    result = _extract_json_line(out)
    logger.info("vm relay done cmd=%s", command)
    return result
