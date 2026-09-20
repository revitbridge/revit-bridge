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

import os
import re
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path

import yaml

from revit_bridge.paths import (
    ENV_CAPABILITIES_DIR,
    builtin_capabilities_dir,
    user_capabilities_dir,
)
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

PACK_SCHEMA_VERSION = 1
DISABLED_SUFFIX = ".disabled"

# A ``{name}`` token in a code template; substituted by ``render``.
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_]\w*)\}")

# Fields of a v1 pack file, in the order they are written. Parameter ``source``
# values are "designer", "tool:<kind>", "answer" or "default".
PACK_FIELDS = (
    "schema_version", "name", "display_name", "description", "version",
    "revit_versions", "code_template", "parameters", "preconditions",
    "not_for", "applies_when", "validator", "fixtures",
    "approved_by", "approved_at", "created_at", "source_query",
)


def default_capabilities_dir() -> Path:
    """Deprecated 0.1 name. Returns the user directory when
    ``REVIT_BRIDGE_CAPABILITIES_DIR`` is set, else the built-in directory."""
    if os.environ.get(ENV_CAPABILITIES_DIR, "").strip():
        return user_capabilities_dir()
    return builtin_capabilities_dir()


def escape_param_value(value) -> str:
    """Make a parameter value safe inside a C# string literal (P0-3).

    Placeholders in code templates sit between double quotes; a value carrying
    a quote, backslash or newline would otherwise terminate the literal and
    inject code. Non-strings are rendered with ``str``.
    """
    if isinstance(value, str):
        return (value.replace("\\", "\\\\")
                     .replace('"', '\\"')
                     .replace("\r", "\\r")
                     .replace("\n", "\\n"))
    return str(value)


@dataclass
class SolidifiedTool:
    """A reusable tool created from a successful code execution.

    Parameter dicts carry the v1 vocabulary after loading:
      - ``source``: "designer" | "tool:<kind>" | "answer" | "default"
      - ``choices_from``: query kind for ``get_tool_choices`` (levels,
        family_types:<OST_*>, floor_types, elements:<OST_*>)
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


KNOWN_FIELDS = {f.name for f in fields(SolidifiedTool)}


# -- normalisation -------------------------------------------------------------

def normalize_pack(data: dict, fallback_name: str = "") -> dict:
    """Return a pack dict in the v1 shape.

    Files without ``schema_version`` are v0 and get ``version = "0.0.0"``.
    Missing parameter keys are filled the same way for both layouts: a
    ``source`` is inferred (``choices_from`` -> ``tool:<kind>``, a ``default``
    -> ``default``, else ``designer``; the 0.1 words ``query:``/``interactive:``
    -> ``tool:``, ``ask_user`` -> ``designer``), ``unit`` is ``mm`` when the
    description says ``(mm)``, ``required`` means "no default". String
    preconditions become ``{"text": ...}``. Usage counters are dropped (they
    live in ``usage.json``); ``tags`` are kept for reading.
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


def _dump_pack(data: dict) -> str:
    ordered = {key: data.get(key) for key in PACK_FIELDS}
    ordered["schema_version"] = PACK_SCHEMA_VERSION
    return yaml.dump(ordered, Dumper=_PackDumper, allow_unicode=True, sort_keys=False)


def _safe_stem(name: str) -> str:
    return re.sub(r"[^\w\-]", "_", name)


# -- store ----------------------------------------------------------------------

class ToolStore:
    """Capability packs from the built-in directory plus the user directory."""

    def __init__(self, user_dir: Path | str | None = None, builtin_dir: Path | str | None = None):
        self.user_dir = Path(user_dir) if user_dir else user_capabilities_dir()
        self.builtin_dir = Path(builtin_dir) if builtin_dir else builtin_capabilities_dir()
        self.usage = UsageStore(self.user_dir / USAGE_FILE)

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
        """The file that ``load(name)`` reads, or None."""
        user = self._tool_path(name)
        if user.exists():
            return user
        if self.is_disabled(name):
            return None
        builtin = self._builtin_path(name)
        return builtin if builtin.exists() else None

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
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                return None
            return normalize_pack(data, fallback_name=path.stem)
        except Exception:
            return None

    def _to_tool(self, data: dict, usage: dict | None = None) -> SolidifiedTool:
        usage = usage or self.usage.get(data["name"])
        filtered = {k: data[k] for k in KNOWN_FIELDS if k in data}
        filtered.update(usage)
        return SolidifiedTool(**filtered)

    def load(self, name: str) -> SolidifiedTool | None:
        """Load a pack by name: the user copy wins, a disabled built-in is hidden.

        Malformed files read as "not found" so a corrupt pack cannot crash the
        caller.
        """
        path = self.path_of(name)
        if path is None:
            return None
        data = self._read_pack(path)
        return self._to_tool(data) if data else None

    def list_tools(self) -> list[SolidifiedTool]:
        """Built-in packs plus user packs; same name -> the user pack."""
        packs: dict[str, dict] = {}
        for path in self._pack_files(self.builtin_dir):
            data = self._read_pack(path)
            if data:
                packs[data["name"]] = data
        for path in self._pack_files(self.user_dir):
            data = self._read_pack(path)
            if data:
                packs[data["name"]] = data
        usage = self.usage.read_all()
        return [
            self._to_tool(packs[name], usage.get(name, empty_usage()))
            for name in sorted(packs)
            if not self.is_disabled(name)
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
        ``applies_when``.
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
        self._write_user_pack(name, data)
        return self._to_tool(data)

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
        patch version."""
        source = self.path_of(name)
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
        self._write_user_pack(name, data)
        return self._to_tool(data)

    def record_usage(self, name: str, success: bool = True) -> None:
        """Record a run in ``usage.json``; the pack file is untouched.

        On success: increment execution_count, reset failure_count.
        On failure: increment failure_count (for health check / auto-degradation).
        """
        if self.path_of(name) is None:
            return
        self.usage.record(name, success)

    def health_check(self, name: str) -> dict:
        """Check if a tool is healthy or should fall back to writing fresh code.

        Returns:
            {"status": "healthy"|"stale"|"failing"|"not_found",
             "issues": [...], "recommendation": "use_tool"|"fallback_to_rag"}
        """
        tool = self.load(name)
        if not tool:
            return {"status": "not_found", "issues": [], "recommendation": "fallback_to_rag"}

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
            rec = "fallback_to_rag" if tool.failure_count >= 2 else "use_tool"
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
        """
        tool = self.load(name)
        if not tool:
            return False, [f"Tool '{name}' not found"], {}

        params = dict(params or {})
        resolved_atoms = resolved_atoms or {}
        errors: list[str] = []

        for pdef in tool.parameters:
            pname = pdef["name"]
            ptype = str(pdef.get("type", "string")).lower()
            source = str(pdef.get("source", ""))
            kind = source.split(":", 1)[0]
            has_default = "default" in pdef

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
                if source == "default":
                    if has_default:
                        params[pname] = pdef["default"]
                    else:
                        errors.append(f"Parameter '{pname}' has source=default but no default value")
                    continue
                # designer / answer / legacy ask_user: provided or defaulted.
                # ``required: false`` never means "leave the placeholder empty":
                # a value the template needs must come from somewhere.
                if has_default:
                    params[pname] = pdef["default"]
                else:
                    errors.append(f"Missing required parameter: {pname}")
                continue

            # Type coercion check for numeric types
            if ptype in ("double", "number", "float", "int", "integer"):
                try:
                    float(params[pname])
                except (ValueError, TypeError):
                    errors.append(
                        f"Parameter '{pname}' expects {ptype}, got {params[pname]!r}"
                    )

        return (len(errors) == 0, errors, params)

    # -- Render ----------------------------------------------------------------

    def render(self, name: str, params: dict | None = None) -> tuple[str | None, list[str]]:
        """Fill the template's ``{placeholders}``; ``(code, [])`` or ``(None, errors)``.

        Validates the parameters first. Code in which a ``{identifier}`` token
        survives substitution is refused too: a leftover placeholder would
        otherwise be shipped to Revit as C#. Templates therefore cannot use
        ``$"{x}"`` interpolation with a bare identifier unless ``x`` is a
        declared parameter.
        """
        tool = self.load(name)
        if not tool:
            return None, [f"Tool '{name}' not found"]

        valid, errors, filled = self.validate_params(name, params)
        if not valid:
            return None, errors

        code = tool.code_template
        for k, v in filled.items():
            code = code.replace(f"{{{k}}}", escape_param_value(v))
        leftover = sorted(set(PLACEHOLDER_RE.findall(code)))
        if leftover:
            return None, [
                f"Template still contains placeholder(s) {leftover}: "
                "declare them as parameters or remove them from the code"
            ]
        return code, []

    def render_code(self, name: str, params: dict | None = None) -> str | None:
        """0.1 form of ``render``: the code, or None when it must not run."""
        return self.render(name, params)[0]

    def get_dynamic_params(self, name: str) -> list[dict]:
        """Extract parameters that need dynamic choices from Revit.

        Returns list of dicts: [{name, choices_from, description}, ...]
        """
        tool = self.load(name)
        if not tool:
            return []
        return [
            {
                "name": p["name"],
                "choices_from": p["choices_from"],
                "description": p.get("description", ""),
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
        keywords = set(re.findall(r'[一-鿿]+|[a-z_]+', query_lower))
        if not keywords:
            return None

        scored: list[tuple[float, SolidifiedTool]] = []
        for tool in self.list_tools():
            # Hard-skip only actively failing tools (consecutive failures)
            if tool.failure_count >= 2:
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
