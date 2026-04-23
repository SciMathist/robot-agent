"""
transports/base.py — abstract transport contract.

Every concrete transport must implement:
    run()   — start the server; blocks until stopped
    stop()  — initiate a graceful shutdown

The base class also provides a small helper for push notifications
so SSE and WebSocket transports can broadcast telemetry updates
without duplicating fan-out logic.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class BaseTransport(ABC):

    @abstractmethod
    async def run(self) -> None:
        """Start serving.  Should block until the server is stopped."""

    @abstractmethod
    async def stop(self) -> None:
        """Initiate a clean shutdown."""

    # ------------------------------------------------------------------
    # Optional push helper
    # Used by SSE and WebSocket transports to broadcast telemetry.
    # stdio has no push concept (Claude polls via tool calls).
    # ------------------------------------------------------------------

    async def push_telemetry(self, payload: dict[str, Any]) -> None:
        """
        Broadcast a telemetry payload to all connected clients.
        Override in transports that support server-push.
        Default implementation is a no-op (stdio).
        """
