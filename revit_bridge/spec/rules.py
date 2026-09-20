"""Pure rules over a TaskSpec: provenance validation, missing parameters,
reconciliation against a snapshot.

Nothing here talks to Revit or to disk. ``validate_spec`` is what
``confirm_spec`` runs before it issues a token; ``missing_params`` turns the
unbound required parameters of a pack into questions the designer can answer;
``reconcile`` compares a draft spec with a ``ProjectSnapshot`` and says what
still stands between the draft and a confirmable spec.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from revit_bridge.capabilities.store import SolidifiedTool
from revit_bridge.revit import sandbox
from revit_bridge.snapshot.project import ProjectSnapshot
from revit_bridge.spec.models import Interpretation, Source, TaskSpec

NUMERIC_TYPES = ("double", "number", "float", "int", "integer")
PREFERENCE_RE = re.compile(r"^preference:[A-Za-z0-9_.\-]+$")

# Words that scope a request to a level ("在 F2", "F2 上", "整层 F2", "所有 F2 ...")
RANGE_BEFORE = ("在", "整层", "所有")
RANGE_AFTER = ("上", "整层")


# -- results ------------------------------------------------------------------------

class SpecError(BaseModel):
    code: str
    param: str | None = None
    message: str


class Question(BaseModel):
    id: str                          # f"q_{param}"
    param: str
    text: str
    why: str
    options: list[dict] = Field(default_factory=list)   # [{label, value, source}], source "tool:<kind>"
    allow_other: bool = True


class Conflict(BaseModel):
    param: str
    claimed: Any
    kind: Literal["not_found", "ambiguous", "unit_missing", "stale_snapshot"]
    available: list[Any] = Field(default_factory=list)
    message: str


class ReconcileResult(BaseModel):
    conflicts: list[Conflict]
    questions: list[Question]
    interpretations_required: list[Interpretation]
    ready: bool                      # no conflicts, no questions, no unconfirmed interpretation


# -- text templates (zh / en) ------------------------------------------------------------

_T = {
    "zh": {
        "ask": "请提供 {param}（{description}）。",
        "choose": "请选择 {param}（{description}）：",
        "why": "{tool} 的必填参数，来源须为 {source}。",
        "unit": "{param} 未标单位，按 {unit} 理解，请确认",
        "range": "'{phrase}' 理解为标高 {level}（在该层上操作），请确认",
        "unit_missing": "{param} = {value} 未标单位；{tool} 按 {unit} 计",
        "not_found": "{param} = {claimed!r} 在快照中不存在",
        "did_you_mean": "{param} = {claimed!r} 在快照中不存在；是否指 {hint!r}？",
        "ambiguous": "{param} = {claimed!r} 匹配多个候选：{matches}",
        "truncated": "{param} = {claimed!r} 不在快照列出的前 {shown} 个名字里（该类别共 {count} 个，名单被截断）；"
                     "请用 query(\"family_types\", {{\"categories\": [\"{category}\"]}}) 核对",
        "stale": "规格依据的快照 {claimed} 与当前快照 {current} 不符，请重新对账",
        "tool_missing": "工具 {tool} 不存在",
    },
    "en": {
        "ask": "Please provide {param} ({description}).",
        "choose": "Which {param} ({description})?",
        "why": "Required by {tool}; its source must be {source}.",
        "unit": "{param} has no unit; reading it as {unit}, please confirm",
        "range": "'{phrase}' is read as level {level} (work on that level); please confirm",
        "unit_missing": "{param} = {value} has no unit; {tool} counts in {unit}",
        "not_found": "{param} = {claimed!r} does not exist in the snapshot",
        "did_you_mean": "{param} = {claimed!r} does not exist in the snapshot; did you mean {hint!r}?",
        "ambiguous": "{param} = {claimed!r} matches several candidates: {matches}",
        "truncated": "{param} = {claimed!r} is not among the first {shown} names the snapshot lists (the category "
                     "has {count}; the list is truncated); verify with "
                     "query(\"family_types\", {{\"categories\": [\"{category}\"]}})",
        "stale": "the spec was reconciled against snapshot {claimed}, the current one is {current}; reconcile again",
        "tool_missing": "tool {tool} does not exist",
    },
}


def _t(language: str, key: str, **kw) -> str:
    table = _T.get((language or "zh").split("-")[0].lower(), _T["zh"])
    return table[key].format(**kw)


# -- helpers ----------------------------------------------------------------------------

def _pack_params(pack: SolidifiedTool | None) -> dict[str, dict]:
    return {p["name"]: p for p in (pack.parameters if pack else [])}


def _is_tool_sourced(pdef: dict) -> bool:
    return bool(pdef.get("choices_from")) or str(pdef.get("source", "")).startswith("tool:")


def _is_numeric(pdef: dict) -> bool:
    return str(pdef.get("type", "string")).lower() in NUMERIC_TYPES


def _confirmed_for(spec: TaskSpec, param: str | None) -> bool:
    return any(it.param == param and it.confirmed for it in spec.interpretations)


def implied_interpretations(spec: TaskSpec, pack: SolidifiedTool | None) -> list[Interpretation]:
    """Readings the spec silently makes: a numeric value without a unit for a
    pack parameter that declares one ("3000 按 mm 理解")."""
    implied: list[Interpretation] = []
    params = _pack_params(pack)
    for binding in spec.parameters:
        pdef = params.get(binding.name)
        if not pdef or not _is_numeric(pdef) or not pdef.get("unit") or binding.unit:
            continue
        implied.append(Interpretation(
            param=binding.name,
            text=_t(spec.language, "unit", param=binding.name, unit=pdef["unit"]),
            confirmed=_confirmed_for(spec, binding.name),
        ))
    return implied


# -- 5.1 validation -----------------------------------------------------------------------

def validate_spec(spec: TaskSpec, pack: SolidifiedTool | None) -> list[SpecError]:
    """Every rule of spec 5.1; an empty list means the spec may be confirmed."""
    errors: list[SpecError] = []
    params = _pack_params(pack)
    bound = {b.name: b for b in spec.parameters}

    if spec.action.kind == "run_tool":
        if pack is None:
            errors.append(SpecError(code="missing_param", param=None,
                                    message=f"tool {spec.action.tool!r} not found; run_tool needs a capability pack"))
        else:
            for name, pdef in params.items():
                if pdef.get("required", "default" not in pdef) and name not in bound:
                    errors.append(SpecError(code="missing_param", param=name,
                                            message=f"required parameter {name!r} of {pack.name} is not bound"))

    for b in spec.parameters:
        if not b.evidence or not b.evidence.strip():
            errors.append(SpecError(code="no_evidence", param=b.name,
                                    message=f"{b.name}: evidence is empty (source {b.source.value})"))
        pdef = params.get(b.name)
        if pdef is not None:
            if _is_tool_sourced(pdef) and b.source not in (Source.tool, Source.answer):
                errors.append(SpecError(code="unsourced_choice", param=b.name,
                                        message=f"{b.name} must come from a Revit query or the designer's answer "
                                                f"(pack: {pdef.get('choices_from') or pdef.get('source')}), "
                                                f"not {b.source.value}"))
            elif str(pdef.get("source", "")) == "designer" and b.source not in (
                    Source.designer, Source.answer, Source.preference):
                # A preference is shown on the card and still confirmed (spec 5.1, 2026-09-20 amendment)
                errors.append(SpecError(code="guessed_value", param=b.name,
                                        message=f"{b.name} must be the designer's words, answer or a "
                                                f"preference, not {b.source.value}"))
        if b.source is Source.default and (pdef is None or "default" not in pdef):
            errors.append(SpecError(code="default_not_declared", param=b.name,
                                    message=f"{b.name}: the pack declares no default for it"))
        if b.source is Source.preference and not PREFERENCE_RE.match(b.evidence or ""):
            errors.append(SpecError(code="bad_preference_ref", param=b.name,
                                    message=f"{b.name}: evidence must be preference:<name>, got {b.evidence!r}"))

    for it in implied_interpretations(spec, pack):
        if not it.confirmed:
            errors.append(SpecError(code="unconfirmed_interpretation", param=it.param, message=it.text))
    for it in spec.interpretations:
        if not it.confirmed:
            errors.append(SpecError(code="unconfirmed_interpretation", param=it.param,
                                    message=f"not confirmed: {it.text}"))

    if spec.action.kind == "execute_code":
        code = spec.action.code or ""
        if not code.strip():
            errors.append(SpecError(code="blocked_code", param=None, message="execute_code without code"))
        else:
            safe, warnings = sandbox.review(code)
            if not safe:
                errors.append(SpecError(code="blocked_code", param=None, message="; ".join(warnings)))
    return errors


# -- 5.2 missing parameters -----------------------------------------------------------------

def _options_for(pdef: dict, snapshot: ProjectSnapshot | None) -> list[dict]:
    """Real choices from the snapshot for a choices_from parameter, or none."""
    if snapshot is None:
        return []
    source = str(pdef.get("choices_from") or "")
    if source == "levels":
        return [{"label": f"{lv.name} ({lv.elevation_mm}mm)", "value": lv.name, "source": "tool:levels"}
                for lv in snapshot.levels]
    if source.startswith("family_types:"):
        category = source.split(":", 1)[1]
        for summary in snapshot.family_types:
            if summary.category == category:
                return [{"label": n, "value": n, "source": "tool:family_types"} for n in summary.names]
    return []


def missing_params(pack: SolidifiedTool, known: dict[str, Any],
                   snapshot: ProjectSnapshot | None, language: str = "zh") -> list[Question]:
    """One question per required pack parameter that ``known`` does not bind."""
    questions: list[Question] = []
    for pdef in pack.parameters:
        name = pdef["name"]
        if name in known or not pdef.get("required", "default" not in pdef):
            continue
        options = _options_for(pdef, snapshot)
        description = str(pdef.get("description") or pdef.get("type", "string"))
        source = str(pdef.get("choices_from") and f"tool:{pdef['choices_from']}" or pdef.get("source", "designer"))
        questions.append(Question(
            id=f"q_{name}",
            param=name,
            text=_t(language, "choose" if options else "ask", param=name, description=description),
            why=_t(language, "why", tool=pack.name, source=source),
            options=options,
        ))
    return questions


# -- 5.2 reconciliation ---------------------------------------------------------------------

def _fold(text: Any) -> str:
    return re.sub(r"\s+", "", str(text)).casefold()


def _match(claimed: Any, candidates: list[str]) -> tuple[bool, list[str]]:
    """(exact hit, fuzzy hits) - fuzzy ignores case and whitespace, for hints only."""
    if claimed in candidates:
        return True, []
    key = _fold(claimed)
    return False, [c for c in candidates if _fold(c) == key]


class _Candidates:
    """What the snapshot knows for one choices_from source."""

    def __init__(self, names: list[str], total: int | None = None, category: str = ""):
        self.names = names
        self.total = len(names) if total is None else total
        self.category = category

    @property
    def truncated(self) -> bool:
        return self.total > len(self.names)


def _candidates(pdef: dict, snapshot: ProjectSnapshot) -> _Candidates | None:
    source = str(pdef.get("choices_from") or "")
    if source == "levels":
        return _Candidates([lv.name for lv in snapshot.levels])
    if source.startswith("family_types:"):
        category = source.split(":", 1)[1]
        for summary in snapshot.family_types:
            if summary.category == category:
                return _Candidates(list(summary.names), summary.count, category)
        return None   # category not in this snapshot: nothing to check against
    return None


# The level name must end at a boundary so "F2" does not match inside "F20".
# ASCII letters/digits only: a CJK character right after the name ("F2上") is
# not part of the name.
_NAME_END = r"(?![0-9A-Za-z_])"


# Snapshot warnings that mean a candidate list is missing rather than empty.
_LEVELS_FAILED = ("levels:", "snapshot code failed", "snapshot: unparseable")
_TYPES_FAILED = ("family_types:",)


def _unreliable(pdef: dict, snapshot: ProjectSnapshot) -> str | None:
    """The snapshot warning that makes this parameter's candidates unusable, if any."""
    source = str(pdef.get("choices_from") or "")
    if source == "levels":
        prefixes = _LEVELS_FAILED
    elif source.startswith("family_types:"):
        prefixes = _TYPES_FAILED + (f"family_types {source.split(':', 1)[1]}:",)
    else:
        return None
    for warning in snapshot.warnings:
        if warning.startswith(prefixes):
            return warning
    return None


def range_interpretations(spec: TaskSpec, snapshot: ProjectSnapshot) -> list[Interpretation]:
    """Level names scoped by a range word in the task text but bound nowhere.

    Longer level names are tried first and the text they matched is
    consumed, so "F20" is never also read as "F2".
    """
    found: list[Interpretation] = []
    bound_values = {_fold(b.value) for b in spec.parameters} | {_fold(b.display) for b in spec.parameters}
    before = "|".join(map(re.escape, RANGE_BEFORE))
    after = "|".join(map(re.escape, RANGE_AFTER))
    task = spec.task
    for lv in sorted(snapshot.levels, key=lambda l: len(l.name), reverse=True):
        if not lv.name or _fold(lv.name) in bound_values:
            continue
        name = re.escape(lv.name) + _NAME_END
        pattern = rf"(?:(?:{before})\s*{name}(?:\s*(?:{after}))?)|(?:{name}\s*(?:{after}))"
        m = re.search(pattern, task)
        if not m:
            continue
        phrase = m.group(0)
        task = task[:m.start()] + " " * len(phrase) + task[m.end():]
        text = _t(spec.language, "range", phrase=phrase, level=lv.name)
        found.append(Interpretation(param=None, text=text, confirmed=_confirmed_text(spec, text)))
    return found


def _confirmed_text(spec: TaskSpec, text: str) -> bool:
    return any(it.text == text and it.confirmed for it in spec.interpretations)


def reconcile(draft: TaskSpec, snapshot: ProjectSnapshot,
              pack: SolidifiedTool | None = None) -> ReconcileResult:
    """Compare the draft with the snapshot: conflicts, open questions, readings to confirm."""
    lang = draft.language
    conflicts: list[Conflict] = []
    questions: list[Question] = []
    required: list[Interpretation] = []

    if draft.snapshot_fingerprint and draft.snapshot_fingerprint != snapshot.fingerprint:
        conflicts.append(Conflict(
            param="snapshot_fingerprint", claimed=draft.snapshot_fingerprint, kind="stale_snapshot",
            available=[snapshot.fingerprint],
            message=_t(lang, "stale", claimed=draft.snapshot_fingerprint, current=snapshot.fingerprint),
        ))

    params = _pack_params(pack)
    if draft.action.kind == "run_tool" and pack is None:
        conflicts.append(Conflict(param="tool", claimed=draft.action.tool, kind="not_found",
                                  message=_t(lang, "tool_missing", tool=draft.action.tool)))

    for b in draft.parameters:
        pdef = params.get(b.name)
        if pdef is None:
            continue
        failed = _unreliable(pdef, snapshot)
        if failed is not None:
            # The snapshot could not read this part: say so, never "not found"
            conflicts.append(Conflict(param=b.name, claimed=b.value, kind="stale_snapshot", message=failed))
            known = None
        else:
            known = _candidates(pdef, snapshot)
        if known is not None:
            candidates = known.names
            exact, fuzzy = _match(b.value, candidates)
            if not exact and known.truncated:
                # The snapshot lists at most MAX_NAMES names: absence proves nothing
                conflicts.append(Conflict(
                    param=b.name, claimed=b.value, kind="ambiguous", available=list(candidates),
                    message=_t(lang, "truncated", param=b.name, claimed=b.value, shown=len(candidates),
                               count=known.total, category=known.category)))
            elif not exact:
                if len(fuzzy) > 1:
                    conflicts.append(Conflict(
                        param=b.name, claimed=b.value, kind="ambiguous", available=fuzzy,
                        message=_t(lang, "ambiguous", param=b.name, claimed=b.value, matches=fuzzy)))
                elif fuzzy:
                    conflicts.append(Conflict(
                        param=b.name, claimed=b.value, kind="not_found", available=fuzzy + [c for c in candidates if c not in fuzzy],
                        message=_t(lang, "did_you_mean", param=b.name, claimed=b.value, hint=fuzzy[0])))
                else:
                    conflicts.append(Conflict(
                        param=b.name, claimed=b.value, kind="not_found", available=list(candidates),
                        message=_t(lang, "not_found", param=b.name, claimed=b.value)))
        if _is_numeric(pdef) and pdef.get("unit") and not b.unit and not _confirmed_for(draft, b.name):
            conflicts.append(Conflict(
                param=b.name, claimed=b.value, kind="unit_missing", available=[pdef["unit"]],
                message=_t(lang, "unit_missing", param=b.name, value=b.value,
                           tool=pack.name if pack else "", unit=pdef["unit"])))

    if pack is not None:
        questions = missing_params(pack, {b.name: b.value for b in draft.parameters}, snapshot, lang)

    required.extend(it for it in implied_interpretations(draft, pack) if not it.confirmed)
    required.extend(it for it in range_interpretations(draft, snapshot) if not it.confirmed)
    unconfirmed_in_draft = any(not it.confirmed for it in draft.interpretations)

    return ReconcileResult(
        conflicts=conflicts,
        questions=questions,
        interpretations_required=required,
        ready=not conflicts and not questions and not required and not unconfirmed_in_draft,
    )
