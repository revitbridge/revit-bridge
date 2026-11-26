"""Capability pack v1: what a pack file must contain, and evaluable preconditions.

``validate_pack(data)`` checks a pack *as written* (spec 7): a v1 file must
declare ``schema_version: 1`` and every parameter must carry an explicit
``source`` and ``required`` (the store infers them for 0.1 files, never for
v1). ``evaluate_preconditions(pack, snapshot)`` returns the reasons a pack
must not run against the model the snapshot describes.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
from typing import Any

from revit_bridge.capabilities.scalars import NUMERIC_TYPES
from revit_bridge.capabilities.template import PLACEHOLDER_RE, Slot, template_slots
from revit_bridge.snapshot.project import ProjectSnapshot

PACK_SCHEMA_VERSION = 1

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z_]\w*$")
CATEGORY_RE = re.compile(r"^OST_[A-Za-z]+$")
ELEMENT_CHOICES = r"elements(?::OST_[A-Za-z]+)?"
CHOICES_FROM_RE = re.compile(rf"^(levels|floor_types|family_types:OST_[A-Za-z]+|{ELEMENT_CHOICES})$")
PARAM_REF_RE = re.compile(r"^\{([A-Za-z_]\w*)\}$")

TOP_LEVEL_FIELDS = frozenset({
    "schema_version", "name", "display_name", "description", "version",
    "revit_versions", "read_only", "ui_only", "code_template", "parameters", "preconditions",
    "not_for", "applies_when", "validator", "fixtures",
    "approved_by", "approved_at", "created_at", "source_query",
    "tags",   # 0.1 leftover: read, never written
})
PARAM_FIELDS = frozenset({"name", "type", "description", "source", "choices_from", "unit", "required", "default"})
PARAM_TYPES = frozenset({"string", "double", "number", "float", "int", "integer", "bool", "boolean"})
PARAM_SOURCES = frozenset({"designer", "answer", "default"})     # plus "tool:<query>"
UNITS = frozenset({"mm", "m", "feet"})
PRECONDITION_KINDS = frozenset({"levels_min", "category_present"})
VALIDATOR_KINDS = frozenset({"created_ids", "count_delta", "param_equals"})


def placeholders(code_template: str, parameters: Iterable[dict] | Mapping[str, dict]) -> list[Slot]:
    """Return declared substitution sites in source order, with half-open Python str offsets.

    Pass pack parameter definitions or a mapping keyed by parameter name.
    Native C# locals are omitted. An unsafe or ambiguous template raises ValueError.
    """
    declared = parameters if isinstance(parameters, Mapping) else {p["name"]: p for p in parameters}
    slots, errors = template_slots(code_template, declared)
    if errors:
        raise ValueError("; ".join(errors))
    return slots


def validate_pack(data: Any) -> list[str]:
    """Problems with a pack file in the v1 layout; an empty list means it is valid."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["pack must be a mapping"]

    if data.get("schema_version") != PACK_SCHEMA_VERSION:
        errors.append(f"schema_version: expected {PACK_SCHEMA_VERSION}, got {data.get('schema_version')!r}")
    for key in sorted(set(data) - TOP_LEVEL_FIELDS):
        errors.append(f"unknown field {key!r}")
    for flag in ("read_only", "ui_only"):
        if flag in data and not isinstance(data[flag], bool):
            errors.append(f"{flag}: must be a boolean")
    if data.get("read_only") is True and data.get("ui_only") is True:
        errors.append("read_only and ui_only cannot both be true")

    if not isinstance(data.get("name"), str) or not data["name"].strip():
        errors.append("name: required, non-empty string")
    for key in ("display_name", "description", "source_query", "created_at"):
        if key in data and data[key] is not None and not isinstance(data[key], str):
            errors.append(f"{key}: must be a string")
    version = data.get("version")
    if not isinstance(version, str) or not SEMVER_RE.match(version):
        errors.append(f"version: expected semver like 1.0.0, got {version!r}")
    revit_versions = data.get("revit_versions", [])
    if not isinstance(revit_versions, list) or not all(isinstance(v, (str, int)) for v in revit_versions):
        errors.append("revit_versions: must be a list of version strings")
    code = data.get("code_template")
    if not isinstance(code, str) or not code.strip():
        errors.append("code_template: required, non-empty string")
        code = ""
    for key in ("approved_by", "approved_at"):
        if data.get(key) is not None and not isinstance(data[key], str):
            errors.append(f"{key}: must be a string or null")
    for key in ("not_for", "applies_when"):
        if key in data and (not isinstance(data[key], list) or not all(isinstance(s, str) for s in data[key])):
            errors.append(f"{key}: must be a list of strings")

    params = data.get("parameters", [])
    declared: dict[str, dict] = {}
    if not isinstance(params, list):
        errors.append("parameters: must be a list")
        params = []
    for index, param in enumerate(params):
        errors.extend(_validate_param(index, param, declared))

    _, template_errors = template_slots(code, declared)
    errors.extend(template_errors)

    preconditions = data.get("preconditions", [])
    if not isinstance(preconditions, list):
        errors.append("preconditions: must be a list")
    else:
        for index, item in enumerate(preconditions):
            errors.extend(_validate_precondition(index, item))

    validator = data.get("validator")
    if validator is not None:
        errors.extend(_validate_validator(validator, declared))

    fixtures = data.get("fixtures", [])
    if not isinstance(fixtures, list):
        errors.append("fixtures: must be a list")
    else:
        for index, fixture in enumerate(fixtures):
            if not isinstance(fixture, dict) or not isinstance(fixture.get("name"), str):
                errors.append(f"fixtures[{index}]: needs a name")
                continue
            if not isinstance(fixture.get("params", {}), dict):
                errors.append(f"fixtures[{index}]: params must be a mapping")
            if not isinstance(fixture.get("expect", {}), dict):
                errors.append(f"fixtures[{index}]: expect must be a mapping")
    return errors


def optional_element_pick(param: dict) -> bool:
    """An element picker can supplement an existing designer value or selection default."""
    choices = param.get("choices_from")
    return (param.get("source") in ("designer", "default") and isinstance(choices, str)
            and re.fullmatch(ELEMENT_CHOICES, choices) is not None)


def _validate_param(index: int, param: Any, declared: dict[str, dict]) -> list[str]:
    errors: list[str] = []
    label = f"parameters[{index}]"
    if not isinstance(param, dict):
        return [f"{label}: must be a mapping"]
    name = param.get("name")
    if not isinstance(name, str) or not IDENTIFIER_RE.match(name):
        return [f"{label}: name must be an identifier, got {name!r}"]
    label = f"parameters[{index}] ({name})"
    if name in declared:
        errors.append(f"{label}: declared twice")
    declared[name] = param
    for key in sorted(set(param) - PARAM_FIELDS):
        errors.append(f"{label}: unknown field {key!r}")
    ptype = str(param.get("type", "string")).lower()
    if ptype not in PARAM_TYPES:
        errors.append(f"{label}: unknown type {param.get('type')!r}")

    source = param.get("source")
    if not isinstance(source, str) or not source:
        errors.append(f"{label}: source is required (designer | tool:<query> | answer | default)")
        source = ""
    elif source.startswith("tool:"):
        if not source[5:]:
            errors.append(f"{label}: source tool:<query> needs a query kind")
    elif source not in PARAM_SOURCES:
        errors.append(f"{label}: unknown source {source!r}")

    choices = param.get("choices_from")
    if choices is not None and (not isinstance(choices, str) or not CHOICES_FROM_RE.match(choices)):
        errors.append(f"{label}: choices_from must be levels | floor_types | family_types:OST_* | elements[:OST_*], "
                      f"got {choices!r}")
    if source.startswith("tool:") and choices is None and not CHOICES_FROM_RE.match(source[5:]):
        errors.append(f"{label}: source {source!r} names no usable query; add choices_from")
    if choices is not None and source and not source.startswith("tool:") and source != "answer" and not optional_element_pick(param):
        errors.append(f"{label}: a choices_from parameter must have source tool:<query> or answer")

    if "required" not in param or not isinstance(param["required"], bool):
        errors.append(f"{label}: required must be true or false")
    if source == "default" and "default" not in param:
        errors.append(f"{label}: source default needs a default value")
    if "default" in param and param.get("required") is True:
        errors.append(f"{label}: a parameter with a default is not required")
    unit = param.get("unit")
    if unit is not None and unit not in UNITS:
        errors.append(f"{label}: unit must be mm | m | feet, got {unit!r}")
    if unit is not None and ptype not in NUMERIC_TYPES:
        errors.append(f"{label}: unit on a non-numeric parameter")
    return errors


def _validate_precondition(index: int, item: Any) -> list[str]:
    label = f"preconditions[{index}]"
    if not isinstance(item, dict):
        return [f"{label}: must be a mapping ({{text: ...}} or {{kind: ...}})"]
    if "kind" not in item:
        if isinstance(item.get("text"), str) and item["text"].strip():
            return []
        return [f"{label}: needs a kind or a text"]
    kind = item["kind"]
    if kind == "levels_min":
        if not isinstance(item.get("value"), int) or item["value"] < 0:
            return [f"{label}: levels_min needs an integer value"]
        return []
    if kind == "category_present":
        if not isinstance(item.get("category"), str) or not CATEGORY_RE.match(item["category"]):
            return [f"{label}: category_present needs an OST_* category"]
        return []
    return [f"{label}: unknown kind {kind!r} (levels_min | category_present)"]


def _validate_validator(validator: Any, declared: dict[str, dict]) -> list[str]:
    if not isinstance(validator, dict):
        return ["validator: must be a mapping"]
    kind = validator.get("kind")
    if kind not in VALIDATOR_KINDS:
        return [f"validator: unknown kind {kind!r} (created_ids | count_delta | param_equals)"]
    errors: list[str] = []
    category = validator.get("category")
    if not _category_or_ref(category, declared):
        errors.append(f"validator: category must be an OST_* name or {{param}}, got {category!r}")
    if kind == "count_delta":
        expected = validator.get("expected")
        if isinstance(expected, bool) or not (isinstance(expected, int) or _param_ref(expected, declared)):
            errors.append(f"validator: count_delta expected must be an int or {{param}}, got {expected!r}")
    if kind == "param_equals":
        checks = validator.get("checks")
        if not isinstance(checks, list) or not checks:
            errors.append("validator: param_equals needs a non-empty checks list")
        else:
            for index, check in enumerate(checks):
                if not isinstance(check, dict) or not isinstance(check.get("param_name"), str) \
                        or not isinstance(check.get("spec_param"), str):
                    errors.append(f"validator: checks[{index}] needs param_name and spec_param")
                elif check["spec_param"] not in declared:
                    errors.append(f"validator: checks[{index}] spec_param {check['spec_param']!r} is not a parameter")
    return errors


def _param_ref(value: Any, declared: dict[str, dict]) -> bool:
    return isinstance(value, str) and bool(PARAM_REF_RE.match(value)) and PARAM_REF_RE.match(value).group(1) in declared


def _category_or_ref(value: Any, declared: dict[str, dict]) -> bool:
    return isinstance(value, str) and (bool(CATEGORY_RE.match(value)) or _param_ref(value, declared))


# -- preconditions ------------------------------------------------------------------------

def precondition_categories(pack) -> list[str]:
    """OST_* names the pack's preconditions and validator refer to (for the snapshot)."""
    found: list[str] = []
    for item in getattr(pack, "preconditions", None) or []:
        cat = item.get("category") if isinstance(item, dict) else None
        if isinstance(cat, str) and CATEGORY_RE.match(cat) and cat not in found:
            found.append(cat)
    validator = getattr(pack, "validator", None) or {}
    cat = validator.get("category") if isinstance(validator, dict) else None
    if isinstance(cat, str) and CATEGORY_RE.match(cat) and cat not in found:
        found.append(cat)
    return found


def evaluate_preconditions(pack, snapshot: ProjectSnapshot | None) -> list[str]:
    """Reasons the pack must not run now. Text items are display-only; a kind the
    snapshot cannot answer (category not in it, or no snapshot) is not a failure."""
    failures: list[str] = []
    if snapshot is None:
        return failures
    types = {summary.category: summary for summary in snapshot.family_types}
    for item in getattr(pack, "preconditions", None) or []:
        if not isinstance(item, dict) or "kind" not in item:
            continue
        kind = item["kind"]
        if kind == "levels_min":
            needed = int(item.get("value", 1))
            if len(snapshot.levels) < needed:
                failures.append(f"levels_min {needed}: the model has {len(snapshot.levels)} level(s)")
        elif kind == "category_present":
            category = str(item.get("category", ""))
            summary = types.get(category)
            if summary is not None and summary.count <= 0:
                failures.append(f"category_present {category}: no family types of that category are loaded")
    return failures
