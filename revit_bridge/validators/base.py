"""Post-execution validators: completion is declared by an assertion, not by ``success: true``.

A validator samples the model before the execution (``before``) and asserts
afterwards (``after``); the report goes into the result and the evidence
ledger. ``{param}`` references in a validator's configuration resolve to the
spec's parameter values.
"""
from __future__ import annotations

import re
from typing import Any, Protocol

from pydantic import BaseModel, Field

from revit_bridge.spec.models import TaskSpec

PARAM_REF_RE = re.compile(r"^\{([A-Za-z_]\w*)\}$")
ID_FIELDS = ("ElementId", "Ids", "ElementIds", "ids")   # spec 8; "ids" is the ledger summary


class Check(BaseModel):
    name: str
    passed: bool
    detail: str


class ValidationReport(BaseModel):
    validator: str
    passed: bool
    checks: list[Check]
    before: dict = Field(default_factory=dict)
    after: dict = Field(default_factory=dict)


class Validator(Protocol):
    kind: str

    async def before(self, client, spec: TaskSpec, cfg: dict) -> dict: ...

    async def after(self, client, spec: TaskSpec, cfg: dict, before: dict, result) -> ValidationReport: ...


class ValidatorError(RuntimeError):
    """The validator could not run (bad configuration, Revit refused the probe)."""


def spec_values(spec: TaskSpec) -> dict[str, Any]:
    return {p.name: p.value for p in spec.parameters}


def resolve(value: Any, spec: TaskSpec) -> Any:
    """``"{param}"`` -> the spec's value for that parameter; anything else as is."""
    if isinstance(value, str):
        m = PARAM_REF_RE.match(value)
        if m:
            values = spec_values(spec)
            if m.group(1) not in values:
                raise ValidatorError(f"validator refers to {value}, which the spec does not bind")
            return values[m.group(1)]
    return value


def extract_ids(result: Any) -> list[int]:
    """Every id under an ``ElementId`` / ``Ids`` / ``ElementIds`` field, in order, no repeats."""
    found: list[int] = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ID_FIELDS:
                    for raw in (value if isinstance(value, list) else [value]):
                        if isinstance(raw, bool):
                            continue
                        if isinstance(raw, int) or (isinstance(raw, str) and raw.lstrip("-").isdigit()):
                            number = int(raw)
                            if number > 0 and number not in found:
                                found.append(number)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(result)
    return found


def failed_report(kind: str, detail: str, before: dict | None = None) -> ValidationReport:
    return ValidationReport(validator=kind, passed=False,
                            checks=[Check(name="validator", passed=False, detail=detail)],
                            before=before or {})
