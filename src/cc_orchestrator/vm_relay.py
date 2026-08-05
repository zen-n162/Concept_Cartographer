"""VM-Excalidraw-MCP への描画中継 (az vm run-command 経由)。

Mac から VM の private IP (<VM の private IP>) へは経路がないため、Azure 制御プレーンの
run-command でスクリプトを VM 内で実行し、同 VM の localhost:8000/mcp に対して
vmexec.py (cc_core) を走らせる。ネットワーク公開ゼロのまま VM を描画先にできる。
"""

from __future__ import annotations

import base64
import json
import subprocess
from typing import Any

from cc_core.logging_util import get_logger

logger = get_logger("cc_orchestrator.vm_relay")

RG = "prj-qst-ai"
VM = "VM-Excalidraw-MCP"
VMEXEC = "sudo -u azureuser /opt/cartographer/venv/bin/python /opt/cartographer/app/vmexec.py"


def _invoke(script: str, timeout_s: int = 600) -> str:
    proc = subprocess.run(
        ["az", "vm", "run-command", "invoke", "-g", RG, "-n", VM,
         "--command-id", "RunShellScript", "--scripts", script,
         "--query", "value[0].message", "-o", "tsv"],
        capture_output=True, text=True, timeout=timeout_s,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"run-command failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


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
