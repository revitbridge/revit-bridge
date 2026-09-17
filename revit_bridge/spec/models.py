"""
Pydantic v2 data models - the TaskSpec building blocks.

Migrated from the former intent-bridge runtime: only the slot / question /
violation / step models are kept. Session and API request models stayed with
the web host. The TaskSpec itself (per-parameter provenance, reconciliation,
gate) is defined by planning in a later phase and will build on these.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

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
