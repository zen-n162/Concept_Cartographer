"""Foundry Agent Service (assistants 互換 REST API) の薄いクライアント。

- ensure_agents(): agents/ の定義に基づき 4 エージェントを作成/更新
- run_agent():     thread 作成 → run → function tool ループ → 最終応答テキスト

SDK ではなく REST を直接使う (この環境では azure-identity/cryptography が
使えないため。API は動作確認済み: GET /assistants -> 200)。

【gpt-5.6 系フォールバック】2026-08 時点、Agent Service ランタイムは run に
temperature/top_p (既定 1.0) を必ず付与するが、gpt-5.6-sol/terra/luna と gpt-5.5
はこれを拒否して run が invalid_prompt で失敗する (gpt-5 / gpt-5.4-mini は動作)。
このため run_agent は当該失敗を検出すると、同じ Foundry プロジェクトの
Chat Completions API (同一デプロイ・同一 instructions・同一 function tools) へ
自動フォールバックする。ランタイム側が修正されれば自動的に Agent Service 実行に戻る。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import httpx

from cc_core.logging_util import get_logger
from cc_orchestrator import API_VERSION, FOUNDRY_PROJECT_ENDPOINT
from cc_orchestrator.token_provider import TOKENS

logger = get_logger("cc_orchestrator.foundry")

ToolExecutor = Callable[[str, dict[str, Any]], dict[str, Any]]


CHAT_COMPLETIONS_URL = "https://rg-cartographer.openai.azure.com/openai/v1/chat/completions"
_UNSUPPORTED_PARAM = "Unsupported parameter"


class FoundryAgents:
    def __init__(self, endpoint: str = FOUNDRY_PROJECT_ENDPOINT) -> None:
        self.endpoint = endpoint.rstrip("/")
        self._http = httpx.Client(timeout=300)
        self._force_fallback: set[str] = set()  # agent_id -> 以後 chat completions 直行

    # ---------- low-level ----------
    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.endpoint}{path}"
        sep = "&" if "?" in path else "?"
        url = f"{url}{sep}api-version={API_VERSION}"
        headers = {"Authorization": f"Bearer {TOKENS.token('https://ai.azure.com')}"}
        resp = self._http.request(method, url, json=body, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text[:400]}")
        return resp.json() if resp.text else {}

    # ---------- agent management ----------
    def list_agents(self) -> dict[str, dict]:
        agents: dict[str, dict] = {}
        after = ""
        while True:
            path = "/assistants?limit=100" + (f"&after={after}" if after else "")
            page = self._req("GET", path)
            for a in page.get("data", []):
                agents[a["name"]] = a
            if not page.get("has_more"):
                return agents
            after = page["last_id"]

    def ensure_agent(self, name: str, model: str, instructions: str,
                     tools: list[dict] | None = None) -> str:
        existing = self.list_agents().get(name)
        # NOTE: gpt-5.6 系 (推論モデル) は temperature 非対応のため指定しない
        body = {
            "name": name,
            "model": model,
            "instructions": instructions,
            "tools": tools or [],
        }
        if existing:
            agent = self._req("POST", f"/assistants/{existing['id']}", body)
            logger.info("agent updated name=%s model=%s id=%s", name, model, agent["id"])
        else:
            agent = self._req("POST", "/assistants", body)
            logger.info("agent created name=%s model=%s id=%s", name, model, agent["id"])
        return agent["id"]

    # ---------- chat completions fallback ----------
    def _chat_fallback(
        self,
        spec: dict[str, Any],
        user_message: str,
        tool_executor: ToolExecutor | None,
        json_response: bool,
        max_iters: int = 10,
    ) -> str:
        logger.info("chat-completions fallback model=%s", spec["model"])
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": spec["instructions"]},
            {"role": "user", "content": user_message},
        ]
        headers = {"Authorization":
                   f"Bearer {TOKENS.token('https://cognitiveservices.azure.com')}"}
        for _ in range(max_iters):
            body: dict[str, Any] = {"model": spec["model"], "messages": messages}
            if spec.get("tools"):
                body["tools"] = spec["tools"]
            if json_response:
                body["response_format"] = {"type": "json_object"}
            resp = self._http.post(CHAT_COMPLETIONS_URL, json=body, headers=headers)
            if resp.status_code >= 400:
                raise RuntimeError(f"chat completions -> {resp.status_code}: {resp.text[:300]}")
            msg = resp.json()["choices"][0]["message"]
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return msg.get("content") or ""
            messages.append({"role": "assistant", "content": msg.get("content"),
                             "tool_calls": tool_calls})
            for call in tool_calls:
                fn = call["function"]["name"]
                args = json.loads(call["function"]["arguments"] or "{}")
                logger.info("tool call (fallback) model=%s fn=%s", spec["model"], fn)
                if tool_executor is None:
                    result: dict[str, Any] = {"error": "no tool executor bound"}
                else:
                    try:
                        result = tool_executor(fn, args)
                    except Exception as exc:
                        result = {"error": f"{type(exc).__name__}: {exc}"}
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps(result, ensure_ascii=False)[:80000]})
        raise RuntimeError("chat completions tool loop exceeded max iterations")

    # ---------- run loop ----------
    def run_agent(
        self,
        agent_id: str,
        user_message: str,
        tool_executor: ToolExecutor | None = None,
        *,
        json_response: bool = False,
        max_wait_s: float = 900.0,
        fallback_spec: dict[str, Any] | None = None,
    ) -> str:
        if fallback_spec is not None and agent_id in self._force_fallback:
            return self._chat_fallback(fallback_spec, user_message,
                                       tool_executor, json_response)
        try:
            return self._run_agent_service(
                agent_id, user_message, tool_executor,
                json_response=json_response, max_wait_s=max_wait_s)
        except RuntimeError as exc:
            if fallback_spec is not None and _UNSUPPORTED_PARAM in str(exc):
                logger.warning(
                    "Agent Service run rejected model params (gpt-5.6 系非互換) — "
                    "chat completions へフォールバック")
                self._force_fallback.add(agent_id)
                return self._chat_fallback(fallback_spec, user_message,
                                           tool_executor, json_response)
            raise

    def _run_agent_service(
        self,
        agent_id: str,
        user_message: str,
        tool_executor: ToolExecutor | None = None,
        *,
        json_response: bool = False,
        max_wait_s: float = 900.0,
    ) -> str:
        thread = self._req("POST", "/threads", {})
        thread_id = thread["id"]
        self._req("POST", f"/threads/{thread_id}/messages",
                  {"role": "user", "content": user_message})

        # gpt-5.6 系推論モデルは temperature/top_p 非対応。assistant 既定値 (1.0) が
        # run に継承されて invalid_prompt になるため、明示的に null で上書きする。
        run_body: dict[str, Any] = {
            "assistant_id": agent_id,
            "temperature": None,
            "top_p": None,
        }
        if json_response:
            run_body["response_format"] = {"type": "json_object"}
        try:
            run = self._req("POST", f"/threads/{thread_id}/runs", run_body)
        except RuntimeError as exc:
            if json_response and "response_format" in str(exc):
                run = self._req("POST", f"/threads/{thread_id}/runs",
                                {"assistant_id": agent_id})
            else:
                raise

        deadline = time.time() + max_wait_s
        while True:
            if time.time() > deadline:
                raise TimeoutError(f"agent run timed out (agent={agent_id})")
            run = self._req("GET", f"/threads/{thread_id}/runs/{run['id']}")
            status = run["status"]
            if status == "requires_action":
                calls = run["required_action"]["submit_tool_outputs"]["tool_calls"]
                outputs = []
                for call in calls:
                    fn = call["function"]["name"]
                    args = json.loads(call["function"]["arguments"] or "{}")
                    logger.info("tool call agent=%s fn=%s", agent_id, fn)
                    if tool_executor is None:
                        result: dict[str, Any] = {"error": "no tool executor bound"}
                    else:
                        try:
                            result = tool_executor(fn, args)
                        except Exception as exc:  # ツール失敗はエージェントに返す
                            result = {"error": f"{type(exc).__name__}: {exc}"}
                    outputs.append({
                        "tool_call_id": call["id"],
                        "output": json.dumps(result, ensure_ascii=False)[:80000],
                    })
                run = self._req(
                    "POST",
                    f"/threads/{thread_id}/runs/{run['id']}/submit_tool_outputs",
                    {"tool_outputs": outputs},
                )
            elif status in ("queued", "in_progress", "cancelling"):
                time.sleep(2.0)
            elif status == "completed":
                break
            else:
                err = run.get("last_error") or {}
                raise RuntimeError(f"run {status}: {err.get('code')}: {err.get('message')}")

        msgs = self._req("GET", f"/threads/{thread_id}/messages?limit=5&order=desc")
        for m in msgs.get("data", []):
            if m["role"] == "assistant":
                parts = [c["text"]["value"] for c in m["content"] if c["type"] == "text"]
                return "\n".join(parts)
        raise RuntimeError("no assistant message in thread")


def fn_tool(name: str, description: str, params: dict[str, Any],
            required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": params, "required": required},
        },
    }
