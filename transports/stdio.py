"""
transports/stdio.py — stdio transport.

How it works
------------
FastMCP reads JSON-RPC from stdin and writes responses to stdout.
Claude Desktop (and the MCP CLI) spawn the server as a subprocess
and communicate over the process's stdio pipes.

When to use
-----------
* MCP server runs on the same machine as the Claude client.
* No network required; lowest possible setup overhead.
* Not suitable for remote robots or multi-client scenarios.

Limitations
-----------
* Single client (the parent process).
* No server-push; Claude must poll via tool calls.
* mcp.run() is synchronous — we run it in a thread executor so the
  asyncio event loop (and the bridge telemetry task) stay alive.
"""

from __future__ import annotations

import asyncio
import logging

from mcp.server.fastmcp import FastMCP

from transports.base import BaseTransport

logger = logging.getLogger(__name__)


class StdioTransport(BaseTransport):
    def __init__(self, mcp: FastMCP) -> None:
        self._mcp = mcp

    async def run(self) -> None:
        logger.info("StdioTransport starting — communicate via stdin/stdout")
        loop = asyncio.get_running_loop()
        # Run the blocking FastMCP.run() in a thread so the event loop stays
        # free to service the bridge's background telemetry task.
        await loop.run_in_executor(None, lambda: self._mcp.run(transport="stdio"))

    async def stop(self) -> None:
        # stdio lifecycle is tied to the parent process; nothing to teardown.
        pass
