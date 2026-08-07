"""新 Foundry Agents API クライアント (kind: prompt + Responses API)。

旧 `/assistants` (クラシック アシスタント) との違い:
  - ポータルの「エージェント」一覧に表示されるのは **`/agents`** の方だけ。
    `/assistants` に作ったものは「以前のエクスペリエンス」でしか見えない。
  - `/agents` は reasoning モデル (gpt-5.6 系) を正式サポートし、
    `reasoning: {effort}` を持つ。旧 API のように temperature/top_p を
    強制送信しないため gpt-5.6-sol/terra/luna がそのまま動く。
  - 実行は threads/runs ではなく **Responses API**:
      POST {project}/openai/v1/responses
        {"agent_reference": {"type":"agent_reference","name":"..."}, "input": ...}
    function tool は output に `function_call` として現れ、
    `previous_response_id` + `function_call_output` で結果を返す。

function tool の宣言はフラット形式 (Responses API 準拠):
    {"type": "function", "name": ..., "description": ..., "parameters": {...}}
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import httpx

from cc_core import token_usage
from cc_core.logging_util import get_logger
from cc_orchestrator import FOUNDRY_PROJECT_ENDPOINT
from cc_orchestrator.token_provider import TOKENS

logger = get_logger("cc_orchestrator.foundry_v2")

ToolExecutor = Callable[[str, dict[str, Any]], dict[str, Any]]
API_VERSION = "v1"


def fn_tool(name: str, description: str, params: dict[str, Any],
            required: list[str]) -> dict[str, Any]:
    """Responses API 形式の function tool 定義。"""
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": params, "required": required},
    }


def mcp_tool(label: str, server_url: str, connection_id: str) -> dict[str, Any]:
    """プロジェクト接続済みの Remote MCP (Work IQ 等) をツールとして参照する。"""
    return {
        "type": "mcp",
        "server_label": label,
        "server_url": server_url,
        "require_approval": "never",
        "project_connection_id": connection_id,
    }


class FoundryAgentsV2:
    def __init__(self, endpoint: str = FOUNDRY_PROJECT_ENDPOINT) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.responses_url = f"{self.endpoint}/openai/v1/responses"
        self._http = httpx.Client(timeout=900)
        # このクライアントが使ったトークンの累計 (裁定 Z)。エージェント管理
        # (_req 経由の /agents) は課金対象の推論ではないので数えない。
        self.usage: dict[str, int] = token_usage.blank()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {TOKENS.token('https://ai.azure.com')}",
                "Content-Type": "application/json"}

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.endpoint}{path}"
        url += ("&" if "?" in path else "?") + f"api-version={API_VERSION}"
        resp = self._http.request(method, url, json=body, headers=self._headers())
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text[:400]}")
        return resp.json() if resp.text else {}

    # ---------- agent management ----------
    def list_agents(self) -> dict[str, dict]:
        page = self._req("GET", "/agents?limit=100")
        return {a["id"]: a for a in page.get("data", [])}

    def ensure_agent(self, name: str, model: str, instructions: str,
                     tools: list[dict] | None = None, *,
                     effort: str = "medium", description: str = "",
                     welcome: str | None = None) -> str:
        definition = {
            "kind": "prompt",
            "model": model,
            "instructions": instructions,
            "reasoning": {"effort": effort},
            "tools": tools or [],
        }
        body: dict[str, Any] = {"name": name, "description": description,
                                "definition": definition}
        if welcome:
            body["metadata"] = {"welcomeMessage": welcome}
        exists = name in self.list_agents()
        if exists:
            # 新バージョンを作成 (ポータルの「バージョン」が増える)
            agent = self._req("POST", f"/agents/{name}/versions", body)
            logger.info("agent version added name=%s model=%s", name, model)
        else:
            agent = self._req("POST", "/agents", body)
            logger.info("agent created name=%s model=%s", name, model)
        return agent.get("name", name)

    def delete_agent(self, name: str) -> None:
        self._req("DELETE", f"/agents/{name}")

    # ---------- run (Responses API) ----------
    # Foundry 側で一過性に起きるエラー (再試行で回復しうる)
    TRANSIENT_MARKS = (
        "TaskCanceledException",      # Remote MCP が 100 秒でタイムアウト (実測)
        "did not complete the request within the configured timeout",
        "rate limit", "429", "502", "503", "504",
        "temporarily unavailable", "ServiceUnavailable",
    )

    def _is_transient(self, message: str) -> bool:
        low = message.lower()
        return any(m.lower() in low for m in self.TRANSIENT_MARKS)

    def run(
        self,
        agent_name: str,
        user_input: str,
        tool_executor: ToolExecutor | None = None,
        *,
        max_rounds: int = 12,
        retries: int = 2,
        retry_wait_s: float = 20.0,
    ) -> str:
        """エージェントを実行する。一過性エラーは待って再試行する。

        Work IQ の copilot_chat は Foundry 側 HttpClient の 100 秒制限で
        TaskCanceledException になることがある【実測 2026-08-07】。資料が多いと
        起きやすく、待って再実行すると通ることがあるため、ここで吸収する。
        """
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return self._run_once(agent_name, user_input, tool_executor,
                                      max_rounds=max_rounds)
            except RuntimeError as exc:
                last = exc
                if attempt < retries and self._is_transient(str(exc)):
                    logger.warning(
                        "transient error on %s (%d/%d); retrying in %.0fs",
                        agent_name, attempt + 1, retries, retry_wait_s)
                    time.sleep(retry_wait_s)
                    continue
                raise
        raise last  # type: ignore[misc]

    def _run_once(
        self,
        agent_name: str,
        user_input: str,
        tool_executor: ToolExecutor | None = None,
        *,
        max_rounds: int = 12,
    ) -> str:
        """エージェントを 1 回実行し、function_call を解決して最終テキストを返す。"""
        payload: dict[str, Any] = {
            "agent_reference": {"type": "agent_reference", "name": agent_name},
            "input": user_input,
        }
        for _ in range(max_rounds):
            resp = self._http.post(self.responses_url, json=payload,
                                   headers=self._headers())
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"responses -> {resp.status_code}: {resp.text[:400]}")
            data = resp.json()
            # 使用量は **status を見る前**に積む。途中で打ち切られた応答でも
            # トークンは消費されているので、失敗した回を集計から落とすと
            # 「測ると安く見える」記録になってしまう (裁定 Z)。
            token_usage.add_response(self.usage, data.get("usage"))
            if data.get("status") not in ("completed", None):
                err = data.get("error") or data.get("incomplete_details")
                raise RuntimeError(f"run {data.get('status')}: {str(err)[:300]}")

            self._log_mcp(agent_name, data)
            calls = [o for o in data.get("output", []) if o.get("type") == "function_call"]
            if not calls:
                return self._final_text(data)

            outputs = []
            for call in calls:
                fname = call["name"]
                args = json.loads(call.get("arguments") or "{}")
                logger.info("tool call agent=%s fn=%s", agent_name, fname)
                if tool_executor is None:
                    result: Any = {"error": "no tool executor bound"}
                else:
                    try:
                        result = tool_executor(fname, args)
                    except Exception as exc:
                        result = {"error": f"{type(exc).__name__}: {exc}"}
                outputs.append({
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": json.dumps(result, ensure_ascii=False)[:100000],
                })
            payload = {
                "agent_reference": {"type": "agent_reference", "name": agent_name},
                "previous_response_id": data["id"],
                "input": outputs,
            }
        raise RuntimeError(f"agent {agent_name}: tool loop exceeded {max_rounds} rounds")

    # ---------- helpers ----------
    @staticmethod
    def _final_text(data: dict) -> str:
        parts = [c.get("text", "")
                 for o in data.get("output", []) if o.get("type") == "message"
                 for c in (o.get("content") or []) if c.get("type") == "output_text"]
        return "\n".join(p for p in parts if p)

    @staticmethod
    def _log_mcp(agent_name: str, data: dict) -> None:
        """Work IQ 等の MCP 呼び出しを可視化 (本文はログに出さない)。"""
        for o in data.get("output", []):
            if o.get("type") == "mcp_call":
                out = o.get("output")
                logger.info("mcp call agent=%s server=%s tool=%s ok=%s bytes=%d",
                            agent_name, o.get("server_label"), o.get("name"),
                            not o.get("error"), len(str(out or "")))
