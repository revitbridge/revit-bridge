"""Connection settings, read from ``REVIT_BRIDGE_*`` environment variables.

The package has no config file: every host (Claude Desktop, Claude Code,
the web demo) passes settings through the environment of the MCP process.

    REVIT_BRIDGE_HOST      add-in host              (default 127.0.0.1)
    REVIT_BRIDGE_PORT      add-in TCP port          (default 18080)
    REVIT_BRIDGE_TOKEN     pre-shared token         (default: none)
    REVIT_BRIDGE_TIMEOUT   command timeout, seconds (default 60)
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

ENV_HOST = "REVIT_BRIDGE_HOST"
ENV_PORT = "REVIT_BRIDGE_PORT"
ENV_TOKEN = "REVIT_BRIDGE_TOKEN"
ENV_TIMEOUT = "REVIT_BRIDGE_TIMEOUT"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18080
DEFAULT_TIMEOUT = 60.0
DEFAULT_CONNECT_TIMEOUT = 5.0

_TRUE_VALUES = {"1", "true", "yes", "on"}


def env_flag(name: str, env: Mapping[str, str] | None = None) -> bool:
    """Return True when the environment variable holds a truthy value."""
    env = os.environ if env is None else env
    return env.get(name, "").strip().lower() in _TRUE_VALUES


@dataclass(frozen=True)
class RevitSettings:
    """Where the Revit add-in listens and how long to wait for it."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    token: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RevitSettings:
        env = os.environ if env is None else env
        host = env.get(ENV_HOST, "").strip() or DEFAULT_HOST
        port = _parse_int(env.get(ENV_PORT), DEFAULT_PORT, ENV_PORT)
        token = env.get(ENV_TOKEN, "").strip() or None
        timeout = _parse_float(env.get(ENV_TIMEOUT), DEFAULT_TIMEOUT, ENV_TIMEOUT)
        return cls(host=host, port=port, token=token, timeout=timeout)

    def describe(self) -> dict:
        """Connection facts safe to print (the token itself is never shown)."""
        return {
            "host": self.host,
            "port": self.port,
            "token": "set" if self.token else "not set",
            "timeout": self.timeout,
        }


def _parse_int(raw: str | None, default: int, name: str) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _parse_float(raw: str | None, default: float, name: str) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
