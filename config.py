"""
config.py — centralised runtime settings.

All values can be overridden via environment variables prefixed ROBOT_MCP_
e.g.  ROBOT_MCP_PORT=9090  ROBOT_MCP_ROBOT_URI=mock python server.py
CLI flags in server.py take precedence over env vars.
"""

from pydantic_settings import BaseSettings
from typing import Literal


class Settings(BaseSettings):
    transport: Literal["stdio", "sse", "websocket"] = "sse"
    host: str = "0.0.0.0"
    port: int = 8080
    # "mock"            — built-in simulated robot (no hardware needed)
    # "ros://localhost" — ROS bridge (requires rospy / rclpy)
    # "serial://COM3:115200"         — serial bridge (Arduino / ESP32 POC)
    robot_uri: str = "mock"
    telemetry_hz: float = 10.0
    log_level: str = "INFO"

    model_config = {"env_prefix": "ROBOT_MCP_"}


# single shared instance — server.py mutates this before any transport starts
settings = Settings()
