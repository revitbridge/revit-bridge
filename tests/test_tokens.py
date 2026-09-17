"""Slot token helpers (rewritten from the former bridge transport tests)."""
from __future__ import annotations

import pytest

from revit_bridge.auth.tokens import (
    load_slot_tokens,
    parse_handshake_token,
    slot_token_required,
    verify_slot_token,
)

TOKEN = "bridge-transport-test-token"


def test_required_flag_parsing():
    assert slot_token_required({"MCP_BRIDGE_REQUIRE_SLOT_TOKEN": "1"})
    assert slot_token_required({"MCP_BRIDGE_REQUIRE_SLOT_TOKEN": "TRUE"})
    assert not slot_token_required({"MCP_BRIDGE_REQUIRE_SLOT_TOKEN": "0"})
    assert not slot_token_required({})


def test_direct_env_token_is_loaded():
    tokens = load_slot_tokens({"MCP_BRIDGE_SLOT_TOKEN_1": TOKEN, "MCP_BRIDGE_REQUIRE_SLOT_TOKEN": "1"})
    assert tokens == {"1": TOKEN}


def test_secret_file_token_is_loaded(tmp_path):
    secret = tmp_path / "slot2.token"
    secret.write_text(f"{TOKEN}\n", encoding="utf-8")
    tokens = load_slot_tokens({"MCP_BRIDGE_SLOT_TOKEN_FILE_2": str(secret)})
    assert tokens == {"2": TOKEN}


def test_direct_value_wins_over_file_and_configured(tmp_path):
    secret = tmp_path / "slot1.token"
    secret.write_text("from-file", encoding="utf-8")
    tokens = load_slot_tokens(
        {"MCP_BRIDGE_SLOT_TOKEN_1": "direct", "MCP_BRIDGE_SLOT_TOKEN_FILE_1": str(secret)},
        configured={"1": "configured", "3": "cfg3"},
    )
    assert tokens == {"1": "direct", "3": "cfg3"}


def test_missing_or_empty_secret_file_raises(tmp_path):
    with pytest.raises(RuntimeError):
        load_slot_tokens({"MCP_BRIDGE_SLOT_TOKEN_FILE_1": str(tmp_path / "nope")})
    empty = tmp_path / "empty"
    empty.write_text("   ", encoding="utf-8")
    with pytest.raises(RuntimeError):
        load_slot_tokens({"MCP_BRIDGE_SLOT_TOKEN_FILE_1": str(empty)})


def test_required_without_tokens_raises_but_optional_is_empty():
    with pytest.raises(RuntimeError):
        load_slot_tokens({"MCP_BRIDGE_REQUIRE_SLOT_TOKEN": "1"})
    assert load_slot_tokens({}) == {}


def test_verify_matches_only_the_right_slot():
    tokens = {"1": TOKEN}
    assert verify_slot_token(tokens, "1", TOKEN)
    assert verify_slot_token(tokens, 1, TOKEN)
    assert not verify_slot_token(tokens, "1", TOKEN + "x")
    assert not verify_slot_token(tokens, "2", TOKEN)
    assert not verify_slot_token(tokens, None, TOKEN)
    assert not verify_slot_token(tokens, "1", None)
    assert not verify_slot_token({}, "1", TOKEN)


def test_handshake_parsing():
    assert parse_handshake_token('{"type": "auth", "slot_id": "1", "token": "abc"}') == "abc"
    assert parse_handshake_token(b'{"token": "abc"}') == "abc"
    assert parse_handshake_token('{"token": ""}') is None
    assert parse_handshake_token('{"token": 5}') is None
    assert parse_handshake_token("[1, 2]") is None
    assert parse_handshake_token("not json") is None
    assert parse_handshake_token(None) is None
