"""
Slot token helpers - pure functions shared by the web relay and its tests.

.. deprecated:: 0.3
   Slots are gone: a remote connection is a *device* now, paired with a
   pairing code and authenticated with its own token
   (:mod:`revit_bridge.auth.devices`). These functions are kept for one
   version so a host can migrate, and are removed in 0.4.

A "slot" is one remote Revit connection on the web host. Each slot may be
protected by a pre-shared token. The functions here only read configuration
and compare secrets; they never touch sockets or frameworks, so the web host
calls them from its HTTP dependency and its WebSocket handshake alike.

Environment variables (per slot ``N`` = 1..max_slots):

    MCP_BRIDGE_REQUIRE_SLOT_TOKEN   "1"/"true"/"yes": tokens are mandatory
    MCP_BRIDGE_SLOT_TOKEN_N         token value (local tests)
    MCP_BRIDGE_SLOT_TOKEN_FILE_N    path of a secret file holding the token
                                    (Docker secrets in production)

Resolution order per slot: direct value, then secret file, then whatever the
caller passed in ``configured`` (e.g. tokens from a host config file).
"""
from __future__ import annotations

import hmac
import json
import os
from collections.abc import Mapping

ENV_REQUIRE_SLOT_TOKEN = "MCP_BRIDGE_REQUIRE_SLOT_TOKEN"
ENV_SLOT_TOKEN_PREFIX = "MCP_BRIDGE_SLOT_TOKEN_"
ENV_SLOT_TOKEN_FILE_PREFIX = "MCP_BRIDGE_SLOT_TOKEN_FILE_"

DEFAULT_MAX_SLOTS = 5

_TRUE_VALUES = {"1", "true", "yes"}


def slot_token_required(env: Mapping[str, str] | None = None) -> bool:
    """True when the deployment demands a token for every slot.

    Deprecated (0.3), removed in 0.4: see the module docstring.
    """
    env = os.environ if env is None else env
    return env.get(ENV_REQUIRE_SLOT_TOKEN, "").strip().lower() in _TRUE_VALUES


def load_slot_tokens(
    env: Mapping[str, str] | None = None,
    max_slots: int = DEFAULT_MAX_SLOTS,
    configured: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Collect ``{slot_id: token}`` without requiring secrets in git.

    Raises ``RuntimeError`` when a referenced secret file is unreadable or
    empty, or when tokens are required but none are configured.

    Deprecated (0.3), removed in 0.4: ``DeviceStore.redeem`` issues a token
    per device instead of a pre-shared one per slot.
    """
    env = os.environ if env is None else env
    tokens: dict[str, str] = {
        str(slot_id): str(token)
        for slot_id, token in (configured or {}).items()
        if str(token).strip()
    }

    for slot_id in range(1, int(max_slots) + 1):
        sid = str(slot_id)
        direct = env.get(f"{ENV_SLOT_TOKEN_PREFIX}{sid}", "").strip()
        token_file = env.get(f"{ENV_SLOT_TOKEN_FILE_PREFIX}{sid}", "").strip()

        if direct:
            tokens[sid] = direct
            continue
        if token_file:
            try:
                with open(token_file, encoding="utf-8") as handle:
                    secret = handle.read().strip()
            except OSError as exc:
                raise RuntimeError(
                    f"Cannot read slot token file for slot {sid}: {token_file}"
                ) from exc
            if not secret:
                raise RuntimeError(f"Slot token file for slot {sid} is empty")
            tokens[sid] = secret

    if not tokens and slot_token_required(env):
        raise RuntimeError("Slot token is required but not configured")

    return tokens


def verify_slot_token(tokens: Mapping[str, str], slot_id: str | int | None, provided: str | None) -> bool:
    """Constant-time check that ``provided`` is the token of ``slot_id``.

    Returns False for unknown slots, missing tokens and non-string input.

    Deprecated (0.3), removed in 0.4: use ``DeviceStore.verify_device``.
    """
    if slot_id is None or not isinstance(provided, str):
        return False
    expected = tokens.get(str(slot_id))
    if not expected:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


def parse_handshake_token(first_message: str | bytes | None) -> str | None:
    """Extract the token from the add-in's first WebSocket message.

    The add-in opens the relay connection by sending a JSON object such as
    ``{"type": "auth", "slot_id": "1", "token": "..."}``. Anything that is
    not a JSON object with a string ``token`` yields ``None``.

    Deprecated (0.3), removed in 0.4: the handshake carries ``device_id``
    now, and the token is checked with ``DeviceStore.verify_device``.
    """
    if first_message is None:
        return None
    try:
        payload = json.loads(first_message)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    token = payload.get("token")
    return token if isinstance(token, str) and token else None
