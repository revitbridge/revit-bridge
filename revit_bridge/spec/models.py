"""
Pydantic v2 data models - the TaskSpec building blocks.

The 0.1 slot / question / violation / step models are kept for the web host
until phase 6 confirms nothing imports them. The 0.2 TaskSpec family
(Source, ParamBinding, Interpretation, Action, WorkflowState, TaskSpec) is
what confirm_spec / reconcile / the gate work on.
"""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SlotStatus(str, Enum):
    empty = "empty"
    filled = "filled"
    defaulted = "defaulted"
    inferred = "inferred"


class SlotSource(str, Enum):
    user_input = "user_input"
    default = "default"
    inferred = "inferred"
    follow_up = "follow_up"
    not_provided = "not_provided"


# ---------------------------------------------------------------------------
# Core state models
# ---------------------------------------------------------------------------

class SlotState(BaseModel):
    name: str
    value: Any = None
    status: SlotStatus = SlotStatus.empty
    source: SlotSource = SlotSource.not_provided
    display: str = ""

    def fill(self, value: Any, source: SlotSource = SlotSource.user_input, display: str = ""):
        self.value = value
        self.status = SlotStatus.filled
        self.source = source
        self.display = display or str(value)

    def set_default(self, value: Any, display: str = ""):
        self.value = value
        self.status = SlotStatus.defaulted
        self.source = SlotSource.default
        self.display = display or f"{value} (default)"

    def set_inferred(self, value: Any, display: str = ""):
        self.value = value
        self.status = SlotStatus.inferred
        self.source = SlotSource.inferred
        self.display = display or f"{value} (inferred)"


# ---------------------------------------------------------------------------
# Question queue item - for step-by-step wizard
# ---------------------------------------------------------------------------

class QuestionItem(BaseModel):
    """One question in the wizard queue."""
    slot: str
    text: str
    options: list[str] = Field(default_factory=list)     # display labels
    values: list[Any] = Field(default_factory=list)       # actual values to fill
    allow_custom: bool = False                             # allow free-text input
    enrich: str = "none"                                   # enrichment tag: none|level|host_pick|family_type:<cat>


class ActionStep(BaseModel):
    """One step in a multi-action plan."""
    step: int = 1
    intent: str = ""
    display_name: str = ""
    api_method: str = ""
    description: str = ""
    slots: dict[str, Any] = Field(default_factory=dict)
    questions: list[QuestionItem] = Field(default_factory=list)
    completed: bool = False
    filled_slots: dict[str, SlotState] = Field(default_factory=dict)


class ConstraintViolation(BaseModel):
    slot: str
    message: str
    current_value: Any = None


# ---------------------------------------------------------------------------
# TaskSpec (0.2): every parameter carries its provenance; the projection is
# what gets executed and what a confirmation token is bound to.
# ---------------------------------------------------------------------------

class Source(str, Enum):
    designer = "designer"          # the designer's own words
    tool = "tool"                  # a tool result; evidence names it, e.g. "tool:get_tool_choices"
    answer = "answer"              # an answer to a question; evidence is the question id
    preference = "preference"      # standards/personal; evidence "preference:<name>"
    default = "default"            # a default the capability pack declares; evidence "default:<tool>"


class ParamBinding(BaseModel):
    name: str
    value: Any
    display: str = ""
    unit: str | None = None        # "mm" | "m" | "feet" | None
    source: Source
    evidence: str                  # never empty; see Source


class Interpretation(BaseModel):
    """A reading the model made that the designer must see and confirm."""
    param: str | None
    text: str                      # e.g. "3000 按 mm 理解" / "'F2 上' 理解为底部约束为 F2"
    confirmed: bool = False


class Action(BaseModel):
    kind: Literal["run_tool", "execute_code"]
    tool: str | None = None        # run_tool
    code: str | None = None        # execute_code
    code_parameters: list | None = None


class WorkflowState(BaseModel):
    name: str
    stage: str
    confirmed: dict[str, Any] = Field(default_factory=dict)
    pending: list[str] = Field(default_factory=list)


class TaskSpec(BaseModel):
    spec_version: Literal[1] = 1
    task: str                      # one sentence
    action: Action
    parameters: list[ParamBinding]
    interpretations: list[Interpretation] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)   # for display
    snapshot_fingerprint: str | None                 # the snapshot the spec was reconciled against
    workflow: WorkflowState | None = None
    language: str = "zh"

    def execution_projection(self) -> dict:
        """What will actually be executed - the part a confirmation binds to."""
        if self.action.kind == "run_tool":
            return {
                "kind": "run_tool",
                "tool": self.action.tool,
                "params": {p.name: p.value for p in self.parameters},
            }
        return {
            "kind": "execute_code",
            "code": self.action.code,
            "parameters": list(self.action.code_parameters or []),
        }

    def canonical_json(self) -> str:
        return canonical_json(self.model_dump(mode="json"))

    def spec_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def card(self) -> str:
        """The spec card as plain text, in the shape the skill shows the designer."""
        lines = [f"Task: {self.task}"]
        if self.action.kind == "run_tool":
            lines.append(f"Tool: {self.action.tool} (run_tool)")
        else:
            code_lines = len((self.action.code or "").splitlines())
            lines.append(f"Code: execute_code ({code_lines} lines)")
        lines.append("Parameters:" if self.parameters else "Parameters: none")
        width = max((len(p.name) for p in self.parameters), default=0)
        for p in self.parameters:
            shown = p.display or _show(p.value)
            if p.unit and p.unit not in shown:
                shown = f"{shown} {p.unit}"
            source = p.source.value if p.evidence == p.source.value else f"{p.source.value} ({p.evidence})"
            lines.append(f"  {p.name.ljust(width)} = {shown.ljust(28)} source: {source}")
        if self.interpretations:
            lines.append("Interpretations:")
            for it in self.interpretations:
                mark = "[x]" if it.confirmed else "[ ]"
                lines.append(f"  {mark} {it.text}")
        if self.steps:
            lines.append("Steps: " + "  ".join(f"{i} {s}" for i, s in enumerate(self.steps, 1)))
        lines.append("Confirm? (yes / change something)")
        return "\n".join(lines)


def canonical_json(data) -> str:
    """Stable JSON: sorted keys, no spaces, unicode kept, integral floats as ints."""
    return json.dumps(_normalize(data), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def projection_hash(projection: dict) -> str:
    """sha256 of the canonical execution projection."""
    return hashlib.sha256(canonical_json(projection).encode("utf-8")).hexdigest()


def _normalize(value):
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)   # 3000.0 and 3000 are the same value to Revit and to the designer
    if isinstance(value, Enum):
        return value.value
    return value


def _show(value) -> str:
    if isinstance(value, str):
        return f'"{value}"'
    return json.dumps(value, ensure_ascii=False)
