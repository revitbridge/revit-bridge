"""``snapshot_digest``: the short snapshot text for the analyst role."""
from __future__ import annotations

import pytest

from revit_bridge.snapshot import ProjectSnapshot, snapshot_digest
from revit_bridge.snapshot.project import DIGEST_MAX_CHARS


def snapshot(levels=3, categories=None, title="Tower <model>", selection=2, warnings=()):
    cats = categories if categories is not None else {"OST_Walls": 12, "OST_Doors": 8}
    return ProjectSnapshot(
        taken_at="2026-01-01T10:00:00Z", duration_ms=5,
        document={"title": title, "revit_version": "2026", "is_workshared": True},
        units={"length": "mm", "raw": "autodesk.unit.unit:millimeters-1.0.1"},
        active_view=None,
        levels=[{"id": 300 + i, "name": f"Level {i:03}", "elevation_mm": 3000.0 * i} for i in range(levels)],
        grids={"count": 0, "names": []},
        family_types=[{"category": c, "count": n, "names": [f"{c}-type-{k}" for k in range(min(n, 50))]}
                      for c, n in cats.items()],
        selection=[], selection_count=selection, links=[], phases=[], warnings=list(warnings),
        fingerprint="0123456789abcdef",
    )


def test_the_digest_has_the_outline_and_no_type_names():
    text = snapshot_digest(snapshot())
    assert text == ("Document: Tower <model> (Revit 2026, workshared)\n"
                    "Units: mm\n"
                    "Levels (3): Level 000, Level 001, Level 002\n"
                    "Types per category: OST_Walls 12, OST_Doors 8\n"
                    "Selection: 2 elements\n"
                    "Fingerprint: 0123456789abcdef")
    assert "type-0" not in text
    assert snapshot_digest(snapshot().model_dump()) == text           # the JSON form too


def test_warnings_and_missing_parts_are_said_not_zeroed():
    text = snapshot_digest(snapshot(categories={}, selection=1, warnings=["family_types: boom"]))
    assert "Types per category: none read" in text and "OST_Walls 0" not in text
    assert "Selection: 1 element\n" in text and "Warnings: 1 (partial snapshot)" in text
    sparse = snapshot_digest({"levels": [{"name": "F1"}], "family_types": [{"category": "OST_Walls"}]})
    assert sparse.splitlines() == ["Document: (no title)", "Units: unknown", "Levels (1): F1",
                                   "Types per category: none read", "Selection: unknown", "Fingerprint: none"]


@pytest.mark.parametrize("levels,categories", [(0, 0), (3, 2), (60, 6), (400, 40), (2000, 300)])
@pytest.mark.parametrize("max_chars", [0, 3, 10, 80, 300, DIGEST_MAX_CHARS, 5000])
def test_the_digest_is_never_longer_than_max_chars(levels, categories, max_chars):
    cats = {f"OST_Category{chr(65 + i % 26)}{chr(65 + i // 26 % 26)}": i + 1 for i in range(categories)}
    text = snapshot_digest(snapshot(levels=levels, categories=cats, title="T" * 300), max_chars=max_chars)
    assert len(text) <= max_chars


def test_long_lists_end_with_how_many_are_left_out():
    text = snapshot_digest(snapshot(levels=400))
    assert len(text) <= DIGEST_MAX_CHARS
    levels_line = next(line for line in text.splitlines() if line.startswith("Levels (400): Level 000"))
    assert levels_line.endswith(" more)") and "... (+" in levels_line
    shown = levels_line.count("Level ")                              # "Levels (400)" has no "Level "
    assert levels_line.endswith(f"... (+{400 - shown} more)")
    assert text.splitlines()[-1] == "Fingerprint: 0123456789abcdef"  # the fixed lines survive


def test_a_newline_in_a_name_cannot_add_a_line():
    text = snapshot_digest(snapshot(title="A\nInjected: yes"))
    assert text.splitlines()[0] == "Document: A Injected: yes (Revit 2026, workshared)"
    with pytest.raises(TypeError):
        snapshot_digest("not a snapshot")
