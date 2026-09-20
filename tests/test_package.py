"""Package-level constraints: no LLM SDK / vector store / web framework."""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from revit_bridge.revit import sandbox
from revit_bridge.snapshot.query import sanitize_categories
from revit_bridge.spec.models import QuestionItem, SlotSource, SlotState, SlotStatus

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("openai", "anthropic", "google-genai", "google.generativeai", "cohere", "chromadb", "fastapi", "langchain")


def test_declared_dependencies_are_the_allowed_set():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    names = sorted(d.split(">")[0].split("<")[0].split("=")[0].strip() for d in data["project"]["dependencies"])
    assert names == ["mcp", "pydantic", "pyyaml", "websockets"]
    assert data["project"]["scripts"] == {"revit-bridge": "revit_bridge.mcp_server:main"}


def test_wheel_bundles_packs_and_skills():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    included = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert included == {
        "capabilities": "revit_bridge/capabilities/builtin",
        "plugin/skills": "revit_bridge/skills",
    }
    assert "plugin/skills" in data["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]


def test_importing_the_server_pulls_no_forbidden_modules():
    import revit_bridge.mcp_server  # noqa: F401

    loaded = {name.split(".")[0] for name in sys.modules}
    assert not loaded.intersection({"openai", "anthropic", "cohere", "chromadb", "fastapi", "langchain"})


def test_source_tree_never_mentions_forbidden_imports_or_old_repo():
    for path in (ROOT / "revit_bridge").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "sys.path.insert" not in text, path
        assert "mcp_bridge" not in text and "intent_bridge" not in text, path
        for name in FORBIDDEN:
            assert f"import {name}" not in text and f"from {name}" not in text, (path, name)


def test_sandbox_blocks_known_patterns():
    safe, warnings = sandbox.review("var uidoc = new UIDocument(document); return 1;")
    assert safe and warnings == []
    safe, warnings = sandbox.review("System.Diagnostics.Process.Start(\"cmd\");")
    assert not safe and warnings


def test_category_sanitizer():
    assert sanitize_categories(["OST_Walls", "OST_NotARealCategory", "OST_Walls"]) == ["OST_Walls"]
    assert sanitize_categories("OST_Walls") == []


def test_slot_state_transitions():
    slot = SlotState(name="height")
    assert slot.status is SlotStatus.empty and slot.source is SlotSource.not_provided
    slot.set_default(3000)
    assert slot.status is SlotStatus.defaulted and slot.display == "3000 (default)"
    slot.fill(3600, SlotSource.follow_up)
    assert slot.status is SlotStatus.filled and slot.source is SlotSource.follow_up
    question = QuestionItem(slot="level", text="Which level?")
    assert question.options == [] and question.allow_custom is False
