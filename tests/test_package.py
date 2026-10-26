"""Package-level constraints: no LLM SDK / vector store / web framework."""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from revit_bridge.revit import sandbox
from revit_bridge.snapshot.query import sanitize_categories

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("openai", "anthropic", "google-genai", "google.generativeai", "cohere", "chromadb", "fastapi", "langchain")


def test_declared_dependencies_are_the_allowed_set():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    names = sorted(d.split(">")[0].split("<")[0].split("=")[0].strip() for d in data["project"]["dependencies"])
    assert names == ["mcp", "pydantic", "pyyaml", "websockets"]
    assert data["project"]["scripts"] == {"revit-bridge": "revit_bridge.mcp_server:main"}


def test_wheel_bundles_packs_skills_and_guidance():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    included = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert included == {
        "capabilities": "revit_bridge/capabilities/builtin",
        "plugin/skills": "revit_bridge/skills",
        "plugin/guidance": "revit_bridge/guidance_content",
    }
    sdist = data["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    assert "plugin/skills" in sdist and "plugin/guidance" in sdist


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


def test_the_0_1_slot_models_are_gone():
    """Phase 6.0: SlotState / QuestionItem / ActionStep / ConstraintViolation were removed."""
    import revit_bridge.spec as spec
    import revit_bridge.spec.models as models

    for name in ("SlotState", "SlotStatus", "SlotSource", "QuestionItem", "ActionStep", "ConstraintViolation"):
        assert not hasattr(models, name), name
        assert name not in spec.__all__, name
    assert {"TaskSpec", "ParamBinding", "Source", "Interpretation", "Action", "WorkflowState"} <= set(spec.__all__)


def test_the_plugin_starts_only_revit_bridge_and_the_readme_shows_the_pair():
    """S5: revit-api-docs is opt-in (its first run downloads 335 MB); the README shows both servers."""
    import json
    import re

    plugin = json.loads((ROOT / "plugin" / ".mcp.json").read_text(encoding="utf-8"))
    assert plugin == {"mcpServers": {"revit-bridge": {"command": "uvx", "args": ["revit-bridge"]}}}
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme[readme.index("## Pair with revit-api-docs"):readme.index("## Configure")]
    assert len(section.strip().splitlines()) <= 15
    pair = json.loads(re.search(r"```json\n(.*?)```", section, re.S).group(1))
    assert pair["mcpServers"] == {"revit-bridge": {"command": "uvx", "args": ["revit-bridge"]},
                                  "revit-api-docs": {"command": "uvx", "args": ["revit-api-docs"]}}
    assert "uvx revit-api-docs download" in section and "TYPESAFE_API_KEY" in section
