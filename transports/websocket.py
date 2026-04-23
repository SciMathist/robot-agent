"""
transports/websocket.py — WebSocket transport (full-duplex JSON-RPC 2.0).

Why WebSocket instead of SSE here
----------------------------------
MCP does not yet standardise WebSocket as an official transport.
We implement the same JSON-RPC 2.0 message envelope used by MCP over a
raw WebSocket connection, giving us:

    * Full-duplex: server can push without a pending request.
    * Low latency: no HTTP request overhead per message.
    * Binary-capable: can stream raw sensor frames if needed.

This is intentionally hand-rolled because FastMCP has no WS runner yet.
The message format is 100% compatible with the MCP spec — any client that
speaks JSON-RPC 2.0 over WS can connect.

Endpoint
--------
    ws://<host>:<port>/ws

Supported JSON-RPC methods
--------------------------
    initialize          — capability negotiation (required first call)
    tools/list          — list available tools
    tools/call          — invoke a tool
    resources/list      — list available resource URIs
    resources/read      — read a resource by URI
    ping                — liveness check

Server-push (telemetry)
-----------------------
When the bridge emits a new telemetry tick the server sends an unsolicited
"notifications/message" frame to every connected client.  No request needed.

When to use
-----------
* High-frequency telemetry (>20 Hz IMU, lidar, camera streams).
* Low-latency actuation loops where HTTP round-trip matters.
* Any scenario where bidirectional streaming is more natural than SSE.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine

from aiohttp import WSMsgType, web

from config import settings
from transports.base import BaseTransport

logger = logging.getLogger(__name__)

# JSON-RPC error codes (standard)
ERR_PARSE_ERROR    = -32700
ERR_INVALID_REQ    = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL       = -32603


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

ToolHandler     = Callable[..., Coroutine[Any, Any, dict]]
ResourceHandler = Callable[[], Coroutine[Any, Any, Any]]


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

class WebSocketTransport(BaseTransport):
    """
    Full-duplex MCP-compatible JSON-RPC server over WebSocket.

    Parameters
    ----------
    tool_handlers : dict[str, ToolHandler]
        Mapping from tool name to async handler coroutine.
        Each handler is called with **arguments from the tools/call params.
    resource_handlers : dict[str, ResourceHandler]
        Mapping from resource URI to async zero-argument callable.
    """

    def __init__(
        self,
        tool_handlers: dict[str, ToolHandler],
        resource_handlers: dict[str, ResourceHandler],
    ) -> None:
        self._tools     = tool_handlers
        self._resources = resource_handlers

        # Set of active WebSocket response objects — for push broadcast
        self._clients: set[web.WebSocketResponse] = set()

        self._app    = web.Application()
        self._runner: web.AppRunner | None = None

        self._app.router.add_get("/ws",     self._ws_handler)
        self._app.router.add_get("/health", self._health_handler)

    # ------------------------------------------------------------------
    # BaseTransport interface
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, settings.host, settings.port)
        await site.start()
        logger.info(
            "WebSocketTransport started — ws://%s:%s/ws",
            settings.host, settings.port,
        )
        # Block forever (bridge + telemetry run in background tasks)
        await asyncio.get_event_loop().create_future()

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            logger.info("WebSocketTransport stopped")

    # ------------------------------------------------------------------
    # Push telemetry to all connected WS clients
    # ------------------------------------------------------------------

    async def push_telemetry(self, payload: dict[str, Any]) -> None:
        """
        Broadcast an unsolicited telemetry notification to all clients.
        Framed as a JSON-RPC 2.0 notification (no "id" field).
        """
        if not self._clients:
            return
        frame = json.dumps({
            "jsonrpc": "2.0",
            "method":  "notifications/message",
            "params": {
                "level": "info",
                "data":  payload,
            },
        })
        dead: list[web.WebSocketResponse] = []
        for ws in self._clients:
            try:
                await ws.send_str(frame)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    # ------------------------------------------------------------------
    # HTTP handlers
    # ------------------------------------------------------------------

    async def _health_handler(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "clients": len(self._clients)})

    # ------------------------------------------------------------------
    # WebSocket connection handler
    # ------------------------------------------------------------------

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        logger.info("WS client connected  [%s]  total=%d", request.remote, len(self._clients))

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._handle_frame(ws, msg.data)
                elif msg.type == WSMsgType.BINARY:
                    # Binary frames: treat as UTF-8 JSON
                    await self._handle_frame(ws, msg.data.decode("utf-8", errors="replace"))
                elif msg.type == WSMsgType.ERROR:
                    logger.warning("WS error: %s", ws.exception())
        finally:
            self._clients.discard(ws)
            logger.info("WS client disconnected  total=%d", len(self._clients))

        return ws

    # ------------------------------------------------------------------
    # JSON-RPC dispatcher
    # ------------------------------------------------------------------

    async def _handle_frame(self, ws: web.WebSocketResponse, raw: str) -> None:
        """Parse one JSON-RPC frame and send back a result or error."""
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as exc:
            await self._send_error(ws, None, ERR_PARSE_ERROR, "Parse error: %s" % exc)
            return

        rpc_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}

        if not isinstance(method, str) or not method:
            await self._send_error(ws, rpc_id, ERR_INVALID_REQ, "Invalid request")
            return

        try:
            result = await self._dispatch(method, params)
        except KeyError as exc:
            await self._send_error(ws, rpc_id, ERR_METHOD_NOT_FOUND, "Method not found: %s" % exc)
            return
        except TypeError as exc:
            await self._send_error(ws, rpc_id, ERR_INVALID_PARAMS, "Invalid params: %s" % exc)
            return
        except Exception as exc:
            logger.exception("Handler raised for method %s", method)
            await self._send_error(ws, rpc_id, ERR_INTERNAL, str(exc))
            return

        # Notifications (no id) don't get a response
        if rpc_id is None:
            return

        await ws.send_str(json.dumps({
            "jsonrpc": "2.0",
            "id":      rpc_id,
            "result":  result,
        }))

    async def _dispatch(self, method: str, params: dict) -> Any:
        """Route a method name to the correct handler."""

        # ---- capability negotiation ----
        if method == "initialize":
            return {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools":     {"listChanged": False},
                    "resources": {"listChanged": False},
                },
                "serverInfo": {"name": "robot-mcp-ws", "version": "0.1.0"},
            }

        if method == "ping":
            return {}

        # ---- tools ----
        if method == "tools/list":
            return {
                "tools": [
                    {"name": name, "description": "", "inputSchema": {"type": "object"}}
                    for name in self._tools
                ]
            }

        if method == "tools/call":
            name = params.get("name")
            if name not in self._tools:
                raise KeyError(name)
            args = params.get("arguments") or {}
            content = await self._tools[name](**args)
            return {
                "content": [{"type": "text", "text": json.dumps(content)}],
                "isError": False,
            }

        # ---- resources ----
        if method == "resources/list":
            return {
                "resources": [
                    {"uri": uri, "name": uri, "mimeType": "application/json"}
                    for uri in self._resources
                ]
            }

        if method == "resources/read":
            uri = params.get("uri")
            if uri not in self._resources:
                raise KeyError(uri)
            content = await self._resources[uri]()
            return {
                "contents": [
                    {
                        "uri":      uri,
                        "mimeType": "application/json",
                        "text":     json.dumps(content),
                    }
                ]
            }

        raise KeyError(method)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _send_error(
        self,
        ws: web.WebSocketResponse,
        rpc_id: Any,
        code: int,
        message: str,
    ) -> None:
        await ws.send_str(json.dumps({
            "jsonrpc": "2.0",
            "id":      rpc_id,
            "error":   {"code": code, "message": message},
        }))
