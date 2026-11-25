"""The linear workflow v1 format. Validation always consults the supplied catalog."""
from __future__ import annotations

import math
import re
from collections.abc import Iterator
from itertools import islice
from typing import Any

from revit_bridge.capabilities.schema import PARAM_TYPES, SEMVER_RE
from revit_bridge.capabilities.store import DISABLED_SUFFIX, SolidifiedTool, ToolStore

MAX_ERRORS = 50

_FIELDS = {"schema_version", "kind", "name", "display_name", "summary", "description", "version", "inputs", "steps"}
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DEVICE_NAME = re.compile(r"CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]", re.I)


def valid_name(value: Any) -> bool:
    """The host's tool-name rules, including the portable filename restrictions."""
    return (isinstance(value, str) and len(value) <= 100 and _IDENTIFIER.fullmatch(value) is not None
            and not _DEVICE_NAME.fullmatch(value))


def _describe(value: Any) -> str:
    """Render only short strings. A container may be shared, cyclic or deeply nested."""
    if type(value) is str and len(value) <= 100:
        return repr(value)
    return type(value).__name__[:100]


def _unknown(data: dict, allowed: set[str], label: str) -> Iterator[str]:
    for key in data:
        if key not in allowed:
            yield f"{label}: unknown field {_describe(key)}"


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, int, bool)) or (isinstance(value, float) and math.isfinite(value))


def _valid_path(path: Any) -> bool:
    return isinstance(path, str) and all(part and not re.fullmatch(r"-[0-9]+", part) for part in path.split("."))


def _check_id(item: dict, key: str, label: str, seen: set[str]) -> Iterator[str]:
    value = item.get(key)
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        yield f"{label}.{key}: expected an identifier"
    elif value in seen:
        yield f"{label}.{key}: duplicate {_describe(value)}"
    else:
        seen.add(value)


def _validate_source(value: Any, label: str, inputs: set[str], earlier: set[str]) -> Iterator[str]:
    if not isinstance(value, dict):
        if not _scalar(value):
            yield f"{label}: expected a scalar literal, input or from reference"
        return
    if "input" in value:
        yield from _unknown(value, {"input"}, label)
        name = value["input"]
        if not isinstance(name, str) or name not in inputs:
            yield f"{label}.input: unknown input {_describe(name)}"
        return
    yield from _unknown(value, {"from", "path"}, label)
    step = value.get("from")
    if not isinstance(step, str) or step not in earlier:
        yield f"{label}.from: {_describe(step)} is not an earlier step"
    if not _valid_path(value.get("path")):
        yield f"{label}.path: expected dot-separated keys or non-negative list indexes"


def _validate_step(step: dict, label: str, tools: ToolStore, inputs: set[str], earlier: set[str],
                   workflow_name: str) -> Iterator[str]:
    yield from _unknown(step, {"id", "title", "tool", "params"}, label)
    if not _text(step.get("title")):
        yield f"{label}.title: required, non-empty string"
    name = step.get("tool")
    tool = tools.load(name) if valid_name(name) else None
    if tool is None or tool.name != name:
        if valid_name(name) and (name == workflow_name or
                                 (tools.user_dir.parent / "workflows" / f"{name}.yaml").is_file()):
            yield f"{label}.tool: {_describe(name)} names a workflow; nested workflows are not allowed"
        else:
            yield f"{label}.tool: unknown tool {_describe(name)}"
        tool = None
    params = step.get("params")
    if not isinstance(params, dict):
        yield f"{label}.params: required mapping"
        return
    declared = {p["name"]: p for p in tool.parameters} if tool else {}
    for key, value in params.items():
        location = f"{label}.params[{_describe(key)}]"
        if tool and key not in declared:
            yield f"{location}: unknown parameter for tool {_describe(name)}"
        yield from _validate_source(value, location, inputs, earlier)
    for key, definition in declared.items():
        if definition.get("required") and key not in params:
            yield f"{label}.params[{_describe(key)}]: required parameter has no source"


def validate_workflow(data: Any, tool_store: ToolStore) -> list[str]:
    """Return up to 50 item-level problems. Only descend into the fixed v1 fields."""
    return list(islice(_problems(data, tool_store), MAX_ERRORS))


def _problems(data: Any, tool_store: ToolStore) -> Iterator[str]:
    if not isinstance(data, dict):
        yield "workflow: expected a mapping"
        return
    yield from _unknown(data, _FIELDS, "workflow")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        yield "schema_version: expected integer 1"
    if data.get("kind") != "workflow":
        yield "kind: expected workflow"
    name = data.get("name")
    if not valid_name(name):
        yield "name: expected a tool identifier (1-100 ASCII characters, no reserved device names)"
    elif any(path.stem.casefold() == name.casefold()
             for directory, suffix in ((tool_store.user_dir, ".yaml"), (tool_store.builtin_dir, ".yaml"),
                                       (tool_store.user_dir, DISABLED_SUFFIX))
             for path in directory.glob(f"*{suffix}")):
        yield f"name: {_describe(name)} already names a tool"
    for key in ("display_name", "summary", "description"):
        if not _text(data.get(key)):
            yield f"{key}: required, non-empty string"
    if isinstance(data.get("summary"), str) and len(data["summary"]) > 120:
        yield "summary: exceeds 120 characters"
    if not isinstance(data.get("version"), str) or not SEMVER_RE.fullmatch(data["version"]):
        yield "version: expected semver such as 1.0.0"

    inputs: set[str] = set()
    if not isinstance(data.get("inputs"), list):
        yield "inputs: expected a list"
    else:
        for index, item in enumerate(data["inputs"]):
            label = f"inputs[{index}]"
            if not isinstance(item, dict):
                yield f"{label}: expected a mapping"
                continue
            yield from _unknown(item, {"name", "type", "description", "ask"}, label)
            yield from _check_id(item, "name", label, inputs)
            if not isinstance(item.get("type"), str) or item["type"] not in PARAM_TYPES:
                yield f"{label}.type: expected a scalar parameter type"
            if not _text(item.get("description")):
                yield f"{label}.description: required, non-empty string"
            if not isinstance(item.get("ask"), bool):
                yield f"{label}.ask: expected a boolean"

    steps = data.get("steps")
    if not isinstance(steps, list):
        yield "steps: expected a list of 2-8 steps"
        return
    if not 2 <= len(steps) <= 8:
        yield "steps: expected 2-8 steps"
    earlier: set[str] = set()
    for index, step in enumerate(steps[:8]):
        label = f"steps[{index}]"
        if not isinstance(step, dict):
            yield f"{label}: expected a mapping"
            continue
        yield from _validate_step(step, label, tool_store, inputs, earlier, name)
        yield from _check_id(step, "id", label, earlier)


_VALUE_TYPES = {
    "string": (str,), "int": (int,), "integer": (int,), "bool": (bool,), "boolean": (bool,),
    "double": (int, float), "number": (int, float), "float": (int, float),
}


def resolve_step_params(step: dict, inputs: dict, results: dict, tool: SolidifiedTool | None = None) -> dict:
    """Resolve one validated step. With its tool definition, also check each declared scalar type."""
    declared = {p["name"]: p["type"].lower() for p in tool.parameters} if tool is not None else {}
    resolved = {}
    for param, source in step["params"].items():
        label = f"step {_describe(step['id'])}, parameter {_describe(param)}"
        value = source
        if isinstance(source, dict):
            if "input" in source:
                name = source["input"]
                if name not in inputs:
                    raise ValueError(f"{label}: missing input {_describe(name)}")
                value = inputs[name]
            else:
                earlier, path = source["from"], source["path"]
                label += f" from {_describe(earlier)} at {_describe(path)}"
                if earlier not in results:
                    raise ValueError(f"{label}: missing step result")
                value = results[earlier]
                for part in path.split("."):
                    if isinstance(value, dict) and part in value:
                        value = value[part]
                    elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", part):
                        try:
                            value = value[int(part)]
                        except (IndexError, ValueError):
                            raise ValueError(f"{label}: list index {_describe(part)} out of range") from None
                    else:
                        raise ValueError(f"{label}: missing path component {_describe(part)}")
        if tool is not None:
            if param not in declared:
                raise ValueError(f"{label}: unknown parameter")
            kind = declared[param]
            if type(value) not in _VALUE_TYPES.get(kind, ()) or not _scalar(value):
                raise ValueError(f"{label}: expected {_describe(kind)}, got {_describe(value)}")
        elif not _scalar(value):
            raise ValueError(f"{label}: expected a scalar value")
        resolved[param] = value
    return resolved
