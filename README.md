# robot-agent

![Status](https://img.shields.io/badge/status-under--development-orange)

An MCP server (`robot-mcp`) that bridges a physical robot (or actuator system) to a virtual
agent such as Claude.  Supports three transports: **stdio**, **SSE**, and
**WebSocket**, all sharing the same tool/resource logic. The project will also have a second module called `embot` which is a AVLA agent that uses the server to control a robot.

`robot-agent = robot-mcp + embot`

---

## Project layout 

```
robot_mcp/
├── core/
│   ├── bridge.py       # hardware abstraction (MockBridge, ROSBridge stub, …)
│   ├── tools.py        # MCP tool handlers  (read_state, send_command, get_telemetry)
│   └── resources.py    # telemetry buffer + MCP resource handlers
├── transports/
│   ├── base.py         # abstract BaseTransport
│   ├── stdio.py        # stdio (subprocess)
│   ├── sse.py          # HTTP + Server-Sent Events
│   └── websocket.py    # full-duplex JSON-RPC 2.0 over WebSocket (aiohttp)
├── server.py           # entrypoint — wires everything together
├── config.py           # Pydantic settings
└── requirements.txt
```

---

## Quick start

```bash
pip install -r requirements.txt

# Simulated robot, SSE transport (default)
python server.py

# stdio (for Claude Desktop / MCP CLI)
python server.py --transport stdio

# WebSocket, high-frequency telemetry
python server.py --transport websocket --port 8081 --telemetry-hz 50

# Arduino / ESP32 serial POC on Windows
python server.py --transport stdio --robot-uri serial://COM3:115200
```

### Serial POC protocol

The current serial bridge expects a simple newline-delimited text protocol:

* Send `LED ON` to turn the LED on.
* Send `LED OFF` to turn the LED off.
* Optionally reply with `OK LED ON` or `OK LED OFF`.
* Optionally support `STATE?` and respond with `STATE LED ON` or `STATE LED OFF`.

For the first proof of concept, the bridge caches the last LED state locally and emits it through the MCP state and telemetry paths. That keeps the server usable even before you add a richer firmware response format.

---

## Transport reference

| Transport | Endpoint | Claude connects via | Push support | Best for |
|-----------|----------|---------------------|--------------|----------|
| stdio | stdin / stdout | subprocess | ✗ poll only | local, single client |
| sse | `GET /sse`, `POST /messages` | HTTP | ✓ SSE stream | networked, ≤20 Hz |
| websocket | `ws://host/ws` | WebSocket | ✓ WS frames | high-frequency, low-latency |

---

## Available MCP tools

| Tool | Description |
|------|-------------|
| `robot_read_state` | Read joint positions + sensor values + status |
| `robot_send_command(joint, value)` | Send a positional command to a named joint |
| `robot_get_telemetry` | Latest sensor snapshot (no joints) |

## Available MCP resources

| URI | Description |
|-----|-------------|
| `robot://telemetry` | Sensors + status (JSON) |
| `robot://state` | Full state including joints (JSON) |

---

## Adding a real robot

Subclass `BaseBridge` in `core/bridge.py`:

```python
class MyRobotBridge(BaseBridge):
    async def connect(self):    ...
    async def disconnect(self): ...
    async def read_state(self): ...
    async def send_command(self, joint, value): ...
```

Register it in `create_bridge()` with a new URI scheme, then run:

```bash
python server.py --robot-uri myrobot://192.168.1.10
```

---

## Environment variables

All settings can be overridden via `ROBOT_MCP_*` env vars:

```bash
ROBOT_MCP_PORT=9090 ROBOT_MCP_TELEMETRY_HZ=25 python server.py
```

See `config.py` for the full list.
