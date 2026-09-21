"""The read-only probe: is the add-in there, and which document is open?

``PING_PROBE`` is the snippet ``RevitClient.ping()`` and ``revit-bridge
check`` send instead of ``say_hello`` (which opens a dialog in Revit).
``check_connection`` opens a fresh connection, runs it and describes the
outcome for the CLI, the ``revit://connection-status`` resource and the web
host's health endpoint.
"""
from __future__ import annotations

from revit_bridge.revit.settings import RevitSettings

PING_PROBE = "return document.Title;"
CHECK_PROBE = PING_PROBE


async def check_connection(settings: RevitSettings | None = None) -> dict:
    """Open a fresh connection, read the open document's title, describe the outcome.

    ``reachable`` is true as soon as the add-in answers at all; what it
    answered is reported separately: ``document`` is the title of the open
    document, ``document_error`` the add-in's message when the probe could
    not run (no document open, wrong token, ...). ``error`` holds transport
    failures only.
    """
    from revit_bridge.revit.client import RevitClient   # client imports PING_PROBE from here

    settings = settings or RevitSettings.from_env()
    status = settings.describe()
    status["document"] = None
    status["document_error"] = None
    client = RevitClient(settings=settings)
    try:
        await client.connect()
        resp = await client.send_code(CHECK_PROBE)
        status["reachable"] = bool(resp.success or resp.raw)   # raw: a reply came back
        status["error"] = None if status["reachable"] else resp.error
        if resp.success and isinstance(resp.result, str):
            status["document"] = resp.result
        elif status["reachable"]:
            status["document_error"] = resp.error or "probe returned no title"
    except Exception as exc:
        status["reachable"] = False
        status["error"] = str(exc) or type(exc).__name__
    finally:
        await client.disconnect()
    status["status"] = "connected" if status["reachable"] else "disconnected"
    return status
