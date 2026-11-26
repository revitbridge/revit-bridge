"""Shared fixtures.

Every test runs with ``REVIT_BRIDGE_DATA_DIR`` pointing at a private temporary
directory, so nothing a test writes (solidified packs, usage.json, evidence)
lands in the developer's real data directory or the source tree, and no
personal, company or project guidance layer of the developer's leaks in.
"""
from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("REVIT_BRIDGE_DATA_DIR", str(data))
    monkeypatch.delenv("REVIT_BRIDGE_CAPABILITIES_DIR", raising=False)
    monkeypatch.delenv("REVIT_BRIDGE_EVIDENCE_DIR", raising=False)
    monkeypatch.delenv("REVIT_BRIDGE_GUIDANCE_COMPANY", raising=False)
    monkeypatch.delenv("REVIT_BRIDGE_GUIDANCE_PROJECT", raising=False)
    server = sys.modules.get("revit_bridge.mcp_server")
    if server is not None:
        from revit_bridge.capabilities.store import ToolStore
        from revit_bridge.evidence.ledger import Ledger
        from revit_bridge.spec.gate import Gate
        monkeypatch.setattr(server, "_tool_store", ToolStore())
        monkeypatch.setattr(server, "_gate", Gate())
        monkeypatch.setattr(server, "_ledger", Ledger())
    return data


# -- The 0.11.1 spelling of a value inside a string literal ---------------------------------
#
# From 0.11.2 a value is written with every character outside [A-Za-z0-9_ ] as a unicode
# escape (tests/test_value_escaping.py). The tests listed below were written against the
# 0.11.1 spelling (backslash escapes for quote, backslash and line ends, everything else as
# is) and assert on rendered text. They keep that spelling through this fixture, so they go
# on showing that nothing else in the rendering moved; the 0.11.1 fixture hashes in
# tests/test_store.py are compared the same way.

_LEGACY = {"\\": "\\\\", '"': '\\"', "\r": "\\r", "\n": "\\n", "\x00": "\\0",
           "\x85": "\\u0085", " ": "\\u2028", " ": "\\u2029"}


def legacy_escape_param_value(value) -> str:
    if isinstance(value, str):
        return "".join(_LEGACY.get(c) or (f"\\u{ord(c):04x}" if 0xD800 <= ord(c) <= 0xDFFF else c) for c in value)
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return int.__repr__(value)
    if isinstance(value, float):
        return float.__repr__(value)
    raise TypeError(f"must be a single value, got a value of type {type(value).__name__}")


@pytest.fixture
def legacy_string_spelling(monkeypatch):
    from revit_bridge.capabilities import store
    from revit_bridge.validators import builtin
    monkeypatch.setattr(store, "escape_param_value", legacy_escape_param_value)
    monkeypatch.setattr(builtin, "escape_param_value", legacy_escape_param_value)


_LEGACY_MODULES = {"test_basic_packs", "test_modeling_packs", "test_query", "test_validators", "test_verification"}
_LEGACY_TESTS = {
    "test_every_built_in_fixture_matches_0_11_1_bytes",
    "test_every_character_that_ends_a_csharp_literal_or_line_is_escaped",
    "test_quoted_booleans_and_string_parameters_keep_scalar_spelling",
    "test_negative_bare_value_after_minus_preserves_the_subtraction",
    "test_numeric_default_depends_on_site_at_save_listing_and_render",
    "test_scalar_spelling_in_both_permitted_contexts",
}


def pytest_collection_modifyitems(items):
    for item in items:
        if item.module.__name__.rpartition(".")[2] in _LEGACY_MODULES or item.originalname in _LEGACY_TESTS:
            item.fixturenames.append("legacy_string_spelling")
