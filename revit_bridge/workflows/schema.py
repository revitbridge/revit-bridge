"""The linear workflow v1 format. Validation always consults the supplied catalog."""
from __future__ import annotations

import math
import re
from typing import Any

from revit_bridge.capabilities.schema import PARAM_TYPES, SEMVER_RE
from revit_bridge.capabilities.store import ToolStore

_FIELDS = {"schema_version", "kind", "name", "display_name", "summary", "description", "version", "inputs", "steps"}
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DEVICE_NAME = re.compile(r"CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]", re.I)


def valid_name(value: Any) -> bool:
    """The host's tool-name rules, including the portable filename restrictions."""
    return (isinstance(value, str) and len(value) <= 100 and _IDENTIFIER.fullmatch(value) is not None
            and not _DEVICE_NAME.fullmatch(value))


def _unknown(data: dict, allowed: set[str], label: str) -> list[str]:
    return [f"{label}: unknown field {key!r}" for key in data if key not in allowed]


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, int, bool)) or (isinstance(value, float) and math.isfinite(value))


def _valid_path(path: Any) -> bool:
    return isinstance(path, str) and all(part and not re.fullmatch(r"-[0-9]+", part) for part in path.split("."))


def _item_id(item: dict, key: str, label: str, seen: set[str], errors: list[str]) -> str | None:
    value = item.get(key)
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        errors.append(f"{label}.{key}: expected an identifier")
        return None
    if value in seen:
        errors.append(f"{label}.{key}: duplicate {value!r}")
    return value


def _validate_source(value: Any, label: str, inputs: set[str], earlier: set[str]) -> list[str]:
    if not isinstance(value, dict):
        return [] if _scalar(value) else [f"{label}: expected a scalar literal, input or from reference"]
    if "input" in value:
        errors = _unknown(value, {"input"}, label)
        name = value["input"]
        if not isinstance(name, str) or name not in inputs:
            errors.append(f"{label}.input: unknown input {name!r}")
        return errors
    errors = _unknown(value, {"from", "path"}, label)
    step = value.get("from")
    if not isinstance(step, str) or step not in earlier:
        errors.append(f"{label}.from: {step!r} is not an earlier step")
    if not _valid_path(value.get("path")):
        errors.append(f"{label}.path: expected dot-separated keys or non-negative list indexes")
    return errors


def _validate_step(step: dict, label: str, tools: ToolStore, inputs: set[str], earlier: set[str],
                   workflow_name: str) -> list[str]:
    errors = _unknown(step, {"id", "title", "tool", "params"}, label)
    if not _text(step.get("title")):
        errors.append(f"{label}.title: required, non-empty string")
    name = step.get("tool")
    tool = tools.load(name) if valid_name(name) else None
    if tool is None or tool.name != name:
        if valid_name(name) and (name == workflow_name or
                                 (tools.user_dir.parent / "workflows" / f"{name}.yaml").is_file()):
            errors.append(f"{label}.tool: {name!r} names a workflow; nested workflows are not allowed")
        else:
            errors.append(f"{label}.tool: unknown tool {name!r}")
        tool = None
    params = step.get("params")
    if not isinstance(params, dict):
        return errors + [f"{label}.params: required mapping"]
    declared = {p["name"]: p for p in tool.parameters} if tool else {}
    for key, value in params.items():
        location = f"{label}.params.{key}"
        if tool and key not in declared:
            errors.append(f"{location}: unknown parameter for tool {name!r}")
        errors.extend(_validate_source(value, location, inputs, earlier))
    for key, definition in declared.items():
        if definition.get("required") and key not in params:
            errors.append(f"{label}.params.{key}: required parameter has no source")
    return errors


def validate_workflow(data: Any, tool_store: ToolStore) -> list[str]:
    """Return every item-level problem. An empty list means the definition is valid now."""
    if not isinstance(data, dict):
        return ["workflow: expected a mapping"]
    errors = _unknown(data, _FIELDS, "workflow")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        errors.append("schema_version: expected integer 1")
    if data.get("kind") != "workflow":
        errors.append("kind: expected workflow")
    name = data.get("name")
    if not valid_name(name):
        errors.append("name: expected a tool identifier (1-100 ASCII characters, no reserved device names)")
    elif tool_store.path_of(name) is not None or tool_store.is_disabled(name):
        errors.append(f"name: {name!r} already names a tool")
    for key in ("display_name", "summary", "description"):
        if not _text(data.get(key)):
            errors.append(f"{key}: required, non-empty string")
    if isinstance(data.get("summary"), str) and len(data["summary"]) > 120:
        errors.append("summary: exceeds 120 characters")
    if not isinstance(data.get("version"), str) or not SEMVER_RE.fullmatch(data["version"]):
        errors.append("version: expected semver such as 1.0.0")

    inputs: set[str] = set()
    if not isinstance(data.get("inputs"), list):
        errors.append("inputs: expected a list")
    else:
        for index, item in enumerate(data["inputs"]):
            label = f"inputs[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{label}: expected a mapping")
                continue
            errors.extend(_unknown(item, {"name", "type", "description", "ask"}, label))
            input_name = _item_id(item, "name", label, inputs, errors)
            if input_name:
                inputs.add(input_name)
            if not isinstance(item.get("type"), str) or item["type"] not in PARAM_TYPES:
                errors.append(f"{label}.type: expected a scalar parameter type")
            if not _text(item.get("description")):
                errors.append(f"{label}.description: required, non-empty string")
            if not isinstance(item.get("ask"), bool):
                errors.append(f"{label}.ask: expected a boolean")

    steps = data.get("steps")
    if not isinstance(steps, list):
        return errors + ["steps: expected a list of 2-8 steps"]
    if not 2 <= len(steps) <= 8:
        errors.append("steps: expected 2-8 steps")
    earlier: set[str] = set()
    for index, step in enumerate(steps):
        label = f"steps[{index}]"
        if not isinstance(step, dict):
            errors.append(f"{label}: expected a mapping")
            continue
        step_id = _item_id(step, "id", label, earlier, errors)
        errors.extend(_validate_step(step, label, tool_store, inputs, earlier, name))
        if step_id:
            earlier.add(step_id)
    return errors


def resolve_step_params(step: dict, inputs: dict, results: dict) -> dict:
    """Resolve the parameters of one validated step from the host's inputs and earlier results."""
    resolved = {}
    for param, source in step["params"].items():
        label = f"step {step['id']!r}, parameter {param!r}"
        value = source
        if isinstance(source, dict):
            if "input" in source:
                name = source["input"]
                if name not in inputs:
                    raise ValueError(f"{label}: missing input {name!r}")
                value = inputs[name]
            else:
                earlier, path = source["from"], source["path"]
                label += f" from {earlier!r} at {path!r}"
                if earlier not in results:
                    raise ValueError(f"{label}: missing step result")
                value = results[earlier]
                for part in path.split("."):
                    if isinstance(value, dict) and part in value:
                        value = value[part]
                    elif isinstance(value, list) and re.fullmatch(r"[0-9]+", part):
                        try:
                            value = value[int(part.lstrip("0") or "0")]
                        except (IndexError, ValueError):
                            raise ValueError(f"{label}: list index {part!r} out of range") from None
                    else:
                        raise ValueError(f"{label}: missing path component {part!r}")
        if not _scalar(value):
            raise ValueError(f"{label}: expected a scalar value")
        resolved[param] = value
    return resolved
