"""Data directories and packaged resources: revit_bridge.paths."""
from __future__ import annotations

from pathlib import Path

import revit_bridge
from revit_bridge import paths

ROOT = Path(__file__).resolve().parents[1]


def test_data_root_platform_defaults(monkeypatch):
    monkeypatch.setattr(paths, "_IS_WINDOWS", True)
    assert paths.data_root({"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}) \
        == Path(r"C:\Users\me\AppData\Local") / "revit-bridge"
    assert paths.data_root({}) == Path.home() / "AppData" / "Local" / "revit-bridge"

    monkeypatch.setattr(paths, "_IS_WINDOWS", False)
    assert paths.data_root({"XDG_DATA_HOME": "/srv/data"}) == Path("/srv/data/revit-bridge")
    assert paths.data_root({}) == Path.home() / ".local" / "share" / "revit-bridge"


def test_environment_overrides():
    env = {"REVIT_BRIDGE_DATA_DIR": "/tmp/rb"}
    assert paths.data_root(env) == Path("/tmp/rb")
    assert paths.user_capabilities_dir(env) == Path("/tmp/rb/capabilities")
    assert paths.evidence_dir(env) == Path("/tmp/rb/evidence")

    env["REVIT_BRIDGE_CAPABILITIES_DIR"] = "/caps"
    env["REVIT_BRIDGE_EVIDENCE_DIR"] = "/ev"
    assert paths.user_capabilities_dir(env) == Path("/caps")
    assert paths.evidence_dir(env) == Path("/ev")


def test_conftest_isolates_the_process_environment(isolated_data_dir):
    assert paths.data_root() == isolated_data_dir
    assert revit_bridge.data_root() == isolated_data_dir


def test_packaged_resources_resolve_in_the_source_tree():
    # No wheel here: the repo directories are the packaged locations.
    assert paths.builtin_capabilities_dir() == ROOT / "capabilities"
    assert paths.skills_dir() == ROOT / "plugin" / "skills"
    assert revit_bridge.skills_dir() is not None
    assert (paths.skills_dir() / "revit-bridge" / "SKILL.md").is_file()
    assert len(list(paths.builtin_capabilities_dir().glob("*.yaml"))) == 11
