"""
Tool Store - solidify successful code executions into reusable named tools.

Flow:
    1. The host model writes C# code
    2. Code executes successfully in Revit
    3. User calls solidify() -> saves as a YAML capability pack
    4. Next time: load tool by name -> fill parameters -> execute directly

Storage: one YAML file per pack, in two directories.

    built-in   ``builtin_capabilities_dir()`` - read-only, shipped in the wheel
               (``revit_bridge/capabilities/builtin/``) or ``<repo>/capabilities/``
    user       ``user_capabilities_dir()`` - ``<data_root>/capabilities``;
               ``REVIT_BRIDGE_CAPABILITIES_DIR`` overrides this directory only

``list_tools`` / ``load`` see the union; a user pack with the same name as a
built-in one replaces it. ``solidify`` / ``update`` / ``delete`` write only
the user directory: updating a built-in pack copies it there first, deleting
one leaves an empty ``<name>.disabled`` marker that hides it. Only top-level
``*.yaml`` files are read; names starting with ``_``, subdirectories and
``.disabled`` markers are skipped.

Execution counters live in ``<user dir>/usage.json`` (see ``usage.py``), not
in the pack files. Pack files without ``schema_version`` are read as the v0
layout and normalised on load (see ``normalize_pack``); anything written is
the v1 layout.
"""
from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path

import yaml

from revit_bridge.paths import (
    ENV_CAPABILITIES_DIR,
    builtin_capabilities_dir,
    user_capabilities_dir,
)
from revit_bridge.capabilities.schema import PACK_SCHEMA_VERSION, optional_element_pick, validate_pack
from revit_bridge.capabilities.scalars import BARE_TYPES, NUMERIC_TYPES, bare_literal
from revit_bridge.capabilities.template import CODE, PLACEHOLDER_RE, template_slots
from revit_bridge.capabilities.usage import USAGE_FILE, UsageStore, empty_usage

__all__ = [
    "ENV_CAPABILITIES_DIR",
    "DISABLED_SUFFIX",
    "PACK_SCHEMA_VERSION",
    "SolidifiedTool",
    "ToolStore",
    "default_capabilities_dir",
    "escape_param_value",
    "normalize_pack",
]

_log = logging.getLogger("revit_bridge.capabilities.store")

DISABLED_SUFFIX = ".disabled"

# Fields of a v1 pack file, in the order they are written. Parameter ``source``
# values are "designer", "tool:<kind>", "answer" or "default".
PACK_FIELDS = (
    "schema_version", "name", "display_name", "description", "version",
    "revit_versions", "read_only", "ui_only", "code_template", "parameters", "preconditions",
    "not_for", "applies_when", "validator", "fixtures",
    "approved_by", "approved_at", "created_at", "source_query",
)


def default_capabilities_dir() -> Path:
    """Deprecated 0.1 name for ``user_capabilities_dir()``.

    Always the writable user directory: a 0.1 caller doing
    ``ToolStore(default_capabilities_dir())`` must never end up writing into
    the read-only packs shipped in the wheel.
    """
    return user_capabilities_dir()


# What a regular C# string literal cannot hold as written: the quote, the backslash and the
# characters C# reads as a line end (CR, LF, U+0085, U+2028, U+2029). NUL and lone UTF-16
# surrogates are escaped too, so the text reaches Revit unchanged.
_CS_ESCAPES = {
    "\\": "\\\\", '"': '\\"', "\r": "\\r", "\n": "\\n", "\x00": "\\0",
    "\x85": "\\u0085", "\N{LINE SEPARATOR}": "\\u2028", "\N{PARAGRAPH SEPARATOR}": "\\u2029",
}
_CS_ESCAPE_RE = re.compile('[\\\\"\r\n\x00\x85\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}\ud800-\udfff]')
# A bare scalar can join an identifier (including an escaped/Unicode one) or
# a neighboring numeric token. Insert trivia only at a potentially shared edge.
_SCALAR_EDGE = re.compile(r"[\w.@\\]|[^\x00-\x7f]")


def _cs_escape(match: re.Match) -> str:
    char = match.group()
    return _CS_ESCAPES.get(char) or f"\\u{ord(char):04x}"


def _value_kind(value) -> str:
    """How a refused parameter value is named in an error: ``a list``, ``an object``, ``null``."""
    if value is None:
        return "null"
    if isinstance(value, (list, tuple)):
        return "a list"
    if isinstance(value, dict):
        return "an object"
    return f"a value of type {type(value).__name__}"


def escape_param_value(value) -> str:
    """Escape ordinary C# string content, preserving a scalar's 0.11.1 spelling."""
    if isinstance(value, str):
        return _CS_ESCAPE_RE.sub(_cs_escape, value)
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return int.__repr__(value)
    if isinstance(value, float):
        return float.__repr__(value)
    raise TypeError(f"must be a single value, got {_value_kind(value)}")


def _value_problem(value, ptype: str) -> str | None:
    """Check a supplied scalar without losing its spelling inside a C# string."""
    if not isinstance(value, (str, int, float)):          # bool is an int
        return f"must be a single value, got {_value_kind(value)}"
    if ptype in BARE_TYPES:
        try:
            bare_literal(value, ptype)
        except ValueError as exc:
            return str(exc)
    elif ptype != "string":
        return f"expects {ptype}, got {type(value).__name__}"
    return None


@dataclass
class SolidifiedTool:
    """A reusable tool created from a successful code execution.

    Parameter dicts carry the v1 vocabulary after loading:
      - ``source``: "designer" | "tool:<kind>" | "answer" | "default"
      - ``choices_from``: query kind for ``get_tool_choices`` (levels,
        family_types:<OST_*>, floor_types, links, elements[:<OST_*>])
      - ``unit``: "mm" | "m" | "feet" for lengths
      - ``required``: bool; ``default``: the declared default value
    """
    name: str                           # e.g. "create_curtain_wall"
    display_name: str                   # e.g. "Create Curtain Wall"
    description: str                    # what it does
    code_template: str                  # C# code with {param} placeholders
    parameters: list[dict]              # [{name, type, description, source, ...}]
    schema_version: int = 0             # 0 = read from a v0 file
    version: str = "0.0.0"              # semver of the pack itself
    revit_versions: list[str] = field(default_factory=list)
    preconditions: list[dict] = field(default_factory=list)  # [{kind, ...}] or [{text}]
    applies_when: list[str] = field(default_factory=list)    # trigger phrases
    not_for: list[str] = field(default_factory=list)         # explicitly excluded scenarios
    validator: dict | None = None       # post-execution assertion (see validators/)
    fixtures: list[dict] = field(default_factory=list)
    approved_by: str | None = None
    approved_at: str | None = None
    created_at: str = ""
    source_query: str = ""              # original user query that generated it
    tags: list[str] = field(default_factory=list)   # v0 only; read, never written
    execution_count: int = 0            # from usage.json
    last_used: str = ""                 # from usage.json
    failure_count: int = 0              # consecutive failures, from usage.json
    read_only: bool = False             # pack declaration; user overrides are still user packs
    ui_only: bool = False
    validation_errors: list[str] = field(default_factory=list)  # computed on load, never persisted


KNOWN_FIELDS = {f.name for f in fields(SolidifiedTool)}
# A host override calling super() must validate the same pack that render read.
_render_snapshot: ContextVar[tuple[ToolStore, str, SolidifiedTool] | None] = ContextVar(
    "render_snapshot", default=None)


# -- normalisation -------------------------------------------------------------

def normalize_pack(data: dict, fallback_name: str = "") -> dict:
    """Return a pack dict in the v1 shape.

    Files without ``schema_version`` are v0 and get ``version = "0.0.0"``.
    Missing parameter keys are filled the same way for both layouts: a
    ``source`` is inferred (``choices_from`` -> ``tool:<kind>``, a ``default``
    -> ``default``, else ``designer``; the 0.1 words ``query:``/``interactive:``
    -> ``tool:``, ``ask_user`` -> ``designer``), a ``tool:<query>`` source
    without ``choices_from`` gets ``choices_from = <query>``, ``unit`` is
    ``mm`` when the description says ``(mm)``, ``required`` means "no
    default". String preconditions become ``{"text": ...}``. Usage counters
    are dropped (they live in ``usage.json``); ``tags`` are kept for reading.
    """
    out = dict(data)
    out["name"] = str(out.get("name") or fallback_name)
    raw_version = out.get("schema_version")
    is_v0 = not raw_version or int(raw_version) < 1
    out["schema_version"] = 0 if is_v0 else int(raw_version)

    out.setdefault("display_name", out["name"].replace("_", " ").title())
    out.setdefault("description", "")
    out.setdefault("code_template", "")
    out["version"] = "0.0.0" if is_v0 else str(out.get("version") or "0.0.0")
    out["revit_versions"] = [str(v) for v in (out.get("revit_versions") or [])]
    out["read_only"] = out.get("read_only") is True
    out["ui_only"] = out.get("ui_only") is True
    if out["read_only"] and out["ui_only"]:
        raise ValueError("read_only and ui_only cannot both be true")
    out["parameters"] = [
        _normalize_param(p) for p in (out.get("parameters") or []) if isinstance(p, dict)
    ]
    out["preconditions"] = [
        _normalize_precondition(p) for p in (out.get("preconditions") or []) if p
    ]
    out["applies_when"] = [str(s) for s in (out.get("applies_when") or [])]
    out["not_for"] = [str(s) for s in (out.get("not_for") or [])]
    out["validator"] = out.get("validator") if isinstance(out.get("validator"), dict) else None
    out["fixtures"] = [f for f in (out.get("fixtures") or []) if isinstance(f, dict)]
    out["approved_by"] = out.get("approved_by") or None
    out["approved_at"] = out.get("approved_at") or None
    out["created_at"] = str(out.get("created_at") or "")
    out["source_query"] = str(out.get("source_query") or "")
    out["tags"] = [str(t) for t in (out.get("tags") or [])]
    for key in ("execution_count", "last_used", "failure_count"):
        out.pop(key, None)
    return out


def _normalize_param(param: dict) -> dict:
    p = dict(param)
    p["name"] = str(p.get("name", ""))
    p.setdefault("type", "string")
    source = str(p.get("source") or "")
    if source.startswith(("query:", "interactive:")):
        p["source"] = "tool:" + source.split(":", 1)[1]
    elif source == "ask_user":
        p["source"] = "designer"
    elif not source:
        if p.get("choices_from"):
            p["source"] = "tool:" + str(p["choices_from"]).split(":", 1)[0]
        elif "default" in p:
            p["source"] = "default"
        else:
            p["source"] = "designer"
    # A Revit-sourced value needs a query for get_tool_choices to run; the
    # part after "tool:" is that query when the pack did not spell it out.
    if str(p["source"]).startswith("tool:") and not p.get("choices_from"):
        p["choices_from"] = str(p["source"]).split(":", 1)[1]
    if "unit" not in p and "(mm)" in str(p.get("description", "")):
        p["unit"] = "mm"
    if "required" not in p:
        p["required"] = "default" not in p
    return p


def _normalize_precondition(item) -> dict:
    if isinstance(item, dict):
        return dict(item)
    return {"text": str(item)}


def _bump_patch(version: str) -> str:
    parts = str(version).split(".")
    while len(parts) < 3:
        parts.append("0")
    try:
        parts[2] = str(int(parts[2]) + 1)
    except ValueError:
        parts[2] = "1"
    return ".".join(parts[:3])


class _PackDumper(yaml.SafeDumper):
    """Write multi-line strings (code templates) as ``|`` blocks."""


def _represent_str(dumper: yaml.SafeDumper, value: str):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_PackDumper.add_representer(str, _represent_str)


def _v1_view(data: dict) -> dict:
    """The pack as it will be written: v1 fields only, schema_version 1."""
    ordered = {key: data.get(key) for key in PACK_FIELDS}
    ordered["schema_version"] = PACK_SCHEMA_VERSION
    return ordered


def _dump_pack(data: dict) -> str:
    return yaml.dump(_v1_view(data), Dumper=_PackDumper, allow_unicode=True, sort_keys=False)


def _safe_stem(name: str) -> str:
    return re.sub(r"[^\w\-]", "_", name)


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


# -- store ----------------------------------------------------------------------

class ToolStore:
    """Capability packs from the built-in directory plus the user directory."""

    def __init__(self, user_dir: Path | str | None = None, builtin_dir: Path | str | None = None):
        self.user_dir = Path(user_dir) if user_dir else user_capabilities_dir()
        self.builtin_dir = Path(builtin_dir) if builtin_dir else builtin_capabilities_dir()
        if _same_dir(self.user_dir, self.builtin_dir):
            raise ValueError(
                f"user_dir must not be the built-in pack directory ({self.builtin_dir}); "
                f"it is read-only"
            )
        self.usage = UsageStore(self.user_dir / USAGE_FILE)
        self._logged: set = set()                  # load_problem warnings already logged

    @property
    def tools_dir(self) -> Path:
        """0.1 name for the directory new packs are written to."""
        return self.user_dir

    # -- paths -----------------------------------------------------------------

    def _tool_path(self, name: str) -> Path:
        """Where a pack of this name is (or would be) written: the user directory."""
        return self.user_dir / f"{_safe_stem(name)}.yaml"

    def _builtin_path(self, name: str) -> Path:
        return self.builtin_dir / f"{_safe_stem(name)}.yaml"

    def _disabled_marker(self, name: str) -> Path:
        return self.user_dir / f"{_safe_stem(name)}{DISABLED_SUFFIX}"

    def is_disabled(self, name: str) -> bool:
        return self._disabled_marker(name).exists()

    def path_of(self, name: str) -> Path | None:
        """The file that ``load(name)`` reads, or None.

        A user file always wins, even next to a stale ``.disabled`` marker;
        the marker only hides the built-in copy. ``list_tools`` applies the
        same rule, so a pack dropped into the user directory by hand is
        visible everywhere or nowhere. A user file that cannot be read as a
        pack (not YAML, not UTF-8, not a mapping) gives way to a visible
        built-in pack of the same name, as ``list_tools`` lets it;
        ``load_problem`` says why.
        """
        user = self._tool_path(name)
        builtin = self._visible_builtin(name)
        if user.exists() and (builtin is None or self._read_pack(user) is not None):
            return user
        return builtin

    def _visible_builtin(self, name: str) -> Path | None:
        builtin = self._builtin_path(name)
        return builtin if builtin.exists() and not self.is_disabled(name) else None

    def load_problem(self, name: str) -> str | None:
        """Why the user pack ``name`` is passed over for the built-in one: its file exists
        but cannot be read as a pack. None otherwise. ``load``, ``path_of`` and
        ``list_tools`` then use the built-in pack; this is the warning for the caller."""
        user = self._tool_path(name)
        if not user.exists() or self._visible_builtin(name) is None:
            return None
        data, why = self._read_pack_why(user)
        if data is not None:
            return None
        return f"user pack {user} cannot be read ({why}); the built-in pack {name!r} is used instead"

    def _ensure_user_dir(self) -> None:
        self.user_dir.mkdir(parents=True, exist_ok=True)

    def enable(self, name: str) -> bool:
        """Remove the ``.disabled`` marker of a hidden built-in pack."""
        marker = self._disabled_marker(name)
        if marker.exists():
            marker.unlink()
            return True
        return False

    # -- reading ---------------------------------------------------------------

    @staticmethod
    def _pack_files(directory: Path) -> list[Path]:
        if not directory.is_dir():
            return []
        return sorted(
            p for p in directory.glob("*.yaml")
            if p.is_file() and not p.name.startswith("_")
        )

    def _read_pack(self, path: Path) -> dict | None:
        return self._read_pack_why(path)[0]

    @staticmethod
    def _read_pack_why(path: Path) -> tuple[dict | None, str | None]:
        """``(pack data, None)``, or ``(None, why)`` for a file that cannot be read as a pack."""
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                return None, "not a mapping"
            return normalize_pack(data, fallback_name=path.stem), None
        except Exception as exc:  # noqa: BLE001 - any unreadable pack reads as "not found"
            detail = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
            return None, f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__

    def _to_tool(self, data: dict, usage: dict | None = None) -> SolidifiedTool:
        usage = usage or self.usage.get(data["name"])
        filtered = {k: data[k] for k in KNOWN_FIELDS if k in data}
        filtered.update(usage)
        declared = {p["name"]: p for p in data["parameters"]}
        _, filtered["validation_errors"] = template_slots(data["code_template"], declared)
        return SolidifiedTool(**filtered)

    def load(self, name: str) -> SolidifiedTool | None:
        """Load a pack by name: the user copy wins, a disabled built-in is hidden.

        Malformed files read as "not found" so a corrupt pack cannot crash the
        caller; a malformed user file of a built-in pack's name gives way to
        the built-in one (``path_of``), with a warning in the log once per
        file version and from ``load_problem``.
        """
        path = self.path_of(name)
        if path is None:
            return None
        if path != self._tool_path(name):
            self._log_problem(name)
        data = self._read_pack(path)
        return self._to_tool(data) if data else None

    def _log_problem(self, name: str) -> None:
        problem = self.load_problem(name)
        if problem is None:
            return
        try:
            key = (str(self._tool_path(name)), self._tool_path(name).stat().st_mtime_ns)
        except OSError:
            key = (str(self._tool_path(name)), None)
        if key not in self._logged:
            self._logged.add(key)
            _log.warning("%s", problem)

    def list_tools(self) -> list[SolidifiedTool]:
        """Built-in packs plus user packs; same name -> the user pack.

        A ``.disabled`` marker hides a built-in pack only (see ``path_of``).
        """
        packs: dict[str, dict] = {}
        for path in self._pack_files(self.builtin_dir):
            data = self._read_pack(path)
            if data and not self.is_disabled(data["name"]):
                packs[data["name"]] = data
        for path in self._pack_files(self.user_dir):
            data = self._read_pack(path)
            if data:
                packs[data["name"]] = data
        usage = self.usage.read_all()
        return [
            self._to_tool(packs[name], usage.get(name, empty_usage()))
            for name in sorted(packs)
        ]

    # -- writing ---------------------------------------------------------------

    def solidify(
        self,
        name: str,
        code: str,
        description: str = "",
        display_name: str = "",
        parameters: list[dict] | None = None,
        tags: list[str] | None = None,
        source_query: str = "",
        preconditions: list | None = None,
        applies_when: list[str] | None = None,
        not_for: list[str] | None = None,
        validator: dict | None = None,
    ) -> SolidifiedTool:
        """Save a successful code execution as a v1 pack in the user directory.

        ``tags`` is accepted for 0.1 callers but no longer written; use
        ``applies_when``. Raises ``ValueError`` listing the problems when the
        pack would not pass ``validate_pack``.
        """
        data = normalize_pack({
            "schema_version": PACK_SCHEMA_VERSION,
            "name": name,
            "display_name": display_name or name.replace("_", " ").title(),
            "description": description,
            "version": "1.0.0",
            "code_template": code,
            "parameters": parameters or [],
            "preconditions": preconditions or [],
            "applies_when": applies_when or [],
            "not_for": not_for or [],
            "validator": validator,
            "created_at": datetime.now().isoformat(),
            "source_query": source_query,
        })
        self._check(data)
        self._write_user_pack(name, data)
        return self._to_tool(data)

    @staticmethod
    def _check(data: dict) -> None:
        errors = validate_pack(_v1_view(data))
        if errors:
            raise ValueError("invalid capability pack: " + "; ".join(errors))

    def _write_user_pack(self, name: str, data: dict) -> Path:
        self._ensure_user_dir()
        path = self._tool_path(name)
        path.write_text(_dump_pack(data), encoding="utf-8")
        self.enable(name)
        return path

    def delete(self, name: str) -> bool:
        """Remove a user pack; hide a built-in one behind a ``.disabled`` marker."""
        user = self._tool_path(name)
        if user.exists():
            user.unlink()
            self.usage.forget(name)
            if self._builtin_path(name).exists():
                self._disabled_marker(name).touch()
            return True
        if self._builtin_path(name).exists() and not self.is_disabled(name):
            self._ensure_user_dir()
            self._disabled_marker(name).touch()
            self.usage.forget(name)
            return True
        return False

    EDITABLE_FIELDS = frozenset({
        "display_name", "description", "code_template", "parameters",
        "source_query", "preconditions", "applies_when", "not_for",
        "validator", "revit_versions",
    })

    def update(self, name: str, updates: dict) -> SolidifiedTool | None:
        """Change editable fields; a built-in pack is copied to the user
        directory first. ``code_template`` or ``parameters`` changes bump the
        patch version. A user file that cannot be read as a pack is never
        written over: None, as before."""
        user = self._tool_path(name)
        source = user if user.exists() else self.path_of(name)
        if source is None:
            return None
        data = self._read_pack(source)
        if data is None:
            return None
        before = {key: data.get(key) for key in ("code_template", "parameters")}
        for key, value in updates.items():
            if key in self.EDITABLE_FIELDS and value is not None:
                data[key] = value
        data = normalize_pack(data)
        if any(data.get(key) != before[key] for key in before):
            data["version"] = _bump_patch(data["version"])
        self._check(data)
        self._write_user_pack(name, data)
        return self._to_tool(data)

    def record_usage(self, name: str, success: bool = True) -> None:
        """Record a run in ``usage.json``; the pack file is untouched.

        On success: increment execution_count, reset failure_count.
        On failure: increment failure_count (for health check / auto-degradation).
        Never raises: a data directory that cannot be written costs the
        counter, not the result of the run that was just executed.
        """
        if self.path_of(name) is None:
            return
        try:
            self.usage.record(name, success)
        except OSError as exc:
            _log.warning("usage.json not updated for %s: %s", name, exc)

    def health_check(self, name: str) -> dict:
        """Check if a tool is healthy or should fall back to writing fresh code.

        Returns:
            {"status": "healthy"|"stale"|"failing"|"invalid"|"not_found",
             "issues": [...], "recommendation": "use_tool"|"write_new_code"}
        """
        tool = self.load(name)
        if not tool:
            return {"status": "not_found", "issues": [], "recommendation": "write_new_code"}

        if errors := tool.validation_errors:
            return {"status": "invalid", "issues": errors, "recommendation": "write_new_code"}

        issues: list[str] = []

        # Consecutive failures
        if tool.failure_count >= 2:
            issues.append(f"连续失败 {tool.failure_count} 次 / {tool.failure_count} consecutive failures")

        # Time decay: stale if unused for 30+ days
        if tool.last_used:
            try:
                last = datetime.fromisoformat(tool.last_used)
                days = (datetime.now() - last).days
                if days > 30:
                    issues.append(f"未使用 {days} 天 / unused for {days} days")
            except ValueError:
                pass

        # Never successfully used - informational, not a hard block
        if tool.execution_count == 0:
            issues.append("从未成功执行 / never successfully executed")

        if issues:
            status = "failing" if tool.failure_count >= 2 else "stale"
            rec = "write_new_code" if tool.failure_count >= 2 else "use_tool"
            return {"status": status, "issues": issues, "recommendation": rec}

        return {"status": "healthy", "issues": [], "recommendation": "use_tool"}

    def get_atom_sources(self, name: str) -> list[dict]:
        """Parameters whose value comes from a Revit query (``source: tool:*``)."""
        tool = self.load(name)
        if not tool:
            return []
        return [
            {"name": p["name"], "source": p["source"], "description": p.get("description", "")}
            for p in tool.parameters
            if str(p.get("source", "")).startswith("tool:")
        ]

    # -- Validation ------------------------------------------------------------

    def validate_params(
        self, name: str, params: dict | None = None,
        resolved_atoms: dict | None = None,
    ) -> tuple[bool, list[str], dict]:
        """Validate and fill default values for tool parameters.

        Args:
            name: tool name
            params: user/LLM-provided parameter values
            resolved_atoms: {param_name: [{label,value},...]} from a choices query

        Returns (valid, errors, filled_params):
          - valid: True if all checks pass
          - errors: list of human-readable error strings
          - filled_params: params dict with defaults filled in

        A value of None counts as not given (its default applies, or it is
        missing). Every value given must be a single string, number or boolean
        that fits the declared type (``_value_problem``): a list or an object
        is refused for every type, a boolean or a non-finite number for a
        numeric one.
        """
        snapshot = _render_snapshot.get()
        tool = snapshot[2] if snapshot and snapshot[0] is self and snapshot[1] == name else self.load(name)
        if not tool:
            return False, [f"Tool '{name}' not found"], {}
        if errors := tool.validation_errors:
            return False, errors, {}
        return self._fill_params(tool, params, resolved_atoms)

    @staticmethod
    def _fill_params(
        tool: SolidifiedTool, params: dict | None, resolved_atoms: dict | None = None,
    ) -> tuple[bool, list[str], dict]:
        params = dict(params or {})
        resolved_atoms = resolved_atoms or {}
        errors: list[str] = []

        for pdef in tool.parameters:
            pname = pdef["name"]
            ptype = str(pdef.get("type", "string")).lower()
            source = str(pdef.get("source", ""))
            kind = source.split(":", 1)[0]
            has_default = "default" in pdef

            if pname in params and params[pname] is None:
                del params[pname]                  # null is no value: treated as not given
            if pname not in params:
                # Values that must come from Revit: never guessed, never defaulted
                if kind in ("tool", "query", "interactive"):
                    if pname in resolved_atoms and resolved_atoms[pname]:
                        errors.append(
                            f"Parameter '{pname}' requires selection from Revit "
                            f"(source: {source}). Available: "
                            f"{[c['label'] for c in resolved_atoms[pname][:5]]}"
                        )
                    else:
                        errors.append(
                            f"Parameter '{pname}' must be resolved from Revit "
                            f"(source: {source}) - call get_tool_choices first"
                        )
                    continue
                # designer / answer / legacy ask_user: provided or defaulted.
                # ``required: false`` never means "leave the placeholder empty":
                # a value the template needs must come from somewhere.
                if has_default:
                    params[pname] = pdef["default"]
                    # Legacy packs may use text as a quoted numeric default.
                    # Bare defaults were checked while validating the template.
                    if ptype in NUMERIC_TYPES and isinstance(pdef["default"], (str, int, float)):
                        continue
                elif source == "default":
                    errors.append(f"Parameter '{pname}' has source=default but no default value")
                    continue
                else:
                    errors.append(f"Missing required parameter: {pname}")
                    continue

            problem = _value_problem(params[pname], ptype)
            if problem:
                errors.append(f"Parameter '{pname}' {problem}")

        return (len(errors) == 0, errors, params)

    # -- Render ----------------------------------------------------------------

    def render(self, name: str, params: dict | None = None) -> tuple[str | None, list[str]]:
        """Apply host parameter validation, then fill only the template's original sites."""
        tool = self.load(name)
        if not tool:
            return None, [f"Tool '{name}' not found"]

        code = tool.code_template
        declared = {p["name"]: p for p in tool.parameters}
        slots, errors = template_slots(code, declared)
        if errors:
            return None, errors
        binding = _render_snapshot.set((self, name, tool))
        try:
            valid, errors, filled = self.validate_params(name, params)
        finally:
            _render_snapshot.reset(binding)
        if not valid:
            return None, errors

        parts = []
        pos = 0
        for slot in slots:
            if slot.name not in filled:
                return None, [f"Missing required parameter: {slot.name}"]
            value = filled[slot.name]
            try:
                if slot.context == CODE:
                    text = bare_literal(value, str(declared[slot.name]["type"]).lower())
                    left = code[slot.start - 1:slot.start]
                    if (_SCALAR_EDGE.fullmatch(left) or (left == "-" and text.startswith("-"))
                            or (pos > 0 and pos == slot.start)):
                        text = " " + text
                    if _SCALAR_EDGE.match(code, slot.end):
                        text += " "
                else:
                    text = escape_param_value(value)
            except (TypeError, ValueError) as exc:
                return None, [f"Parameter '{slot.name}' {exc}"]
            parts.append(code[pos:slot.start])
            parts.append(text)
            pos = slot.end
        parts.append(code[pos:])
        return "".join(parts), []

    def render_code(self, name: str, params: dict | None = None) -> str | None:
        """0.1 form of ``render``: the code, or None when it must not run."""
        return self.render(name, params)[0]

    def get_dynamic_params(self, name: str) -> list[dict]:
        """Extract parameters that need dynamic choices from Revit.

        Returns list of dicts: [{name, choices_from, description}, ...].
        Every ``source: tool:*`` parameter has a ``choices_from`` after
        normalisation, so this covers them too.
        """
        tool = self.load(name)
        if not tool:
            return []
        return [
            {
                "name": p["name"],
                "choices_from": p["choices_from"],
                "description": p.get("description", ""),
                **({"type": p["type"]} if p["choices_from"] == "elements" else {}),
                **({"source": p["source"]} if optional_element_pick(p) else {}),
            }
            for p in tool.parameters
            if "choices_from" in p
        ]

    def search(self, query: str) -> list[SolidifiedTool]:
        """Simple keyword search across tool names, descriptions, and tags."""
        query_lower = query.lower()
        results = []
        for tool in self.list_tools():
            searchable = f"{tool.name} {tool.display_name} {tool.description} {' '.join(tool.tags)}".lower()
            if query_lower in searchable:
                results.append(tool)
        return results

    def match_tool(self, user_query: str) -> SolidifiedTool | None:
        """Smart tool matching using enriched metadata.

        Scoring considers: name, description, tags, source_query,
        AND the applies_when / not_for fields.

        Returns None when the caller should fall back to writing fresh code.
        Also returns None if the tool is unhealthy (failing/stale).
        """
        query_lower = user_query.lower()
        keywords = set(re.findall(r'[\u4e00-\u9fff]+|[a-z_]+', query_lower))
        if not keywords:
            return None

        scored: list[tuple[float, SolidifiedTool]] = []
        for tool in self.list_tools():
            # An invalid or repeatedly failing pack cannot satisfy the request.
            if tool.failure_count >= 2 or tool.validation_errors:
                continue

            # Check not_for exclusions first
            not_for_texts = [nf.lower() for nf in (tool.not_for or [])]
            if any(nf in query_lower for nf in not_for_texts):
                continue

            # Score from applies_when (high weight - explicit trigger phrases)
            applies_texts = [aw.lower() for aw in (tool.applies_when or [])]
            applies_hits = sum(2.0 for aw in applies_texts if aw in query_lower)

            # Score from standard fields
            searchable = (
                f"{tool.name} {tool.display_name} {tool.description} "
                f"{' '.join(tool.tags)} {tool.source_query}"
            ).lower()
            keyword_hits = sum(1 for kw in keywords if kw in searchable)

            total_hits = applies_hits + keyword_hits
            max_possible = len(keywords) + len(applies_texts) * 2
            if max_possible == 0:
                continue
            score = total_hits / max_possible

            # Soft penalty for never-executed tools (less confident match)
            if tool.execution_count <= 0:
                score *= 0.8

            if total_hits > 0:
                scored.append((score, tool))

        if not scored:
            return None

        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_tool = scored[0]

        if best_score < 0.3:
            return None
        if len(scored) > 1 and scored[1][0] >= best_score * 0.8:
            return None  # ambiguous

        return best_tool
