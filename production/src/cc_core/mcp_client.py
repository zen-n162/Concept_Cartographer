"""Streamable HTTP MCP client wrapper for the Excalidraw MCP gateway.

- Endpoint: http://127.0.0.1:8000/mcp locally / ACA internal FQDN in Azure
  (env: EXCALIDRAW_MCP_URL)
- Connect timeout (接続 + initialize) と per-call timeout / bounded retry
  (引き継ぎメモ §10-8 + 描画ハング恒久対処 計画 B)
- Optional auth header injection (ACA API key) via EXCALIDRAW_MCP_API_KEY
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from cc_core.logging_util import get_logger

logger = get_logger("cc_core.mcp_client")

DEFAULT_URL = os.environ.get("EXCALIDRAW_MCP_URL", "http://127.0.0.1:8000/mcp")
API_KEY_ENV = "EXCALIDRAW_MCP_API_KEY"
API_KEY_HEADER = os.environ.get("EXCALIDRAW_MCP_API_KEY_HEADER", "x-api-key")

DEFAULT_CONNECT_TIMEOUT = 10.0
"""接続 + initialize の上限 (秒)。call_timeout は**ツール呼び出しにしか効かない**
ので、これが無いと「ポートは応答するが SSE が返らない」半死のゲートウェイに対して
`__aenter__` が永久に待つ【実測 2026-08-07: ジョブが数時間 running のまま固まった】。"""

DEFAULT_HEALTH_URL = "http://127.0.0.1:8000/healthz"
HEALTH_URL_ENV = "CC_GATEWAY_HEALTH"


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
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        max_retries: int = 2,
        retry_backoff: float = 1.0,
    ) -> None:
        self.url = url or DEFAULT_URL
        self.call_timeout = call_timeout
        self.connect_timeout = connect_timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self._cm = None
        self._session_cm = None
        self.session: ClientSession | None = None

    async def __aenter__(self) -> "ExcalidrawClient":
        """接続する。connect_timeout を超えたら ConnectionError (無限待ちしない)。

        タイムアウト時は**開きかけの context manager を必ず閉じる**。閉じずに
        捨てると HTTP 接続とタスクグループが残り、次の試行が同じ相手に対して
        さらに待つことになる。

        時間切れの仕掛けに `asyncio.timeout` を使うのが要点 — `wait_for` は
        中身を**別タスク**で走らせるため、MCP (anyio) のキャンセルスコープを
        入ったのと違うタスクで抜けることになり、**接続に成功した経路まで**
        `RuntimeError: Attempted to exit cancel scope in a different task` で
        壊れる【実測: 実 canvas への --render が失敗した】。
        """
        try:
            async with asyncio.timeout(self.connect_timeout):
                await self._connect()
        except asyncio.TimeoutError as exc:
            await self._close_partial()
            raise ConnectionError(
                f"MCP gateway に接続できません ({self.url} / "
                f"{self.connect_timeout:.0f} 秒で打ち切り)"
            ) from exc
        except asyncio.CancelledError as exc:
            # ゲートウェイが落ちている場合、initialize は anyio のスコープ
            # キャンセルとして現れ、**本当の理由 (接続拒否) は後始末のときに
            # 出てくる**。CancelledError のまま投げると BaseException なので
            # 呼び出し側の except Exception をすり抜け、原因も分からない。
            cause = await self._close_partial()
            if cause is None:
                raise
            raise ConnectionError(
                f"MCP gateway に接続できません ({self.url}): {_brief(cause)}"
            ) from cause
        except BaseException:
            await self._close_partial()
            raise
        return self

    async def _connect(self) -> None:
        headers: dict[str, str] = {}
        api_key = os.environ.get(API_KEY_ENV)
        if api_key:
            headers[API_KEY_HEADER] = api_key
        self._cm = streamablehttp_client(self.url, headers=headers or None)
        read, write, _ = await self._cm.__aenter__()
        self._session_cm = ClientSession(read, write)
        self.session = await self._session_cm.__aenter__()
        await self.session.initialize()

    async def _close_partial(self) -> BaseException | None:
        """開きかけの後始末。**失敗の理由になりそうな例外を 1 つ返す**。

        後始末そのものは best effort で、ここで投げ直すことはしない (本来の
        失敗理由を後始末の失敗で覆い隠さないため)。ただし MCP/anyio は
        「接続できなかった」ことをこの `__aexit__` の側で知らせてくるので、
        最初に捕まえた例外だけは呼び出し元へ返して理由に使う。
        """
        first: BaseException | None = None
        for attr in ("_session_cm", "_cm"):
            cm = getattr(self, attr)
            setattr(self, attr, None)
            if cm is None:
                continue
            try:
                await cm.__aexit__(None, None, None)
            except BaseException as exc:  # noqa: BLE001 - 後始末は best effort
                logger.debug("cleanup after failed connect: %s", type(exc).__name__)
                if first is None:
                    first = exc
        self.session = None
        return first

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


def _brief(exc: BaseException) -> str:
    """例外を 1 行にする。ExceptionGroup は中身の 1 つ目まで開く。

    anyio のスコープキャンセルは "Cancelled via cancel scope 0x… by <Task …>"
    という**読んでも何も分からない**文字列になるので、そこだけ言い換える。
    """
    inner = getattr(exc, "exceptions", None)
    if inner:
        exc = inner[0]
    if isinstance(exc, asyncio.CancelledError):
        return "ゲートウェイが応答しません (接続が中断されました)"
    text = str(exc).strip() or type(exc).__name__
    return f"{type(exc).__name__}: {text}"[:200]


def gateway_health_url() -> str:
    """ゲートウェイのヘルス URL (env CC_GATEWAY_HEALTH で上書き可)。

    呼び出しのたびに環境変数を読む — import 時に固めるとテストや起動スクリプト
    からの上書きが効かない。
    """
    return os.environ.get(HEALTH_URL_ENV) or DEFAULT_HEALTH_URL


def gateway_healthy(url: str | None = None, *, timeout: float = 3.0) -> bool:
    """MCP gateway が生きているかを短時間で確かめる (計画 C のプリフライト)。

    「描きに行ってから固まる」より「描く前に落ちていると分かる」ほうが速い。
    どんな失敗 (接続拒否・タイムアウト・4xx/5xx) も False を返し、例外は投げない。
    """
    target = url or gateway_health_url()
    try:
        with urllib.request.urlopen(target, timeout=timeout) as resp:  # noqa: S310
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
            return 200 <= int(status) < 300
    except (urllib.error.URLError, OSError, ValueError, AttributeError) as exc:
        logger.warning("gateway health check failed url=%s err=%s",
                       target, type(exc).__name__)
        return False


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
