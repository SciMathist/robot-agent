"""
core/tools.py — MCP tool handler functions.

These are plain async functions.  They know nothing about transports.
Each transport layer wraps them in its own registration mechanism.

Tool surface
------------
read_state()              → joints + sensors + status
send_command(joint, val)  → actuate a single joint
get_telemetry()           → latest sensor snapshot (alias for quick reads)
"""

from __future__ import annotations

from core.bridge import BaseBridge


async def read_state(bridge: BaseBridge) -> dict:
    """
    Return the full current robot state.

    Response keys
    -------------
    joints   : {name: float}  — current joint positions (degrees or radians)
    sensors  : {name: float}  — latest sensor readings
    status   : str            — robot status string
    """
    state = await bridge.read_state()
    return {
        "joints":  state.joints,
        "sensors": state.sensors,
        "status":  state.status,
    }


async def send_command(bridge: BaseBridge, joint: str, value: float) -> dict:
    """
    Send a positional command to a named joint.

    Parameters
    ----------
    joint : str   — joint name (must exist in robot state)
    value : float — target position (degrees or radians, robot-dependent)

    Response keys
    -------------
    status : "ok" | "error"
    joint  : echoed joint name
    value  : echoed target value
    reason : error message (only present when status == "error")
    """
    if not joint:
        return {"status": "error", "reason": "joint name is required"}
    return await bridge.send_command(joint, float(value))


async def get_telemetry(bridge: BaseBridge) -> dict:
    """
    Return the latest sensor snapshot without joint positions.
    Useful for quick health checks or high-frequency polling.

    Response keys
    -------------
    sensors : {name: float}
    status  : str
    """
    state = await bridge.read_state()
    return {
        "sensors": state.sensors,
        "status":  state.status,
    }
