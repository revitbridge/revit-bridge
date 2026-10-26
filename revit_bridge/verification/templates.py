"""Read-back templates for ad-hoc code: the three built-in validators with explicit parameters.

``plan_verification(template, params)`` takes one of:

    created_ids    {"category": "OST_*"}
    count_delta    {"category": "OST_*", "expected": int}      (negative for removals)
    param_equals   {"category": "OST_*", "checks": [{"param": str, "expected": str | number | bool,
                                                     "unit": "mm" | "m" | "feet" | null,
                                                     "tolerance": number | null}]}

``check_template`` validates and normalises them (every problem at once);
``validator_call`` turns a checked template into the arguments of the
matching validator in ``validators.builtin``. Those validators read the
category and the expected values from a TaskSpec (``{param}`` references in a
pack's configuration), so the adapter builds a throwaway TaskSpec that binds
each expected value - nothing is confirmed or stored with it - and a
configuration in the pack shape: ``param_equals`` check ``i`` becomes the
spec parameter ``check_<i>`` with the check's ``unit``, and its
``tolerance`` goes to the validator as a keyword. The capability pack path
never calls this module.

``param`` names a BuiltInParameter (``WALL_USER_HEIGHT_PARAM``) or a
parameter's display name. It is written into the fixed read-back probe as a
C# string literal, so a double quote, a backslash or a control character is
refused here.
"""
from __future__ import annotations

from typing import Any

from revit_bridge.spec.models import Action, ParamBinding, Source, TaskSpec
from revit_bridge.validators.builtin import CATEGORY_RE, MM_PER_UNIT, get_validator

TEMPLATES = ("created_ids", "count_delta", "param_equals")
TEMPLATE_KEYS = {
    "created_ids": ("category",),
    "count_delta": ("category", "expected"),
    "param_equals": ("category", "checks"),
}
CHECK_KEYS = ("param", "expected", "unit", "tolerance")
UNITS = tuple(MM_PER_UNIT)
MAX_PARAM_CHARS = 200
SPEC_EVIDENCE = "plan_verification"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _integer(value: Any) -> int | None:
    """An int, or a float with no fractional part; None for anything else (bool included)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _param_problem(name: Any) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return "must be a non-empty string"
    if len(name) > MAX_PARAM_CHARS:
        return f"is longer than {MAX_PARAM_CHARS} characters"
    if any(ch in name for ch in '"\\') or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return "must not contain a double quote, a backslash or a control character"
    return None


def _keys(where: str, given: dict, allowed: tuple[str, ...], required: tuple[str, ...]) -> list[str]:
    problems = [f"{where}.{key} is required" for key in required if key not in given]
    problems += [f"{where}.{key} is not a key here (allowed: {', '.join(allowed)})"
                 for key in given if key not in allowed]
    return problems


def _check_checks(checks: Any) -> tuple[list[dict], list[str]]:
    if not isinstance(checks, list) or not checks:
        return [], ["params.checks must be a non-empty list of {param, expected, unit, tolerance}"]
    out: list[dict] = []
    problems: list[str] = []
    seen: set[str] = set()
    for index, check in enumerate(checks):
        where = f"params.checks[{index}]"
        if not isinstance(check, dict):
            problems.append(f"{where} must be an object {{param, expected, unit, tolerance}}")
            continue
        problems += _keys(where, check, CHECK_KEYS, ("param", "expected"))
        name = check.get("param")
        if "param" in check:
            problem = _param_problem(name)
            if problem:
                problems.append(f"{where}.param {problem}")
            elif name in seen:
                problems.append(f"{where}.param repeats {name!r}")
            else:
                seen.add(name)
        expected = check.get("expected")
        if "expected" in check and not (isinstance(expected, (str, bool)) or _is_number(expected)):
            problems.append(f"{where}.expected must be a string, a number or a boolean, got {expected!r}")
        unit = check.get("unit")
        if unit is not None and unit not in UNITS:
            problems.append(f"{where}.unit must be one of {', '.join(UNITS)} or null, got {unit!r}")
        tolerance = check.get("tolerance")
        if tolerance is not None and not (_is_number(tolerance) and tolerance >= 0):
            problems.append(f"{where}.tolerance must be a number >= 0 or null, got {tolerance!r}")
        out.append({"param": name, "expected": expected, "unit": unit, "tolerance": tolerance})
    return out, problems


def check_template(template: Any, params: Any) -> tuple[dict | None, list[str]]:
    """``(normalised params, [])`` for a valid template, else ``(None, problems)``."""
    if not isinstance(template, str) or template not in TEMPLATES:
        return None, [f"template must be one of {', '.join(TEMPLATES)}, got {template!r}"]
    if not isinstance(params, dict):
        return None, [f"params must be an object with {', '.join(TEMPLATE_KEYS[template])}"]
    problems = _keys("params", params, TEMPLATE_KEYS[template], TEMPLATE_KEYS[template])
    normal: dict[str, Any] = {}
    if "category" in params:
        category = params["category"]
        if not isinstance(category, str) or not CATEGORY_RE.match(category):
            problems.append(f'params.category must be a BuiltInCategory OST_* name such as "OST_Walls", '
                            f"got {category!r}")
        normal["category"] = category
    if template == "count_delta" and "expected" in params:
        expected = _integer(params["expected"])
        if expected is None:
            problems.append(f"params.expected must be an integer (negative for removals), "
                            f"got {params['expected']!r}")
        normal["expected"] = expected
    if template == "param_equals" and "checks" in params:
        normal["checks"], more = _check_checks(params["checks"])
        problems += more
    return (None, problems) if problems else (normal, [])


def validator_call(template: str, params: dict) -> tuple[Any, TaskSpec, dict, dict]:
    """``(validator, spec, cfg, keywords)`` for ``validator.after(client, spec, cfg, before,
    result, **keywords)``; ``params`` is the output of ``check_template``."""
    validator = get_validator(template)
    cfg: dict[str, Any] = {"kind": template, "category": params["category"]}
    bindings: list[ParamBinding] = []
    keywords: dict[str, Any] = {}
    if template == "count_delta":
        cfg["expected"] = params["expected"]
    if template == "param_equals":
        cfg["checks"] = []
        tolerances: dict[str, float] = {}
        for index, check in enumerate(params["checks"]):
            name = f"check_{index}"
            cfg["checks"].append({"param_name": check["param"], "spec_param": name})
            bindings.append(ParamBinding(name=name, value=check["expected"], unit=check.get("unit"),
                                         source=Source.answer, evidence=SPEC_EVIDENCE))
            if check.get("tolerance") is not None:
                tolerances[name] = float(check["tolerance"])
        keywords["tolerances"] = tolerances
    spec = TaskSpec(task=f"verify_execution {template}", action=Action(kind="execute_code"), parameters=bindings)
    return validator, spec, cfg, keywords
