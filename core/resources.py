"""
core/resources.py — MCP resource handlers.

Resources vs Tools
------------------
Tools    = actions Claude invokes (RPC-style, request/response).
Resources = data Claude can read as URIs (GET-style, current state).

TelemetryBuffer
---------------
Subscribes to the bridge's telemetry stream via subscribe_telemetry().
Stores the most recent RobotState in memory.
Exposes snapshot() for the MCP resource handler and WebSocket push.

Resource URIs
-------------
robot://telemetry   — latest sensor readings + status (JSON string)
robot://state       — full state including joints (JSON string)
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from core.bridge import BaseBridge, RobotState


@dataclass
class TelemetryBuffer:
    """In-memory store for the most recent robot state."""
    _latest: RobotState | None = None

    def update(self, state: RobotState) -> None:
        self._latest = state

    def snapshot(self) -> dict:
        """Return sensors + status as a plain dict (empty if no data yet)."""
        if self._latest is None:
            return {"sensors": {}, "status": "no_data"}
        return {
            "sensors": self._latest.sensors,
            "status":  self._latest.status,
        }

    def full_snapshot(self) -> dict:
        """Return joints + sensors + status."""
        if self._latest is None:
            return {"joints": {}, "sensors": {}, "status": "no_data"}
        return {
            "joints":  self._latest.joints,
            "sensors": self._latest.sensors,
            "status":  self._latest.status,
        }

    def snapshot_json(self) -> str:
        return json.dumps(self.snapshot())

    def full_snapshot_json(self) -> str:
        return json.dumps(self.full_snapshot())


def make_buffer(bridge: BaseBridge) -> TelemetryBuffer:
    """
    Create a TelemetryBuffer and wire it to the bridge's telemetry stream.

    The bridge calls the registered coroutine each tick.
    The buffer update is synchronous (just a dict assignment) — safe to
    call from an async context without scheduling overhead.
    """
    buf = TelemetryBuffer()

    async def _on_telemetry(state: RobotState) -> None:
        buf.update(state)

    bridge.subscribe_telemetry(_on_telemetry)
    return buf
