"""Guidance: lessons for writing Revit code, chosen and ordered without a model.

Content format v1 lives in ``plugin/guidance/`` (``revit_bridge/guidance_content``
inside the wheel; ``paths.guidance_dir()`` finds it): ``guidance.yaml`` plus
one directory per category with a ``SKILL.md`` and ``lessons/<slug>.md``.

    load(root)                         validate a root -> Library (GuidanceError lists every problem)
    load_layers(framework, company=, project=, personal=)
                                       the four layers merged: a bad higher layer is rejected
                                       alone (a layer_invalid warning), higher ids replace lower
    recommend(library, stage, ...)     choose, order and budget lessons for a stage
    pointers(library, stage, ...)      the same choice without bodies (summary only)
    code_symbols(code)                 API-looking names in C#, for symbols= (no model)
    index(library) / read(library, ids)
    respond(library, mode, ...)        the get_guidance tool reply
    render_index(library)              the revit://guidance/index text
    plan_index(root) / indexer.apply   what `revit-bridge guidance index` writes
    load_contract(root)                the snippet contract (P2), from revit-core/snippet-contract

No model, no network, no environment variables: the callers (the MCP server
and the CLI) read the layer directories from the environment and pass them in.
"""
from __future__ import annotations

from revit_bridge.guidance.contract import SnippetContract, load_contract
from revit_bridge.guidance.indexer import plan_index
from revit_bridge.guidance.loader import GuidanceError, Library, load, load_layers
from revit_bridge.guidance.model import (
    CATEGORIES,
    KINDS,
    LAYER_COMPANY,
    LAYER_FRAMEWORK,
    LAYER_PERSONAL,
    LAYER_PROJECT,
    LAYERS,
    SCHEMA_VERSION,
    STAGES,
    Budget,
    Category,
    Config,
    Layer,
    Lesson,
    Triggers,
    VersionRange,
    parse_version,
)
from revit_bridge.guidance.scan import code_symbols
from revit_bridge.guidance.select import index, pointers, read, recommend, render_index, respond

__all__ = [
    "CATEGORIES", "KINDS", "LAYERS", "LAYER_COMPANY", "LAYER_FRAMEWORK", "LAYER_PERSONAL", "LAYER_PROJECT",
    "SCHEMA_VERSION", "STAGES",
    "Budget", "Category", "Config", "GuidanceError", "Layer", "Lesson", "Library", "SnippetContract", "Triggers",
    "VersionRange",
    "code_symbols", "index", "load", "load_contract", "load_layers", "parse_version", "plan_index", "pointers",
    "read", "recommend", "render_index", "respond",
]
