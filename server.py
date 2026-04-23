"""
server.py — main entrypoint.

Usage
-----
    python server.py --transport stdio
    python server.py --transport sse     --host 0.0.0.0 --port 8080
    python server.py --transport websocket --port 8081

    python server.py --transport sse --robot-uri mock --telemetry-hz 10

All flags can also be set via ROBOT_MCP_* environment variables (see config.py).

Architecture
------------
1. Parse CLI args → mutate config.settings
2. Create bridge (MockBridge / ROSBridge / …)
3. Connect bridge — starts background telemetry loop
4. Build TelemetryBuffer — subscribes to bridge emit
5. Register MCP tools and resources
6. Choose transport → run (blocks until SIGINT)
7. Disconnect bridge on exit
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from typing import Any

import config  # module-level import so we can mutate config.settings
from core.bridge import BaseBridge, create_bridge
from core.resources import TelemetryBuffer, make_buffer
from core.tools import get_telemetry, read_state, send_command
from transports.base import BaseTransport


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Robot MCP Server — connects a physical robot to Claude via MCP",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--transport",
        choices=["stdio", "sse", "websocket"],
        default=config.settings.transport,
        help="Transport layer to use",
    )
    p.add_argument("--host",          default=config.settings.host,          help="Bind host (sse/websocket)")
    p.add_argument("--port",          type=int, default=config.settings.port, help="Bind port (sse/websocket)")
    p.add_argument("--robot-uri",     default=config.settings.robot_uri,     help="Robot URI: mock | ros://... | serial://...")
    p.add_argument("--telemetry-hz",  type=float, default=config.settings.telemetry_hz, help="Telemetry frequency")
    p.add_argument("--log-level",     default=config.settings.log_level,     help="Logging level")
    return p


# ---------------------------------------------------------------------------
# Transport factory helpers
# ---------------------------------------------------------------------------

def _build_stdio_sse_transport(
    transport_name: str,
    bridge: BaseBridge,
    buf: TelemetryBuffer,
) -> BaseTransport:
    """
    Build a stdio or SSE transport using FastMCP.
    FastMCP handles JSON-RPC routing; we just register tools/resources.
    """
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("robot-mcp")

    # -- tools --------------------------------------------------------------

    @mcp.tool()
    async def robot_read_state() -> dict:
        """Read current robot joint positions and sensor values."""
        return await read_state(bridge)

    @mcp.tool()
    async def robot_send_command(joint: str, value: float) -> dict:
        """Send a positional command to a robot joint."""
        return await send_command(bridge, joint, value)

    @mcp.tool()
    async def robot_get_telemetry() -> dict:
        """Get the latest sensor telemetry snapshot."""
        return await get_telemetry(bridge)

    # -- resources ----------------------------------------------------------

    @mcp.resource("robot://telemetry")
    async def telemetry_resource() -> str:
        """Latest sensor readings and robot status."""
        return buf.snapshot_json()

    @mcp.resource("robot://state")
    async def state_resource() -> str:
        """Full robot state including joint positions."""
        return buf.full_snapshot_json()

    # -- transport ----------------------------------------------------------

    if transport_name == "stdio":
        from transports.stdio import StdioTransport
        return StdioTransport(mcp)
    else:  # sse
        from transports.sse import SSETransport
        transport = SSETransport(mcp)

        # Wire telemetry push: bridge → buffer → SSE clients
        async def _push_to_sse(state: Any) -> None:
            await transport.push_telemetry(buf.snapshot())

        bridge.subscribe_telemetry(_push_to_sse)
        return transport


def _build_websocket_transport(
    bridge: BaseBridge,
    buf: TelemetryBuffer,
) -> BaseTransport:
    """
    Build a WebSocket transport with manually registered handlers.
    The WS transport implements JSON-RPC 2.0 over WebSocket without FastMCP.
    """
    from transports.websocket import WebSocketTransport

    # -- tool handlers: keyword-arg callables ------------------------------

    async def _read_state(**_: Any) -> dict:
        return await read_state(bridge)

    async def _send_command(joint: str = "", value: float = 0.0, **_: Any) -> dict:
        return await send_command(bridge, joint, value)

    async def _get_telemetry(**_: Any) -> dict:
        return await get_telemetry(bridge)

    tool_handlers = {
        "robot_read_state":  _read_state,
        "robot_send_command": _send_command,
        "robot_get_telemetry": _get_telemetry,
    }

    # -- resource handlers: zero-arg async callables -----------------------

    async def _telemetry_resource() -> dict:
        return buf.snapshot()

    async def _state_resource() -> dict:
        return buf.full_snapshot()

    resource_handlers = {
        "robot://telemetry": _telemetry_resource,
        "robot://state":     _state_resource,
    }

    transport = WebSocketTransport(tool_handlers, resource_handlers)

    # Wire telemetry push: bridge → buffer → WS clients
    async def _push_to_ws(state: Any) -> None:
        await transport.push_telemetry(buf.snapshot())

    bridge.subscribe_telemetry(_push_to_ws)
    return transport


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    args = _build_parser().parse_args()

    # Apply CLI overrides to shared settings
    config.settings.transport    = args.transport
    config.settings.host         = args.host
    config.settings.port         = args.port
    config.settings.robot_uri    = args.robot_uri
    config.settings.telemetry_hz = args.telemetry_hz
    config.settings.log_level    = args.log_level

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    logger = logging.getLogger("server")
    logger.info(
        "Starting robot-mcp  transport=%s  robot_uri=%s  %.1f Hz",
        args.transport, args.robot_uri, args.telemetry_hz,
    )

    # -- bridge ----------------------------------------------------------------
    bridge = create_bridge(args.robot_uri)
    await bridge.connect()

    # -- telemetry buffer ------------------------------------------------------
    buf = make_buffer(bridge)

    # -- transport -------------------------------------------------------------
    if args.transport in ("stdio", "sse"):
        transport = _build_stdio_sse_transport(args.transport, bridge, buf)
    else:
        transport = _build_websocket_transport(bridge, buf)

    # -- graceful shutdown -----------------------------------------------------
    loop = asyncio.get_running_loop()

    def _shutdown() -> None:
        logger.info("Shutdown signal received")
        loop.create_task(_stop(transport, bridge))

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass  # Windows

    # -- run (blocks) ----------------------------------------------------------
    await transport.run()


async def _stop(transport: BaseTransport, bridge: BaseBridge) -> None:
    await transport.stop()
    await bridge.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
