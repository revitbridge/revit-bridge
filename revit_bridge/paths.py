"""Where revit-bridge keeps user data and where packaged resources live.

User data (solidified packs, usage statistics, the evidence ledger) never goes
into the package directory or a source checkout: it lives under a per-user
data root that every host shares.

    data_root()                 %LOCALAPPDATA%/revit-bridge on Windows,
                                $XDG_DATA_HOME/revit-bridge or
                                ~/.local/share/revit-bridge elsewhere
                                (REVIT_BRIDGE_DATA_DIR overrides)
    user_capabilities_dir()     <data_root>/capabilities
                                (REVIT_BRIDGE_CAPABILITIES_DIR overrides)
    evidence_dir()              <data_root>/evidence
                                (REVIT_BRIDGE_EVIDENCE_DIR overrides)
    auth_dir()                  <data_root>/auth (pairing codes, device tokens)
    builtin_capabilities_dir()  packs shipped in the wheel, or <repo>/capabilities
    skills_dir()                skills shipped in the wheel, or <repo>/plugin/skills

Nothing here creates a directory; the code that writes creates what it needs.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

ENV_DATA_DIR = "REVIT_BRIDGE_DATA_DIR"
ENV_CAPABILITIES_DIR = "REVIT_BRIDGE_CAPABILITIES_DIR"
ENV_EVIDENCE_DIR = "REVIT_BRIDGE_EVIDENCE_DIR"

APP_DIR_NAME = "revit-bridge"

_PACKAGE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PACKAGE_DIR.parent
_IS_WINDOWS = os.name == "nt"


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _override(env: Mapping[str, str], name: str) -> Path | None:
    value = env.get(name, "").strip()
    return Path(value).expanduser() if value else None


def data_root(env: Mapping[str, str] | None = None) -> Path:
    """Per-user data directory shared by every host on this machine."""
    env = _env(env)
    override = _override(env, ENV_DATA_DIR)
    if override is not None:
        return override
    if _IS_WINDOWS:
        base = env.get("LOCALAPPDATA", "").strip()
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    else:
        base = env.get("XDG_DATA_HOME", "").strip()
        root = Path(base) if base else Path.home() / ".local" / "share"
    return root / APP_DIR_NAME


def user_capabilities_dir(env: Mapping[str, str] | None = None) -> Path:
    """Where solidified packs, overrides and ``usage.json`` are written."""
    env = _env(env)
    return _override(env, ENV_CAPABILITIES_DIR) or data_root(env) / "capabilities"


def evidence_dir(env: Mapping[str, str] | None = None) -> Path:
    """Where the execution ledger and pending confirmations are written."""
    env = _env(env)
    return _override(env, ENV_EVIDENCE_DIR) or data_root(env) / "evidence"


def auth_dir(env: Mapping[str, str] | None = None) -> Path:
    """Where ``devices.json`` (pairing codes and device tokens, hashed) is written."""
    return data_root(env) / "auth"


def builtin_capabilities_dir() -> Path:
    """Read-only packs: inside the wheel, or the repo's ``capabilities/``."""
    packaged = _PACKAGE_DIR / "capabilities" / "builtin"
    if packaged.is_dir():
        return packaged
    return _REPO_ROOT / "capabilities"


def skills_dir() -> Path:
    """The plugin skills: inside the wheel, or the repo's ``plugin/skills``."""
    packaged = _PACKAGE_DIR / "skills"
    if packaged.is_dir():
        return packaged
    return _REPO_ROOT / "plugin" / "skills"
