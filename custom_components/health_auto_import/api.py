"""Async TCP client for the Health Auto Export iOS app's MCP/JSON-RPC server.

Wire format (legacy v0.0.1):

    { "jsonrpc": "2.0", "id": <id>, "method": "callTool",
      "params": { "name": <tool>, "arguments": {...} } }

One JSON-RPC request per TCP connection. Send request + newline, read until the
buffer parses as JSON, close. See 02-hae-tcp-protocol.md.

Security notes:
 - Read buffer capped at MAX_RESPONSE_BYTES (4 MiB) to prevent OOM from a
   rogue/compromised server.
 - JSON-RPC IDs use a monotonic counter, not random (no entropy leak).
 - All server-returned strings pass through sanitise helpers before reaching HA
   entity IDs or file paths (see slugify callers in coordinator.py / sensor.py).
 - No TLS yet (server doesn't support it); see §6 open question.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .const import (
    CONNECT_TIMEOUT_S,
    INTER_REQUEST_DELAY_S,
    MAX_RESPONSE_BYTES,
    READ_CHUNK_BYTES,
    READ_TIMEOUT_S,
    RPC_METHOD_CALL_TOOL,
    RPC_METHOD_LIST_TOOLS,
    RPC_METHOD_LIST_TOOLS_LEGACY,
)

_LOGGER = logging.getLogger(__name__)

# Module-level monotonic ID counter — no entropy leak, no collisions.
_next_rpc_id: int = 0


def _rpc_id() -> int:
    global _next_rpc_id  # noqa: PLW0603
    _next_rpc_id += 1
    return _next_rpc_id


class HaeError(Exception):
    """Base error for HAE API problems."""


class HaeTransportError(HaeError):
    """TCP-level failure: connect refused, read timeout, closed early, etc."""


class HaeProtocolError(HaeError):
    """JSON-RPC-level failure: malformed envelope or `error` key in response."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def extract_payload(rpc_response: dict[str, Any]) -> dict[str, Any]:
    """Unwrap both the direct (``result.data``) and MCP (``result.content[0].text``) shapes.

    Returns an empty dict when the server signals "no data".
    """
    result = rpc_response.get("result")
    if not isinstance(result, dict):
        return {}
    if "data" in result:
        data = result["data"]
        return data if isinstance(data, dict) else {}
    content = result.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and isinstance(first.get("text"), str):
            try:
                parsed = json.loads(first["text"])
            except (json.JSONDecodeError, ValueError) as err:
                raise HaeProtocolError(
                    f"content[0].text not valid JSON: {err}"
                ) from err
            if isinstance(parsed, dict):
                inner = parsed.get("data")
                return inner if isinstance(inner, dict) else {}
    return {}


class HaeClient:
    """Async, connection-per-request client for the HAE TCP server.

    Each public method opens one TCP connection, sends one JSON-RPC request,
    reads one response, and closes. A connection lock ensures only one request
    is in flight at a time — the HAE iOS app's TCP server freezes under
    concurrent connections.
    """

    def __init__(self, host: str, port: int) -> None:
        # Host/port validated upstream in config_flow; stored immutably.
        self._host = host
        self._port = port
        self._lock = asyncio.Lock()
        self._last_success: float | None = None  # monotonic timestamp

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    def seconds_since_last_success(self) -> float | None:
        """Seconds since last successful RPC response, or None if never."""
        if self._last_success is None:
            return None
        return asyncio.get_event_loop().time() - self._last_success

    async def probe(self) -> bool:
        """Cheap TCP connect + immediate close, used by the reachability sensor."""
        async with self._lock:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self._host, self._port),
                    timeout=CONNECT_TIMEOUT_S,
                )
            except (OSError, asyncio.TimeoutError):
                return False
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass
            return True

    async def list_tools(self) -> list[dict[str, Any]] | None:
        """Return the tool catalog, or ``None`` if unsupported.

        Tries legacy ``listTools`` first (confirmed working on v0.0.1), then
        standard MCP ``tools/list``. Returns list of tool descriptor dicts
        (each has at minimum ``name: str``).
        """
        for method in (RPC_METHOD_LIST_TOOLS_LEGACY, RPC_METHOD_LIST_TOOLS):
            try:
                resp = await self._send({"method": method, "params": {}})
            except HaeTransportError:
                continue
            except HaeProtocolError as exc:
                # -32601 = method not found on this server version — try next.
                if exc.code == -32601:
                    continue
                raise
            tools_raw = (resp.get("result") or {}).get("tools")
            if not isinstance(tools_raw, list):
                continue
            tools: list[dict[str, Any]] = []
            for t in tools_raw:
                if isinstance(t, dict) and isinstance(t.get("name"), str):
                    tools.append(t)
                elif isinstance(t, str):
                    tools.append({"name": t})
            return tools
        return None

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Invoke a HAE tool. Returns the unwrapped ``data`` payload.

        Raises ``HaeProtocolError`` on JSON-RPC errors (including unknown tool
        signalled as ``-32602``).
        """
        resp = await self._send(
            {
                "method": RPC_METHOD_CALL_TOOL,
                "params": {"name": name, "arguments": arguments},
            }
        )
        err = resp.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            msg = err.get("message", "unknown error")
            raise HaeProtocolError(
                f"{name}: [{code}] {msg}", code=int(code) if code is not None else None
            )
        return extract_payload(resp)

    # ---- private --------------------------------------------------------------

    async def _send(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Open a TCP connection, send one JSON-RPC request, read one response.

        Security:
         - Read buffer capped at MAX_RESPONSE_BYTES to prevent OOM.
         - Hard read timeout prevents hanging on a stalled server.
         - Connection is always closed in the ``finally`` block.
         - Connection lock prevents concurrent requests that freeze HAE.
        """
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": _rpc_id(),
            **envelope,
        }
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8") + b"\n"

        async with self._lock:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self._host, self._port),
                    timeout=CONNECT_TIMEOUT_S,
                )
            except (OSError, asyncio.TimeoutError) as err:
                raise HaeTransportError(
                    f"connect {self._host}:{self._port}: {err}"
                ) from err

            try:
                writer.write(payload)
                try:
                    await writer.drain()
                except OSError as err:
                    raise HaeTransportError(
                        f"write error: {err}"
                    ) from err

                buf = bytearray()
                deadline = asyncio.get_running_loop().time() + READ_TIMEOUT_S
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise HaeTransportError("read timeout")
                    try:
                        chunk = await asyncio.wait_for(
                            reader.read(READ_CHUNK_BYTES), timeout=remaining
                        )
                    except asyncio.TimeoutError as err:
                        raise HaeTransportError("read timeout") from err
                    except OSError as err:
                        raise HaeTransportError(
                            f"read error: {err}"
                        ) from err
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if len(buf) > MAX_RESPONSE_BYTES:
                        raise HaeTransportError(
                            f"response exceeded {MAX_RESPONSE_BYTES} bytes — "
                            "aborting to prevent OOM"
                        )
                    try:
                        result = json.loads(buf.decode("utf-8").strip())
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    self._last_success = asyncio.get_event_loop().time()
                    return result
                if not buf:
                    raise HaeTransportError(
                        "connection closed before any data received"
                    )
                raise HaeTransportError(
                    "connection closed before parseable JSON response"
                )
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
                # Cooldown: give the iOS app breathing room between requests.
                await asyncio.sleep(INTER_REQUEST_DELAY_S)
