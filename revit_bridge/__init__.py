"""revit-bridge: intent bridge between designers and AI for Revit.

The package talks to the Revit add-in over a local TCP socket (JSON-RPC 2.0)
and exposes that connection to any MCP client. It contains no LLM SDK, no
vector store and no web framework; hosts (Claude Desktop, Claude Code, the
demo web app) bring their own model.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from revit_bridge.paths import data_root, skills_dir

try:
    __version__ = version("revit-bridge")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0"

__all__ = ["__version__", "data_root", "skills_dir"]
