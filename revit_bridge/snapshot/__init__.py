"""Project snapshot primitives: query atoms and Revit model queries.

``get_project_snapshot`` (one-shot collection of units, active view, levels,
selection, links, phases, type summary, grids) is built on top of these in a
later phase.
"""
from revit_bridge.snapshot.atoms import ATOM_CATALOG, AtomDef, AtomKind, AtomResolver, get_atom_keys, list_atoms
from revit_bridge.snapshot.query import (
    ALLOWED_CATEGORIES,
    CATEGORY_RE,
    HOSTED_CATEGORIES,
    OST_REFERENCE,
    RevitQueryExecutor,
    detect_length_unit,
    sanitize_categories,
)

__all__ = [
    "ALLOWED_CATEGORIES",
    "ATOM_CATALOG",
    "AtomDef",
    "AtomKind",
    "AtomResolver",
    "CATEGORY_RE",
    "HOSTED_CATEGORIES",
    "OST_REFERENCE",
    "RevitQueryExecutor",
    "detect_length_unit",
    "get_atom_keys",
    "list_atoms",
    "sanitize_categories",
]
