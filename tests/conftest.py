"""Shared fixtures.

Every test runs with ``REVIT_BRIDGE_DATA_DIR`` pointing at a private temporary
directory, so nothing a test writes (solidified packs, usage.json, evidence)
lands in the developer's real data directory or the source tree.
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
    server = sys.modules.get("revit_bridge.mcp_server")
    if server is not None:
        from revit_bridge.capabilities.store import ToolStore
        from revit_bridge.evidence.ledger import Ledger
        from revit_bridge.spec.gate import Gate
        monkeypatch.setattr(server, "_tool_store", ToolStore())
        monkeypatch.setattr(server, "_gate", Gate())
        monkeypatch.setattr(server, "_ledger", Ledger())
    return data
