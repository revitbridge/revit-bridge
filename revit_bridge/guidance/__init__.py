"""Guidance: lessons for writing Revit code, chosen and ordered without a model.

Content format v1 lives in ``plugin/guidance/`` (``revit_bridge/guidance_content``
inside the wheel; ``paths.guidance_dir()`` finds it): ``guidance.yaml`` plus
one directory per category with a ``SKILL.md`` and ``lessons/<slug>.md``.

    load(root)                         validate a root -> Library (GuidanceError lists every problem)
    recommend(library, stage, ...)     choose, order and budget lessons for a stage
    index(library) / read(library, ids)
    respond(library, mode, ...)        the get_guidance tool reply
    render_index(library)              the revit://guidance/index text

Only the framework layer is loaded in this version. No model, no network,
no environment variables.
"""
from __future__ import annotations

from revit_bridge.guidance.loader import GuidanceError, Library, load
from revit_bridge.guidance.model import (
    CATEGORIES,
    KINDS,
    LAYER_FRAMEWORK,
    SCHEMA_VERSION,
    STAGES,
    Budget,
    Category,
    Config,
    Lesson,
    Triggers,
    VersionRange,
    parse_version,
)
from revit_bridge.guidance.select import index, read, recommend, render_index, respond

__all__ = [
    "CATEGORIES", "KINDS", "LAYER_FRAMEWORK", "SCHEMA_VERSION", "STAGES",
    "Budget", "Category", "Config", "GuidanceError", "Lesson", "Library", "Triggers", "VersionRange",
    "index", "load", "parse_version", "read", "recommend", "render_index", "respond",
]
