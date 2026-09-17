"""
RevitClientPool - singleton TCP connection with auto-reconnect.

Avoids per-request connect/disconnect overhead (~1s ExternalEvent latency).
The pool is bound to the running event loop; when a different loop calls it
(e.g. successive ``asyncio.run`` invocations) the stale connection is dropped
and a fresh one is opened.
"""
from __future__ import annotations

import asyncio

from revit_bridge.revit.client import RevitClient
from revit_bridge.revit.settings import RevitSettings


class RevitClientPool:
    """Singleton Revit TCP client with auto-reconnect."""

    _instance: RevitClient | None = None
    _lock: asyncio.Lock | None = None
    _lock_loop: asyncio.AbstractEventLoop | None = None

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if cls._lock is None or cls._lock_loop is not loop:
            cls._lock = asyncio.Lock()
            cls._lock_loop = loop
            cls._instance = None  # a connection belongs to the loop that opened it
        return cls._lock

    @classmethod
    async def get_client(cls, host: str | None = None, port: int | None = None,
                         timeout: float | None = None, connect_timeout: float | None = None,
                         token: str | None = None,
                         settings: RevitSettings | None = None) -> RevitClient:
        async with cls._get_lock():
            if cls._instance is None or not cls._instance.connected:
                cls._instance = RevitClient(
                    host=host, port=port, timeout=timeout,
                    connect_timeout=connect_timeout, token=token,
                    settings=settings,
                )
                await cls._instance.connect()
            return cls._instance

    @classmethod
    async def disconnect(cls) -> None:
        async with cls._get_lock():
            if cls._instance:
                await cls._instance.disconnect()
                cls._instance = None

    @classmethod
    async def ping(cls, **kwargs) -> bool:
        try:
            client = await cls.get_client(**kwargs)
            return await client.ping()
        except Exception:
            return False
