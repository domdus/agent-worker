"""In front of the MCP HTTP app: makes the 2026-07-28 ("modern") protocol path tolerant of proxies, or turns it off.

mcp 2.3.0 routes a POST by its `MCP-Protocol-Version` header alone: a handshake version (or no header) goes to
the initialize/Mcp-Session-Id transport, anything else to the 2026-07-28 per-request transport, which also
requires `Mcp-Method` (and `Mcp-Name` for tools/call, prompts/get, resources/read) to equal the body. A proxy
between client and worker that forwards only some headers breaks that with an HTTP 400:

- `MCP-Protocol-Version` dropped: a 2026-07-28 request lands on the session transport and gets
  `400 Bad Request: Missing session ID` (-32600), which a 2026-07-28 client cannot read as a protocol answer.
- `Mcp-Method` / `Mcp-Name` dropped: `400` HEADER_MISMATCH (-32020).
- a stale 2026-07-28 header on an `initialize` (or on a request of an initialize-era session): `400`
  INVALID_PARAMS, the body has no per-request envelope.

MCP_PROTOCOL_MODERN=auto (default) repairs these: a header that is *missing* is filled in from the body's
envelope (`params._meta`), and a 2026-07-28 header on a request without envelope that belongs to the
initialize era is dropped. A header that is present and disagrees with the body is left alone (the SDK
rejects it, as the spec says). MCP_PROTOCOL_MODERN=off answers every 2026-07-28 request with
UNSUPPORTED_PROTOCOL_VERSION (-32022) naming only the handshake versions; MCP clients in auto mode
(Python, TypeScript and Go SDKs) then fall back to `initialize`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp_types import PROTOCOL_VERSION_META_KEY, UNSUPPORTED_PROTOCOL_VERSION
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp.shared.inbound import (
    MCP_METHOD_HEADER,
    MCP_NAME_HEADER,
    MCP_PROTOCOL_VERSION_HEADER,
    NAME_BEARING_METHODS,
    encode_header_value,
)

logger = logging.getLogger(__name__)

SESSION_HEADER = b"mcp-session-id"
# The SDK refuses bodies above 4 MiB itself; bigger ones pass through untouched.
MAX_BODY = 4 * 1024 * 1024


def _envelope_version(decoded: Any) -> str | None:
    """The protocol version of a 2026-07-28 per-request envelope (params._meta), or None."""
    if not isinstance(decoded, dict):
        return None
    params = decoded.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    value = meta.get(PROTOCOL_VERSION_META_KEY) if isinstance(meta, dict) else None
    return value if isinstance(value, str) else None


def _unsupported(request_id: Any, requested: str) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": UNSUPPORTED_PROTOCOL_VERSION,
                "message": "Unsupported protocol version: this server speaks the initialize handshake "
                "(MCP_PROTOCOL_MODERN=off)",
                "data": {"supported": list(HANDSHAKE_PROTOCOL_VERSIONS), "requested": requested},
            },
        },
        separators=(",", ":"),
    ).encode()


class ModernProtocolShim:
    """ASGI middleware for the MCP endpoint (see the module docstring)."""

    def __init__(self, app: ASGIApp, mode: str = "auto", path: str = "/mcp") -> None:
        self.app = app
        self.mode = "off" if mode == "off" else "auto"
        self.path = path.rstrip("/") or "/"
        self.repairs: dict[str, int] = {}

    def _count(self, kind: str) -> None:
        first = kind not in self.repairs
        self.repairs[kind] = self.repairs.get(kind, 0) + 1
        # Once at INFO per kind (a proxy does it on every request), then at DEBUG.
        (logger.info if first else logger.debug)(
            "MCP 2026-07-28: %s (a proxy between client and worker may drop MCP headers)", kind)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or (scope.get("path") or "").rstrip("/") != self.path
        ):
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        size = 0
        more = True
        extra: list[Message] = []
        while more:
            message = await receive()
            if message["type"] != "http.request":
                extra.append(message)
                break
            chunk = message.get("body", b"")
            chunks.append(chunk)
            size += len(chunk)
            more = bool(message.get("more_body"))
            if size > MAX_BODY:
                break
        body = b"".join(chunks)
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": more and not extra}
            if extra:
                return extra.pop(0)
            return await receive()

        if size > MAX_BODY or more:
            await self.app(scope, replay, send)
            return

        try:
            decoded = json.loads(body) if body else None
        except (ValueError, RecursionError):
            decoded = None
        headers: list[tuple[bytes, bytes]] = list(scope.get("headers") or [])
        names = {k.lower() for k, _ in headers}
        header_version = next(
            (v.decode("latin-1") for k, v in headers if k.lower() == MCP_PROTOCOL_VERSION_HEADER.encode()), None)
        envelope = _envelope_version(decoded)
        modern_header = header_version is not None and header_version not in HANDSHAKE_PROTOCOL_VERSIONS

        if self.mode == "off":
            if envelope is not None or modern_header:
                if isinstance(decoded, dict) and "id" not in decoded:
                    await _respond(send, 202, b"")
                    return
                request_id = decoded.get("id") if isinstance(decoded, dict) else None
                self._count("refused (MCP_PROTOCOL_MODERN=off)")
                await _respond(send, 400, _unsupported(request_id, envelope or header_version or ""))
                return
            await self.app(scope, replay, send)
            return

        changed = False
        if envelope is not None and envelope in MODERN_PROTOCOL_VERSIONS and isinstance(decoded, dict):
            if MCP_PROTOCOL_VERSION_HEADER.encode() not in names:
                headers.append((MCP_PROTOCOL_VERSION_HEADER.encode(), envelope.encode("latin-1")))
                self._count("MCP-Protocol-Version header missing, taken from the request envelope")
                changed = True
            method = decoded.get("method")
            if isinstance(method, str) and MCP_METHOD_HEADER.encode() not in names:
                headers.append((MCP_METHOD_HEADER.encode(), method.encode("latin-1", "replace")))
                self._count("Mcp-Method header missing, taken from the body")
                changed = True
            key = NAME_BEARING_METHODS.get(method) if isinstance(method, str) else None
            params = decoded.get("params")
            value = params.get(key) if key and isinstance(params, dict) else None
            if isinstance(value, str) and MCP_NAME_HEADER.encode() not in names:
                headers.append((MCP_NAME_HEADER.encode(), encode_header_value(value).encode("latin-1")))
                self._count("Mcp-Name header missing, taken from the body")
                changed = True
        elif modern_header and envelope is None and isinstance(decoded, dict) and (
            decoded.get("method") == "initialize" or SESSION_HEADER in names
        ):
            # An initialize-era request (handshake, or one of a session) with a 2026-07-28 header it did not
            # mean: serve it on the initialize-era transport instead of refusing it.
            headers = [(k, v) for k, v in headers if k.lower() != MCP_PROTOCOL_VERSION_HEADER.encode()]
            self._count("2026-07-28 header on an initialize-era request dropped")
            changed = True
        if changed:
            scope = {**scope, "headers": headers}
        await self.app(scope, replay, send)


async def _respond(send: Send, status: int, body: bytes) -> None:
    headers = [(b"content-type", b"application/json")] if body else []
    headers.append((b"content-length", str(len(body)).encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
