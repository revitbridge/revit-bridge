"""The read-only probe: is the add-in there, and which document is open?

``PING_PROBE`` is the snippet ``RevitClient.ping()`` and ``revit-bridge
check`` send instead of ``say_hello`` (which opens a dialog in Revit).
``check_connection`` opens a fresh connection, runs it and describes the
outcome for the CLI, the ``revit://connection-status`` resource and the web
host's health endpoint; the first two also ask for the add-in's capability
and environment (``environment=True``).
"""
from __future__ import annotations

from revit_bridge.revit.settings import RevitSettings

PING_PROBE = "return document.Title;"
CHECK_PROBE = PING_PROBE


async def check_connection(settings: RevitSettings | None = None, environment: bool = False) -> dict:
    """Open a fresh connection, read the open document's title, describe the outcome.

    ``reachable`` is true as soon as the add-in answers at all; what it
    answered is reported separately: ``document`` is the title of the open
    document, ``document_error`` the add-in's message when the probe could
    not run (no document open, wrong token, ...). ``error`` holds transport
    failures only.

    With ``environment`` the reply also carries ``capability`` (the
    ``get_bridge_capabilities`` state: supported / unsupported / unknown,
    with its reason) and ``environment`` (``get_environment``: Revit version,
    runtime, Kit, or the error that kept them unknown). An add-in that did
    not answer the title probe is not asked again: its capability is
    ``unknown`` with the probe's error as the reason.
    """
    from revit_bridge.revit.capabilities import Capabilities, from_response
    from revit_bridge.revit.client import RevitClient   # client imports PING_PROBE from here
    from revit_bridge.revit.environment import get_environment, unknown_environment

    settings = settings or RevitSettings.from_env()
    status = settings.describe()
    status["document"] = None
    status["document_error"] = None
    env: dict | None = None
    unreached: Capabilities | None = None
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
        if environment:
            if status["reachable"]:
                env = await get_environment(client)
            else:
                unreached = from_response(resp)
    except Exception as exc:
        status["reachable"] = False
        status["error"] = str(exc) or type(exc).__name__
        unreached = Capabilities.from_exception(exc)
    finally:
        await client.disconnect()
    status["status"] = "connected" if status["reachable"] else "disconnected"
    if environment:
        if env is None:
            env = unknown_environment(unreached or Capabilities("unknown", reason=status["error"],
                                                                error_kind="unreachable"))
        status["capability"] = env["capability"]
        status["environment"] = env
    return status
