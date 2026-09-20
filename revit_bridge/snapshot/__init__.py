"""Project snapshot primitives: query atoms, model queries, the snapshot itself.

``take_snapshot`` collects units, active view, levels, grids, selection,
links, phases and a type summary in one go; ``run_query`` answers the
read-only ``query(kind, args)`` tool.
"""
from revit_bridge.snapshot.atoms import ATOM_CATALOG, AtomDef, AtomKind, AtomResolver, get_atom_keys, list_atoms
from revit_bridge.snapshot.project import (
    DEFAULT_CATEGORIES,
    ActiveView,
    GridSummary,
    LevelInfo,
    LinkInfo,
    ProjectSnapshot,
    SelectedElement,
    TypeSummary,
    take_snapshot,
)
from revit_bridge.snapshot.query import (
    ALLOWED_CATEGORIES,
    CATEGORY_RE,
    HOSTED_CATEGORIES,
    OST_REFERENCE,
    QUERY_KINDS,
    RevitQueryError,
    RevitQueryExecutor,
    detect_length_unit,
    run_query,
    sanitize_categories,
)

__all__ = [
    "ALLOWED_CATEGORIES",
    "ATOM_CATALOG",
    "ActiveView",
    "AtomDef",
    "AtomKind",
    "AtomResolver",
    "CATEGORY_RE",
    "DEFAULT_CATEGORIES",
    "GridSummary",
    "HOSTED_CATEGORIES",
    "LevelInfo",
    "LinkInfo",
    "OST_REFERENCE",
    "ProjectSnapshot",
    "QUERY_KINDS",
    "RevitQueryError",
    "RevitQueryExecutor",
    "SelectedElement",
    "TypeSummary",
    "detect_length_unit",
    "get_atom_keys",
    "list_atoms",
    "run_query",
    "sanitize_categories",
    "take_snapshot",
]
