"""The three built-in validators (spec 8): created_ids, count_delta, param_equals.

Each one runs one read-only C# probe. ``category`` and ``expected`` may be
written as ``"{param}"`` and then take the spec's value for that parameter.
"""
from __future__ import annotations

import re

from revit_bridge.spec.models import TaskSpec
from revit_bridge.validators.base import (
    Check,
    ValidationReport,
    ValidatorError,
    extract_ids,
    failed_report,
    resolve,
)

CATEGORY_RE = re.compile(r"^OST_[A-Za-z]+$")
LENGTH_TOLERANCE_MM = 0.5


def _category(cfg: dict, spec: TaskSpec) -> str:
    category = resolve(cfg.get("category"), spec)
    if not isinstance(category, str) or not CATEGORY_RE.match(category):
        raise ValidatorError(f"validator category must be an OST_* name, got {category!r}")
    return category


async def _probe(client, code: str):
    resp = await client.send_code(code)
    if not resp.success:
        raise ValidatorError(resp.error or "Revit returned no result")
    return resp.result


def _ids_literal(ids: list[int]) -> str:
    return ", ".join(f"{i}L" for i in ids)


# -- created_ids ---------------------------------------------------------------------------

class CreatedIds:
    """Every id the result reports exists and belongs to the configured category."""

    kind = "created_ids"

    async def before(self, client, spec: TaskSpec, cfg: dict) -> dict:
        return {}

    async def after(self, client, spec: TaskSpec, cfg: dict, before: dict, result) -> ValidationReport:
        category = _category(cfg, spec)
        ids = extract_ids(result)
        if not ids:
            return failed_report(self.kind, "the result reports no ElementId / Ids / ElementIds", before)
        code = (
            f'var bic = (BuiltInCategory)Enum.Parse(typeof(BuiltInCategory), "{category}");\n'
            f'var rows = new List<object>();\n'
            f'foreach (var raw in new long[] {{ {_ids_literal(ids)} }}) {{\n'
            f'    var e = document.GetElement(new ElementId(raw));\n'
            f'    bool exists = e != null;\n'
            f'    string cat = exists && e.Category != null ? e.Category.Name : null;\n'
            f'    bool matches = exists && e.Category != null\n'
            f'        && (BuiltInCategory)(int)e.Category.Id.Value == bic;\n'
            f'    rows.Add(new {{ Id = raw, Exists = exists, Category = cat, Matches = matches }});\n'
            f'}}\n'
            f'return rows;'
        )
        rows = await _probe(client, code)
        rows = rows if isinstance(rows, list) else [rows]
        checks = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            exists, matches = bool(row.get("Exists")), bool(row.get("Matches"))
            detail = (f"id {row.get('Id')} exists, category {row.get('Category')!r}" if exists
                      else f"id {row.get('Id')} does not exist")
            if exists and not matches:
                detail += f" (expected {category})"
            checks.append(Check(name=f"id {row.get('Id')}", passed=exists and matches, detail=detail))
        passed = bool(checks) and all(c.passed for c in checks)
        return ValidationReport(validator=self.kind, passed=passed, checks=checks, before=before,
                                after={"ids": ids, "category": category})


# -- count_delta -----------------------------------------------------------------------------

class CountDelta:
    """The number of instances of a category moved by exactly ``expected``."""

    kind = "count_delta"

    @staticmethod
    def _count_code(category: str) -> str:
        return (
            f'var bic = (BuiltInCategory)Enum.Parse(typeof(BuiltInCategory), "{category}");\n'
            f'return new FilteredElementCollector(document).OfCategory(bic)\n'
            f'    .WhereElementIsNotElementType().GetElementCount();'
        )

    async def _count(self, client, category: str) -> int:
        value = await _probe(client, self._count_code(category))
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValidatorError(f"count probe returned {value!r}") from exc

    async def before(self, client, spec: TaskSpec, cfg: dict) -> dict:
        category = _category(cfg, spec)
        return {"category": category, "count": await self._count(client, category)}

    async def after(self, client, spec: TaskSpec, cfg: dict, before: dict, result) -> ValidationReport:
        category = _category(cfg, spec)
        expected = resolve(cfg.get("expected"), spec)
        try:
            expected = int(expected)
        except (TypeError, ValueError) as exc:
            raise ValidatorError(f"count_delta expected must be an int, got {expected!r}") from exc
        if "count" not in before:
            return failed_report(self.kind, "no count was sampled before the execution", before)
        after_count = await self._count(client, category)
        delta = after_count - int(before["count"])
        detail = f"{category}: before {before['count']}, after {after_count}, delta {delta}, expected {expected}"
        return ValidationReport(
            validator=self.kind, passed=delta == expected,
            checks=[Check(name="count_delta", passed=delta == expected, detail=detail)],
            before=before, after={"category": category, "count": after_count, "delta": delta},
        )


# -- param_equals ------------------------------------------------------------------------------

class ParamEquals:
    """Parameters read back from the result's elements equal the spec's values."""

    kind = "param_equals"

    async def before(self, client, spec: TaskSpec, cfg: dict) -> dict:
        return {}

    async def after(self, client, spec: TaskSpec, cfg: dict, before: dict, result) -> ValidationReport:
        category = _category(cfg, spec)
        ids = extract_ids(result)
        if not ids:
            return failed_report(self.kind, "the result reports no ElementId / Ids / ElementIds", before)
        checks_cfg = cfg.get("checks") or []
        values = {p.name: p.value for p in spec.parameters}
        wanted = []
        for check in checks_cfg:
            name, spec_param = str(check.get("param_name", "")), str(check.get("spec_param", ""))
            if spec_param not in values:
                raise ValidatorError(f"param_equals: spec has no parameter {spec_param!r}")
            wanted.append((name, spec_param))
        names = ", ".join(f'"{name}"' for name, _ in wanted)
        code = (
            f'var rows = new List<object>();\n'
            f'foreach (var raw in new long[] {{ {_ids_literal(ids)} }}) {{\n'
            f'    var e = document.GetElement(new ElementId(raw));\n'
            f'    foreach (var name in new string[] {{ {names} }}) {{\n'
            f'        if (e == null) {{ rows.Add(new {{ Id = raw, Name = name, Kind = "missing", Value = (object)null }}); continue; }}\n'
            f'        Parameter p = null;\n'
            f'        try {{\n'
            f'            BuiltInParameter bip;\n'
            f'            if (Enum.TryParse(name, out bip)) p = e.get_Parameter(bip);\n'
            f'            else p = e.LookupParameter(name);\n'
            f'        }} catch (Exception) {{ p = null; }}\n'
            f'        if (p == null) {{ rows.Add(new {{ Id = raw, Name = name, Kind = "none", Value = (object)null }}); continue; }}\n'
            f'        string kind = "string"; object value = null;\n'
            f'        switch (p.StorageType) {{\n'
            f'            case StorageType.Double: {{\n'
            f'                bool isLength = SpecTypeId.Length.Equals(p.Definition.GetDataType());\n'
            f'                kind = isLength ? "length_mm" : "double";\n'
            f'                value = isLength ? p.AsDouble() * 304.8 : p.AsDouble();\n'
            f'                break;\n'
            f'            }}\n'
            f'            case StorageType.Integer: kind = "int"; value = p.AsInteger(); break;\n'
            f'            case StorageType.ElementId: kind = "elementid"; value = p.AsElementId().Value; break;\n'
            f'            default: kind = "string"; value = p.AsString(); break;\n'
            f'        }}\n'
            f'        rows.Add(new {{ Id = raw, Name = name, Kind = kind, Value = value }});\n'
            f'    }}\n'
            f'}}\n'
            f'return rows;'
        )
        rows = await _probe(client, code)
        rows = rows if isinstance(rows, list) else [rows]
        by_param = {name: values[spec_param] for name, spec_param in wanted}
        checks = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("Name"))
            expected = by_param.get(name)
            passed, detail = _compare(row, expected)
            checks.append(Check(name=f"id {row.get('Id')} {name}", passed=passed, detail=detail))
        passed = bool(checks) and all(c.passed for c in checks)
        return ValidationReport(validator=self.kind, passed=passed, checks=checks, before=before,
                                after={"ids": ids, "category": category})


def _compare(row: dict, expected) -> tuple[bool, str]:
    kind, actual = row.get("Kind"), row.get("Value")
    if kind == "missing":
        return False, "element does not exist"
    if kind == "none":
        return False, "parameter not found on the element"
    if kind in ("length_mm", "double", "int", "elementid"):
        try:
            got, want = float(actual), float(expected)
        except (TypeError, ValueError):
            return False, f"got {actual!r}, expected {expected!r}"
        tolerance = LENGTH_TOLERANCE_MM if kind == "length_mm" else 1e-6
        unit = " mm" if kind == "length_mm" else ""
        return abs(got - want) <= tolerance, f"got {got:g}{unit}, expected {want:g}{unit}"
    return str(actual) == str(expected), f"got {actual!r}, expected {expected!r}"


BUILTIN_VALIDATORS = {v.kind: v for v in (CreatedIds(), CountDelta(), ParamEquals())}


def get_validator(kind: str):
    """The validator for ``kind``; raises ValidatorError for an unknown kind."""
    try:
        return BUILTIN_VALIDATORS[kind]
    except KeyError as exc:
        raise ValidatorError(f"unknown validator kind {kind!r}") from exc
