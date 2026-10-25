"""Which Revit and which .NET runtime the add-in runs in: ``get_environment(client)``.

The facts come from the add-in's ``get_bridge_capabilities`` reply when it has
one (capability ``supported``): no document, no transaction. An older add-in
(``unsupported``) is asked through the old channel instead - ``ENV_PROBE``
goes through ``send_code_to_revit``, so it needs an open document and runs
inside a transaction there. When the capability is ``unknown`` nothing more
is asked: whatever stopped the discovery would stop the probe too.

The reply always has the same keys::

    {"source": "capabilities" | "probe" | None,
     "revit": {"version_number", "version_build", "version_name"},
     "runtime": {"framework", "clr"},
     "kit": None,
     "capability": Capabilities.as_dict(),
     "error": None | "<kind>: <message>"}

``error`` says why facts are missing - ``no_document``, ``read_only``,
``auth``, ``timeout``, ``unreachable``, ``bad_reply``, ``probe_failed``, ... -
and is never reported as "no Kit": ``kit`` is what the add-in said (always
None for now), and None next to an ``error`` means "not known".
"""
from __future__ import annotations

from revit_bridge.revit.capabilities import SUPPORTED, UNSUPPORTED, Capabilities, discover, error_kind

# The old-channel probe (D7): the same facts as get_bridge_capabilities, read
# through send_code_to_revit. Environment.Version is the CLR version; the
# framework description is not reachable from the snippet's references.
ENV_PROBE = (
    "var app = document.Application;\n"
    "return new { version_number = app.VersionNumber, version_build = app.VersionBuild, "
    "version_name = app.VersionName, clr = Environment.Version.ToString() };"
)

REVIT_KEYS = ("version_number", "version_build", "version_name")
RUNTIME_KEYS = ("framework", "clr")


def _text(value) -> str | None:
    return None if value is None else str(value)


def empty_environment(capabilities: Capabilities, error: str | None = None) -> dict:
    """The reply shape with nothing known yet."""
    return {
        "source": None,
        "revit": {k: None for k in REVIT_KEYS},
        "runtime": {k: None for k in RUNTIME_KEYS},
        "kit": None,
        "capability": capabilities.as_dict(),
        "error": error,
    }


def unknown_environment(capabilities: Capabilities) -> dict:
    """The reply for a capability that could not be discovered: its reason as the error."""
    return empty_environment(capabilities, f"{capabilities.error_kind or 'unknown'}: {capabilities.reason}")


def version_label(environment: dict | None) -> str | None:
    """"Revit 2026.5" from ``version_name`` ("Autodesk Revit 2026.5"), else "Revit <number>", else None."""
    revit = (environment or {}).get("revit") or {}
    name = revit.get("version_name")
    if isinstance(name, str) and name.strip():
        name = name.strip()
        return name[len("Autodesk "):] if name.startswith("Autodesk ") else name
    number = revit.get("version_number")
    return f"Revit {number}" if number else None


async def get_environment(client, capabilities: Capabilities | None = None) -> dict:
    """The add-in's Revit version, runtime and Kit (see the module docstring); never raises
    for a transport or add-in failure.

    ``capabilities`` skips the discovery when the caller already has it.
    ``client`` needs ``send_command(method, params)`` and, for the probe,
    ``send_code(code)``.
    """
    caps = capabilities if capabilities is not None else await discover(client)
    if caps.state == SUPPORTED:
        env = empty_environment(caps)
        env["source"] = "capabilities"
        env["revit"].update({k: _text(caps.revit.get(k)) for k in REVIT_KEYS})
        env["runtime"].update({k: _text(caps.runtime.get(k)) for k in RUNTIME_KEYS})
        env["kit"] = caps.kit
        if not caps.revit:
            env["error"] = "bad_reply: get_bridge_capabilities carried no revit block"
        return env
    if caps.state != UNSUPPORTED:
        return unknown_environment(caps)

    env = empty_environment(caps)
    env["source"] = "probe"
    try:
        resp = await client.send_code(ENV_PROBE)
    except Exception as exc:  # noqa: BLE001 - the probe's transport failed: say which way
        kind = Capabilities.from_exception(exc).error_kind
        env["error"] = f"{kind}: {str(exc) or type(exc).__name__}"
        return env
    result = resp.result
    if resp.success and isinstance(result, dict) and any(k in result for k in (*REVIT_KEYS, "clr")):
        env["revit"] = {k: _text(result.get(k)) for k in REVIT_KEYS}
        env["runtime"]["clr"] = _text(result.get("clr"))
        return env
    if resp.success:
        env["error"] = f"probe_failed: unexpected reply {str(result)[:200]}"
    else:
        message = resp.error or "no result"
        env["error"] = f"{error_kind(message) or 'probe_failed'}: {message}"
    return env
