"""
Pydantic v2 data models: the TaskSpec family (Source, ParamBinding,
Interpretation, Action, WorkflowState, TaskSpec) that confirm_spec,
reconcile and the gate work on. The 0.1 slot / question / step models were
removed in 0.2.1 (no host imported them).
"""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


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
    param: str | None = None       # None: about the task as a whole (a range word)
    text: str                      # e.g. "3000 read as mm", "'on F2' read as base level F2"
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
    snapshot_fingerprint: str | None = None          # the snapshot the spec was reconciled against
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
