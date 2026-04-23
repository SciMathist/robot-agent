"""
transports/sse.py — HTTP + Server-Sent Events transport.

How it works
------------
FastMCP exposes two HTTP endpoints via a Starlette / Uvicorn app:

    GET  /sse       — client opens a persistent SSE stream for notifications
    POST /messages  — client sends JSON-RPC tool calls; responses come back
                      either as HTTP response bodies or as SSE events

Telemetry push
--------------
The bridge's telemetry loop calls push_telemetry() on this transport.
We fan-out a JSON-RPC "notifications/message" event to every open SSE
connection.  This lets Claude receive live sensor updates without polling.

When to use
-----------
* Networked setup: Claude (cloud) ↔ robot (edge/LAN).
* Moderate telemetry frequency (up to ~20 Hz before SSE overhead matters).
* Standard MCP-compliant transport — works with Claude.ai and Claude Desktop.

Limitations
-----------
* SSE is unidirectional (server → client).  Commands still come in via POST.
* mcp.run() is synchronous — runs in a thread executor.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from config import settings
from transports.base import BaseTransport

logger = logging.getLogger(__name__)


class SSETransport(BaseTransport):
    def __init__(self, mcp: FastMCP) -> None:
        self._mcp = mcp
        # Active SSE response queues — one per connected client.
        self._queues: list[asyncio.Queue] = []

    # ------------------------------------------------------------------
    # BaseTransport interface
    # ------------------------------------------------------------------

    async def run(self) -> None:
        logger.info(
            "SSETransport starting — http://%s:%s  (SSE: /sse, POST: /messages)",
            settings.host,
            settings.port,
        )
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: self._mcp.run(
                transport="sse",
                host=settings.host,
                port=settings.port,
            ),
        )

    async def stop(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Push telemetry to all connected SSE clients
    # ------------------------------------------------------------------

    async def push_telemetry(self, payload: dict[str, Any]) -> None:
        """
        Enqueue a telemetry notification for every active SSE connection.

        Encoded as a JSON-RPC 2.0 "notifications/message" event so the
        MCP client can distinguish it from tool-call responses.
        """
        if not self._queues:
            return
        event = json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/message",
            "params": {
                "level": "info",
                "data": payload,
            },
        })
        dead: list[asyncio.Queue] = []
        for q in self._queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._queues.remove(q)

    def register_sse_queue(self, q: asyncio.Queue) -> None:
        """Called by the Starlette SSE endpoint when a client connects."""
        self._queues.append(q)

    def unregister_sse_queue(self, q: asyncio.Queue) -> None:
        """Called when a client disconnects."""
        self._queues.discard(q) if hasattr(self._queues, "discard") else None
        if q in self._queues:
            self._queues.remove(q)
