"""
core/bridge.py — hardware abstraction layer.

Hierarchy
---------
BaseBridge          abstract base; defines the contract
  MockBridge        simulated robot (random sensors, logged commands)
  ROSBridge         stub — implement with rospy / rclpy
  SerialBridge      stub — implement with pyserial

All transports and tool handlers talk ONLY to BaseBridge.
Swap the concrete bridge via config.robot_uri at startup.

Telemetry callbacks
-------------------
Any layer can subscribe to live telemetry via subscribe_telemetry(coro).
The bridge calls every registered coroutine each time new sensor data
arrives.  The SSE and WebSocket transports use this to push notifications.
"""

from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RobotState:
    """Snapshot of the robot at a single point in time."""
    joints: dict[str, float] = field(default_factory=dict)
    sensors: dict[str, float] = field(default_factory=dict)
    status: str = "idle"


TelemetryCallback = Callable[[RobotState], Awaitable[None]]


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class BaseBridge(ABC):
    def __init__(self) -> None:
        self._state = RobotState()
        self._callbacks: list[TelemetryCallback] = []
        self._running = False

    # -- lifecycle ----------------------------------------------------------

    @abstractmethod
    async def connect(self) -> None:
        """Open connection to the robot / simulator."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection cleanly."""

    # -- operations ---------------------------------------------------------

    @abstractmethod
    async def read_state(self) -> RobotState:
        """Return the latest known robot state."""

    @abstractmethod
    async def send_command(self, joint: str, value: float) -> dict:
        """
        Send a positional command to a named joint.

        Returns a result dict with at minimum {"status": "ok" | "error"}.
        """

    # -- telemetry subscription --------------------------------------------

    def subscribe_telemetry(self, callback: TelemetryCallback) -> None:
        """Register an async callback invoked on every telemetry update."""
        self._callbacks.append(callback)

    async def _emit(self, state: RobotState) -> None:
        """Fan-out a state snapshot to all subscribers."""
        for cb in self._callbacks:
            try:
                await cb(state)
            except Exception as exc:
                logger.warning("Telemetry callback raised: %s", exc)


# ---------------------------------------------------------------------------
# Mock bridge
# ---------------------------------------------------------------------------

class MockBridge(BaseBridge):
    """
    Fully simulated robot.  No hardware required.

    * Joints start at 0.0 and track whatever send_command sets.
    * Sensors (temperature, voltage, current) are randomised each tick.
    * A background asyncio task drives the telemetry loop at telemetry_hz.
    """

    async def connect(self) -> None:
        self._running = True
        self._state.joints = {"base": 0.0, "shoulder": 0.0, "elbow": 0.0, "wrist": 0.0}
        self._state.status = "running"
        asyncio.create_task(self._loop(), name="mock-telemetry")
        logger.info("MockBridge connected — simulated robot ready")

    async def disconnect(self) -> None:
        self._running = False
        logger.info("MockBridge disconnected")

    async def read_state(self) -> RobotState:
        return self._state

    async def send_command(self, joint: str, value: float) -> dict:
        if joint not in self._state.joints:
            return {"status": "error", "reason": "unknown joint %s" % joint}
        self._state.joints[joint] = value
        logger.info("MockBridge ← command  joint=%s  value=%s", joint, value)
        return {"status": "ok", "joint": joint, "value": value}

    async def _loop(self) -> None:
        from config import settings
        interval = 1.0 / settings.telemetry_hz
        while self._running:
            self._state.sensors = {
                "temperature_c": round(random.uniform(20.0, 30.0), 2),
                "voltage_v":     round(random.uniform(11.5, 12.5), 2),
                "current_a":     round(random.uniform(0.5,  2.0),  2),
            }
            await self._emit(self._state)
            await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# ROS bridge stub
# ---------------------------------------------------------------------------

class ROSBridge(BaseBridge):
    """
    Stub for ROS 1 (rospy) or ROS 2 (rclpy) integration.

    Implementation guide
    --------------------
    connect()       : call rospy.init_node() or rclpy.init(); spin in thread
    disconnect()    : rospy.signal_shutdown() / rclpy.shutdown()
    read_state()    : read from subscribed topic callbacks stored on self._state
    send_command()  : publish to a JointTrajectory or std_msgs/Float64 topic
    _loop()         : subscribe to sensor topics and call self._emit() on each msg
    """

    async def connect(self) -> None:
        raise NotImplementedError(
            "ROSBridge: implement connect() with rospy/rclpy, "
            "then set robot_uri='ros://localhost'."
        )

    async def disconnect(self) -> None:
        raise NotImplementedError

    async def read_state(self) -> RobotState:
        raise NotImplementedError

    async def send_command(self, joint: str, value: float) -> dict:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Serial bridge stub
# ---------------------------------------------------------------------------

class SerialBridge(BaseBridge):
    """
    Stub for microcontroller / serial-protocol robots.

    Expected robot_uri format:  serial://COM3:115200
    or serial:///dev/ttyUSB0:115200
    """

    def __init__(self, robot_uri: str) -> None:
        super().__init__()
        self._robot_uri = robot_uri
        self._serial_port = ""
        self._baudrate = 115200
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._io_lock = asyncio.Lock()

    def _parse_robot_uri(self) -> tuple[str, int]:
        prefix = "serial://"
        if not self._robot_uri.startswith(prefix):
            raise ValueError("SerialBridge requires a serial:// URI")

        target = self._robot_uri[len(prefix) :]
        if not target:
            raise ValueError("SerialBridge requires a port and baudrate")

        port, separator, baud_text = target.rpartition(":")
        if not separator:
            return target.lstrip("/"), 115200

        port = port.lstrip("/")
        if not port:
            raise ValueError("SerialBridge requires a serial port name")

        try:
            baudrate = int(baud_text)
        except ValueError as exc:
            raise ValueError("SerialBridge baudrate must be an integer") from exc

        return port, baudrate

    async def _write_line(self, line: str) -> None:
        if self._writer is None:
            raise RuntimeError("SerialBridge is not connected")
        self._writer.write((line.rstrip() + "\n").encode("utf-8"))
        await self._writer.drain()

    async def _read_line(self, timeout: float = 2.0) -> str:
        if self._reader is None:
            raise RuntimeError("SerialBridge is not connected")
        raw = await asyncio.wait_for(self._reader.readline(), timeout=timeout)
        return raw.decode("utf-8", errors="replace").strip()

    async def _heartbeat(self) -> None:
        from config import settings

        interval = 1.0 / max(settings.telemetry_hz, 0.1)
        while self._running:
            await self._emit(self._state)
            await asyncio.sleep(interval)

    async def connect(self) -> None:
        try:
            import serial_asyncio
        except ImportError as exc:
            raise RuntimeError(
                "SerialBridge requires the pyserial-asyncio package."
            ) from exc

        self._serial_port, self._baudrate = self._parse_robot_uri()
        try:
            self._reader, self._writer = await serial_asyncio.open_serial_connection(
                url=self._serial_port,
                baudrate=self._baudrate,
            )
        except Exception as exc:
            raise RuntimeError(
                "Failed to open serial port %s at %s baud" % (self._serial_port, self._baudrate)
            ) from exc

        self._running = True
        self._state.status = "running"
        self._state.joints = {"led": 0.0}
        self._state.sensors = {}
        self._heartbeat_task = asyncio.create_task(self._heartbeat(), name="serial-heartbeat")
        logger.info("SerialBridge connected — port=%s baud=%s", self._serial_port, self._baudrate)

    async def disconnect(self) -> None:
        self._running = False
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None
        self._state.status = "stopped"
        logger.info("SerialBridge disconnected")

    async def read_state(self) -> RobotState:
        if not self._running:
            return self._state

        async with self._io_lock:
            try:
                await self._write_line("STATE?")
                response = await self._read_line(timeout=1.0)
            except (asyncio.TimeoutError, RuntimeError):
                return self._state

        parsed = self._parse_state_response(response)
        if parsed is not None:
            self._state.joints["led"] = parsed
        return self._state

    async def send_command(self, joint: str, value: float) -> dict:
        if joint.lower() not in {"led", "blink"}:
            return {"status": "error", "reason": "SerialBridge supports only the led joint"}

        led_value = 1.0 if float(value) else 0.0
        command = "LED ON" if led_value else "LED OFF"

        async with self._io_lock:
            await self._write_line(command)
            try:
                response = await self._read_line(timeout=2.0)
            except asyncio.TimeoutError:
                response = ""

        if response and response.upper().startswith("ERROR"):
            return {"status": "error", "joint": "led", "value": led_value, "reason": response}

        self._state.joints["led"] = led_value
        await self._emit(self._state)
        result = {"status": "ok", "joint": "led", "value": led_value}
        if response:
            result["response"] = response
        return result

    def _parse_state_response(self, response: str) -> float | None:
        normalized = response.strip().upper()
        if not normalized:
            return None
        if normalized.startswith("STATE "):
            normalized = normalized[6:].strip()
        if normalized.startswith("LED "):
            value = normalized.split(maxsplit=1)[1] if " " in normalized else ""
            if value in {"1", "ON", "HIGH", "TRUE"}:
                return 1.0
            if value in {"0", "OFF", "LOW", "FALSE"}:
                return 0.0
        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_bridge(robot_uri: str) -> BaseBridge:
    """
    Return a concrete bridge based on the URI scheme.

    Supported schemes
    -----------------
    mock                    → MockBridge
    ros://...               → ROSBridge  (stub)
    serial:///dev/...       → SerialBridge (stub)
    """
    if robot_uri == "mock":
        return MockBridge()
    if robot_uri.startswith("ros://"):
        return ROSBridge()
    if robot_uri.startswith("serial://"):
        return SerialBridge(robot_uri)
    raise ValueError("Unrecognised robot_uri scheme: %s" % robot_uri)
