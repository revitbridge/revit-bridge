"""What the add-in on the other end can do: ``get_bridge_capabilities``, three states.

``discover(client)`` asks the add-in once per connection and returns a
:class:`Capabilities` whose ``state`` is

- ``supported``   - the add-in answered ``get_bridge_capabilities`` with its
  protocol, methods, features, Revit version and runtime (``info``);
- ``unsupported`` - it answered JSON-RPC ``-32601`` (method not found): an
  add-in from before the method existed, which only knows the old channel
  (``send_code_to_revit``);
- ``unknown``     - nothing usable came back: connection refused or lost,
  wrong token, timeout, any other JSON-RPC error, a malformed reply.

``reason`` keeps the original error text; ``error_kind`` classifies an
``unknown`` (``unreachable``, ``auth``, ``timeout``, ``rpc_error``,
``bad_reply``, ...). An ``unknown`` is never read as "unsupported".

The two definite answers are cached per connection - the client object and
its ``connection_generation`` - so a reconnect asks again; ``unknown`` is not
cached. The method carries no ``confirm``, opens no transaction and needs no
open document on the add-in side.
"""
from __future__ import annotations

import asyncio
import re
import weakref
from dataclasses import dataclass, field

CAPABILITIES_METHOD = "get_bridge_capabilities"
CHECK_CODE_METHOD = "check_code_in_revit"
METHOD_NOT_FOUND = -32601
# The add-in answers from a cache on its socket thread; a reply that takes
# longer than this is not coming.
DISCOVERY_TIMEOUT = 10.0

SUPPORTED = "supported"
UNSUPPORTED = "unsupported"
UNKNOWN = "unknown"


def _cjk(*code_points: int) -> str:
    """Chinese text from code points, so this source file stays ASCII."""
    return "".join(map(chr, code_points))


# The add-in's messages come in English or Chinese (its own texts, and .NET
# exception messages on a Chinese runtime).
_AUTH_RE = re.compile(r"unauthori[sz]ed|invalid or missing token", re.I)
_TIMEOUT_RE = re.compile(r"\btime(?:d)? ?out|" + _cjk(0x8D85, 0x65F6), re.I)         # "timed out"
_NO_DOCUMENT_RE = re.compile(
    r"no (?:active |open )?(?:ui)?document|ActiveUIDocument|NullReferenceException"
    r"|Object reference not set|"
    + _cjk(0x672A, 0x5C06, 0x5BF9, 0x8C61, 0x5F15, 0x7528, 0x8BBE, 0x7F6E),            # "object reference not set"
    re.I)
_READ_ONLY_RE = re.compile(
    r"read[- ]?only|ModificationForbidden|new transaction is not permitted"
    r"|cannot start a new transaction|" + _cjk(0x53EA, 0x8BFB),                        # "read-only"
    re.I)


def error_kind(message: str | None) -> str | None:
    """Classify an add-in error message: ``auth``, ``timeout``, ``no_document``, ``read_only`` or None."""
    text = message or ""
    for kind, pattern in (("auth", _AUTH_RE), ("timeout", _TIMEOUT_RE),
                          ("no_document", _NO_DOCUMENT_RE), ("read_only", _READ_ONLY_RE)):
        if pattern.search(text):
            return kind
    return None


@dataclass(frozen=True)
class Capabilities:
    """The outcome of one ``get_bridge_capabilities`` call (see the module docstring)."""

    state: str
    reason: str | None = None
    error_kind: str | None = None
    error_code: int | None = None
    info: dict = field(default_factory=dict)     # the add-in's reply as is (supported only)

    @classmethod
    def from_exception(cls, exc: BaseException) -> Capabilities:
        """``unknown`` for a client that raised: transport, timeout or a bad setting."""
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            kind = "timeout"
        elif isinstance(exc, OSError):
            kind = "unreachable"
        elif isinstance(exc, ValueError):
            kind = "settings"                    # e.g. REVIT_BRIDGE_PORT=abc
        else:
            kind = "client_error"
        return cls(UNKNOWN, reason=str(exc) or type(exc).__name__, error_kind=kind)

    @property
    def supported(self) -> bool:
        return self.state == SUPPORTED

    @property
    def methods(self) -> list[str]:
        methods = self.info.get("methods")
        return [m for m in methods if isinstance(m, str)] if isinstance(methods, list) else []

    @property
    def features(self) -> dict:
        features = self.info.get("features")
        return dict(features) if isinstance(features, dict) else {}

    @property
    def revit(self) -> dict:
        revit = self.info.get("revit")
        return dict(revit) if isinstance(revit, dict) else {}

    @property
    def runtime(self) -> dict:
        runtime = self.info.get("runtime")
        return dict(runtime) if isinstance(runtime, dict) else {}

    @property
    def kit(self):
        return self.info.get("kit")

    def has_method(self, name: str) -> bool:
        return name in self.methods

    @property
    def can_check_code(self) -> bool:
        """The add-in compiles without running: ``features.check_code``, else the method list."""
        if not self.supported:
            return False
        features = self.features
        if "check_code" in features:
            return features["check_code"] is True
        return self.has_method(CHECK_CODE_METHOD)

    def as_dict(self) -> dict:
        """The JSON shape the tools and ``revit-bridge check`` print."""
        return {
            "state": self.state,
            "reason": self.reason,
            "error_kind": self.error_kind,
            "error_code": self.error_code,
            "protocol": self.info.get("protocol"),
            "addin_version": self.info.get("addin_version"),
            "methods": self.methods,
            "features": self.features,
        }


def from_response(resp) -> Capabilities:
    """Read a ``RevitResponse`` to ``get_bridge_capabilities`` as one of the three states."""
    if getattr(resp, "success", False):
        info = resp.result
        if isinstance(info, dict) and ("protocol" in info or "methods" in info):
            return Capabilities(SUPPORTED, info=dict(info))
        return Capabilities(UNKNOWN, reason=f"unexpected reply to {CAPABILITIES_METHOD}: {str(info)[:200]}",
                            error_kind="bad_reply")
    code = getattr(resp, "error_code", None)
    message = getattr(resp, "error", None) or "no reply"
    if code == METHOD_NOT_FOUND:
        return Capabilities(UNSUPPORTED, reason=message, error_code=code)
    kind = error_kind(message) or ("rpc_error" if code is not None else "no_reply")
    return Capabilities(UNKNOWN, reason=message, error_kind=kind, error_code=code)


# -- the per-connection cache -------------------------------------------------------

_CACHE: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _marker(client):
    return getattr(client, "connection_generation", None)


def cached(client) -> Capabilities | None:
    """The definite answer for the client's current connection, or None."""
    try:
        entry = _CACHE.get(client)
    except TypeError:                            # not weak-referenceable
        return None
    if entry is None:
        return None
    marker, caps = entry
    if marker != _marker(client) or getattr(client, "connected", True) is False:
        forget(client)
        return None
    return caps


def forget(client) -> None:
    """Drop what is cached for ``client`` (the next ``discover`` asks again)."""
    try:
        _CACHE.pop(client, None)
    except TypeError:
        pass


def _remember(client, caps: Capabilities) -> None:
    try:
        _CACHE[client] = (_marker(client), caps)
    except TypeError:
        pass


async def discover(client, *, refresh: bool = False, timeout: float = DISCOVERY_TIMEOUT) -> Capabilities:
    """Ask the add-in for its capabilities; never raises for a transport or protocol failure.

    ``client`` needs ``send_command(method, params)``. ``refresh`` ignores
    the cache. ``supported`` and ``unsupported`` are cached for the
    client's current connection; ``unknown`` is asked again next time.
    """
    if not refresh:
        hit = cached(client)
        if hit is not None:
            return hit
    try:
        resp = await asyncio.wait_for(client.send_command(CAPABILITIES_METHOD, {}), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        return Capabilities(UNKNOWN, reason=f"no reply to {CAPABILITIES_METHOD} within {timeout:g}s",
                            error_kind="timeout")
    except Exception as exc:  # noqa: BLE001 - refused, reset, closed, bad settings: all "unknown"
        return Capabilities.from_exception(exc)
    caps = from_response(resp)
    if caps.state != UNKNOWN:
        _remember(client, caps)
    return caps
