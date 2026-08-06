"""Streamable HTTP MCP client wrapper for the Excalidraw MCP gateway.

- Endpoint: http://127.0.0.1:8000/mcp locally / ACA internal FQDN in Azure
  (env: EXCALIDRAW_MCP_URL)
- Per-call timeout and bounded retry (引き継ぎメモ §10-8)
- Optional auth header injection (ACA API key) via EXCALIDRAW_MCP_API_KEY
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from cc_core.logging_util import get_logger

logger = get_logger("cc_core.mcp_client")

DEFAULT_URL = os.environ.get("EXCALIDRAW_MCP_URL", "http://127.0.0.1:8000/mcp")
API_KEY_ENV = "EXCALIDRAW_MCP_API_KEY"
API_KEY_HEADER = os.environ.get("EXCALIDRAW_MCP_API_KEY_HEADER", "x-api-key")


class ToolCallError(RuntimeError):
    """A tool call failed after all retries."""


class ExcalidrawClient:
    """One MCP session against the Excalidraw gateway.

    Usage:
        async with ExcalidrawClient() as client:
            await client.call("create_element", {...})
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        call_timeout: float = 30.0,
        max_retries: int = 2,
        retry_backoff: float = 1.0,
    ) -> None:
        self.url = url or DEFAULT_URL
        self.call_timeout = call_timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self._cm = None
        self._session_cm = None
        self.session: ClientSession | None = None

    async def __aenter__(self) -> "ExcalidrawClient":
        headers: dict[str, str] = {}
        api_key = os.environ.get(API_KEY_ENV)
        if api_key:
            headers[API_KEY_HEADER] = api_key
        self._cm = streamablehttp_client(self.url, headers=headers or None)
        read, write, _ = await self._cm.__aenter__()
        self._session_cm = ClientSession(read, write)
        self.session = await self._session_cm.__aenter__()
        await self.session.initialize()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._session_cm is not None:
            await self._session_cm.__aexit__(*exc)
        if self._cm is not None:
            await self._cm.__aexit__(*exc)
        self.session = None

    async def list_tool_names(self) -> list[str]:
        assert self.session is not None
        result = await self.session.list_tools()
        return [t.name for t in result.tools]

    async def call(self, tool: str, args: dict[str, Any] | None = None) -> str:
        """Call a tool with timeout + bounded retry; return concatenated text content."""
        assert self.session is not None, "use 'async with ExcalidrawClient()'"
        args = args or {}
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                result = await asyncio.wait_for(
                    self.session.call_tool(tool, args), timeout=self.call_timeout
                )
                text = "\n".join(
                    c.text for c in result.content if getattr(c, "text", None)
                )
                if getattr(result, "isError", False):
                    raise ToolCallError(f"{tool}: server returned error: {text[:300]}")
                return text
            except (asyncio.TimeoutError, ToolCallError, ConnectionError, OSError) as exc:
                last_error = exc
                logger.warning(
                    "tool call failed tool=%s attempt=%d/%d err=%s",
                    tool, attempt + 1, self.max_retries + 1, type(exc).__name__,
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_backoff * (attempt + 1))
        raise ToolCallError(f"{tool}: failed after {self.max_retries + 1} attempts") from last_error

    async def call_json(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        """Call a tool and parse the first JSON object/array found in the response text."""
        text = await self.call(tool, args)
        return extract_json(text)


def extract_json(text: str) -> Any:
    """Extract a JSON value from tool response text.

    Excalidraw MCP responses mix human-readable prose with JSON payloads
    (e.g. "Element created successfully!\\n\\n{...}\\n\\n✅ Synced to canvas").
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        while start != -1:
            depth = 0
            in_str = False
            escape = False
            for i in range(start, len(text)):
                ch = text[i]
                if in_str:
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == open_ch:
                    depth += 1
                elif ch == close_ch:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start : i + 1])
                        except json.JSONDecodeError:
                            break
            start = text.find(open_ch, start + 1)
    raise ValueError(f"no JSON payload found in tool response ({len(text)} chars)")
