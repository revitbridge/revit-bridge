"""Guidance format v1: loading and validation, selection, the seed content, get_guidance."""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import re
from pathlib import Path

import pytest

import revit_bridge.mcp_server as server
from revit_bridge import guidance, paths
from revit_bridge.guidance import GuidanceError, VersionRange, code_symbols, load, parse_version, recommend, respond
from revit_bridge.guidance import scan
from revit_bridge.guidance.model import parse_range
from revit_bridge.guidance.select import READ_HINT, index, read, render_index
from revit_bridge.revit import sandbox

ROOT = Path(__file__).resolve().parents[1]
CONTENT = ROOT / "plugin" / "guidance"

CONFIG = """\
schema_version: 1
order: [revit-core, revit-collect, revit-params, revit-geometry, revit-families,
        revit-elements, revit-structure, revit-mep, revit-views, revit-addin, revit-verify]
always: [revit-core]
budget:
  items: {items}
  utf8_bytes: {utf8_bytes}
  fix_inline_items: {inline_items}
  fix_inline_bytes: {inline_bytes}
"""


def config(items=5, utf8_bytes=12000, inline_items=2, inline_bytes=2400) -> str:
    return CONFIG.format(items=items, utf8_bytes=utf8_bytes, inline_items=inline_items, inline_bytes=inline_bytes)


def lesson(lesson_id: str, body: str = "## Scenario\n\nText.\n", *, raw_meta: str | None = None, **meta) -> str:
    """A lesson file; ``meta`` overrides the defaults (None removes a key), ``raw_meta`` is appended."""
    fields = {
        "schema_version": "1",
        "id": lesson_id,
        "revision": "1",
        "title": f'"Lesson {lesson_id}"',
        "stage": "[orient, write, fix]",
        "kind": "pitfall",
        "revit": '">=2024 <2028"',
        "triggers": "\n  symbols: [Wall]",
    }
    fields.update(meta)
    lines = ["---"] + [f"{k}: {v}" for k, v in fields.items() if v is not None]
    if raw_meta:
        lines.append(raw_meta)
    return "\n".join(lines) + "\n---\n\n" + body


def make_root(tmp_path: Path, lessons: dict[str, str], cfg: str | None = None, skills: dict[str, str] | None = None) -> Path:
    root = tmp_path / "guidance"
    root.mkdir(parents=True)
    (root / "guidance.yaml").write_text(cfg if cfg is not None else config(), encoding="utf-8")
    for rel, text in lessons.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    for category, text in (skills or {}).items():
        (root / category).mkdir(exist_ok=True)
        (root / category / "SKILL.md").write_text(text, encoding="utf-8", newline="\n")
    return root


def problems_of(root: Path) -> list[str]:
    with pytest.raises(GuidanceError) as info:
        load(root)
    assert info.value.problems
    return info.value.problems


def mentions(problems: list[str], *fragments: str) -> bool:
    return any(all(f in p for f in fragments) for p in problems)


@pytest.fixture(scope="module")
def seed():
    return load(CONTENT)


# -- version ranges ------------------------------------------------------------------

def test_version_ranges_parse_and_contain():
    assert parse_range("any") == ()
    assert parse_range(">=2024 <2027") == ((">=", (2024, 0)), ("<", (2027, 0)))
    assert parse_range("==2026") == (("==", (2026, 0)),)
    assert parse_range("<=2025.5") == (("<=", (2025, 5)),)
    for bad in ("", "2024", ">=24", ">=2024.3", "=>2024", ">= 2024", "any >=2024", "ANY", None, 2024):
        assert parse_range(bad) is None, bad
    rng = VersionRange.parse(">=2024 <2027")
    assert rng.contains("2024") and rng.contains("2026.5") and rng.contains((2026, 4))
    assert not rng.contains("2027") and not rng.contains("2023.5")
    assert VersionRange.parse("any").contains("2019") and VersionRange.parse("any").is_any
    assert VersionRange.parse("<=2025.5").contains("2025.5") and not VersionRange.parse("<=2025.5").contains("2026")
    assert parse_version("Autodesk Revit 2026.5") == (2026, 5)
    assert parse_version("2026") == (2026, 0)
    assert parse_version("Revit 26") is None and parse_version(None) is None
    with pytest.raises(ValueError):
        VersionRange.parse(">=2024.1")


# -- loading: the valid case ---------------------------------------------------------

def test_a_minimal_root_loads_with_hash_layer_and_clean_body(tmp_path):
    text = lesson("revit-core/a", body="\n\n## Scenario\n\nText.\n\n\n")
    root = make_root(tmp_path, {"revit-core/lessons/a.md": text})
    library = load(root)
    (item,) = library.lessons
    raw = (root / "revit-core/lessons/a.md").read_bytes()
    assert item.hash == hashlib.sha256(raw).hexdigest()[:16] and len(item.hash) == 16
    assert item.layer == "framework" and item.path == "revit-core/lessons/a.md"
    assert item.body == "## Scenario\n\nText.\n"
    assert item.order == 100 and item.supersedes == () and item.superseded_by is None and item.enforced is False
    assert library.get("revit-core/a") is item and library.get("nope") is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.title = "changed"
    with pytest.raises(dataclasses.FrozenInstanceError):
        library.lessons = ()
    assert isinstance(library.lessons, tuple)


def test_lessons_are_sorted_by_category_order_then_order_then_id(tmp_path):
    root = make_root(tmp_path, {
        "revit-geometry/lessons/a.md": lesson("revit-geometry/a"),
        "revit-core/lessons/z.md": lesson("revit-core/z", order="5"),
        "revit-core/lessons/b.md": lesson("revit-core/b", order="5"),
        "revit-core/lessons/a.md": lesson("revit-core/a", order="50"),
        "revit-collect/lessons/a.md": lesson("revit-collect/a", order="1"),
    })
    assert [x.id for x in load(root).lessons] == [
        "revit-core/b", "revit-core/z", "revit-core/a", "revit-collect/a", "revit-geometry/a"]


# -- loading: one negative case per rule --------------------------------------------

def test_wrong_category_directory(tmp_path):
    root = make_root(tmp_path, {"revit-foo/lessons/a.md": lesson("revit-foo/a")})
    problems = problems_of(root)
    assert mentions(problems, "revit-foo/", "not a category in the category table")
    assert mentions(problems, "revit-foo/lessons/a.md", "not in the category table")


def test_id_must_match_directory_and_file_name(tmp_path):
    root = make_root(tmp_path, {
        "revit-core/lessons/a.md": lesson("revit-core/b"),
        "revit-core/lessons/c.md": lesson("revit-geometry/c"),
    })
    problems = problems_of(root)
    assert mentions(problems, "revit-core/lessons/a.md", "does not match its path", "revit-core/a")
    assert mentions(problems, "revit-core/lessons/c.md", "does not match its path")


def test_slug_must_be_lowercase_letters_digits_and_hyphens(tmp_path):
    root = make_root(tmp_path, {"revit-core/lessons/Bad_Name.md": lesson("revit-core/Bad_Name")})
    assert mentions(problems_of(root), "Bad_Name.md", "[a-z0-9-]+")


def test_duplicate_id(tmp_path):
    root = make_root(tmp_path, {
        "revit-core/lessons/a.md": lesson("revit-core/a"),
        "revit-core/lessons/b.md": lesson("revit-core/a"),
    })
    assert mentions(problems_of(root), "duplicate id 'revit-core/a'", "revit-core/lessons/a.md", "revit-core/lessons/b.md")


def test_dangling_and_self_supersedes(tmp_path):
    root = make_root(tmp_path, {
        "revit-core/lessons/a.md": lesson("revit-core/a", supersedes="[revit-core/missing]"),
        "revit-core/lessons/b.md": lesson("revit-core/b", supersedes="[revit-core/b]"),
        "revit-core/lessons/c.md": lesson("revit-core/c", superseded_by="revit-core/gone"),
        "revit-core/lessons/d.md": lesson("revit-core/d", superseded_by="revit-core/d"),
    })
    problems = problems_of(root)
    assert mentions(problems, "a.md", "supersedes unknown id 'revit-core/missing'")
    assert mentions(problems, "b.md", "supersedes itself")
    assert mentions(problems, "c.md", "superseded_by unknown id 'revit-core/gone'")
    assert mentions(problems, "d.md", "superseded_by points at itself")


def test_existing_supersedes_targets_load(tmp_path):
    root = make_root(tmp_path, {
        "revit-core/lessons/a.md": lesson("revit-core/a", supersedes="[revit-core/b]"),
        "revit-core/lessons/b.md": lesson("revit-core/b", superseded_by="revit-core/a", enforced="true"),
    })
    library = load(root)
    assert library.get("revit-core/a").supersedes == ("revit-core/b",)
    assert library.get("revit-core/b").superseded_by == "revit-core/a" and library.get("revit-core/b").enforced


@pytest.mark.parametrize("value", ['"2024"', '">=24"', '">=2024.3"', '"=>2024"', '"since 2024"', "2024", '""'])
def test_bad_version_range(tmp_path, value):
    root = make_root(tmp_path, {"revit-core/lessons/a.md": lesson("revit-core/a", revit=value)})
    assert mentions(problems_of(root), "revit: bad version range")


def test_errors_need_symbols(tmp_path):
    root = make_root(tmp_path, {"revit-core/lessons/a.md": lesson(
        "revit-core/a", triggers="\n  errors: [CS1061]\n  categories: [OST_Walls]")})
    assert mentions(problems_of(root), "errors need symbols")


def test_triggers_must_not_be_all_empty(tmp_path):
    root = make_root(tmp_path, {
        "revit-core/lessons/a.md": lesson("revit-core/a", triggers="\n  symbols: []\n  categories: []"),
        "revit-core/lessons/b.md": lesson("revit-core/b", triggers="{}"),
    })
    problems = problems_of(root)
    assert mentions(problems, "a.md", "all empty") and mentions(problems, "b.md", "all empty")


def test_trigger_values_are_checked(tmp_path):
    root = make_root(tmp_path, {"revit-core/lessons/a.md": lesson("revit-core/a", triggers=(
        "\n  categories: [Walls]\n  symbols: [Wall, 'not an identifier']\n  errors: [1061]"
        "\n  exceptions: [Bad-Name]\n  extra: [x]"))})
    problems = problems_of(root)
    assert mentions(problems, "triggers.categories", "'Walls'", "OST_")
    assert mentions(problems, "triggers.symbols", "'not an identifier'")
    assert mentions(problems, "triggers.errors", "1061")
    assert mentions(problems, "triggers.exceptions", "'Bad-Name'")
    assert mentions(problems, "triggers: unknown key 'extra'")


def test_body_size_limit(tmp_path):
    fits = "x" * 16383 + "\n"
    over = "x" * 16384 + "\n"
    assert load(make_root(tmp_path / "ok", {"revit-core/lessons/a.md": lesson("revit-core/a", body=fits)}))
    root = make_root(tmp_path / "over", {"revit-core/lessons/a.md": lesson("revit-core/a", body=over)})
    assert mentions(problems_of(root), "body is 16385 UTF-8 bytes; the limit is 16384")


def test_always_must_be_in_order_and_order_must_name_known_categories(tmp_path):
    cfg = config().replace("always: [revit-core]", "always: [revit-core, revit-extra]").replace(
        "revit-verify]", "revit-verify, revit-unknown]")
    problems = problems_of(make_root(tmp_path, {"revit-core/lessons/a.md": lesson("revit-core/a")}, cfg=cfg))
    assert mentions(problems, "guidance.yaml", "always: 'revit-extra' is not in order")
    assert mentions(problems, "guidance.yaml", "order: unknown category 'revit-unknown'")


def test_config_schema_version_budget_and_missing_file(tmp_path):
    cfg = config().replace("schema_version: 1", "schema_version: 2").replace("items: 5", "items: 0")
    problems = problems_of(make_root(tmp_path / "a", {}, cfg=cfg))
    assert mentions(problems, "guidance.yaml", "schema_version must be 1")
    assert mentions(problems, "guidance.yaml", "budget.items must be a positive integer")
    root = make_root(tmp_path / "b", {})
    (root / "guidance.yaml").unlink()
    assert mentions(problems_of(root), "guidance.yaml: missing")


def test_lesson_field_types(tmp_path):
    root = make_root(tmp_path, {"revit-core/lessons/a.md": lesson(
        "revit-core/a", schema_version="2", revision="0", title='"' + "t" * 121 + '"',
        stage="[orient, deploy]", kind="tip", order="first", enforced="maybe", raw_meta="colour: red")})
    problems = problems_of(root)
    for fragment in ("schema_version must be 1", "revision must be an integer >= 1", "title is longer than 120",
                     "unknown stage 'deploy'", "kind must be one of", "order must be an integer",
                     "enforced must be true or false", "unknown key 'colour'"):
        assert mentions(problems, fragment), fragment


def test_required_keys_empty_stage_and_front_matter(tmp_path):
    root = make_root(tmp_path, {
        "revit-core/lessons/a.md": lesson("revit-core/a", triggers=None, kind=None),
        "revit-core/lessons/b.md": lesson("revit-core/b", stage="[]"),
        "revit-core/lessons/c.md": "no front matter here\n",
        "revit-core/lessons/d.md": "---\nid: [unclosed\n---\n",
        "revit-core/lessons/e.md": lesson("revit-core/e", raw_meta="kind: fact"),
        "revit-core/lessons/f.md": lesson("revit-core/f", stage=""),
    })
    problems = problems_of(root)
    assert mentions(problems, "a.md", "triggers is required") and mentions(problems, "a.md", "kind is required")
    assert mentions(problems, "b.md", "stage must not be empty")
    assert mentions(problems, "f.md", "stage must not be empty")
    assert mentions(problems, "c.md", "no front matter")
    assert mentions(problems, "d.md", "not valid YAML")
    assert mentions(problems, "e.md", "duplicate key 'kind'")


def test_every_problem_is_reported_at_once(tmp_path):
    root = make_root(tmp_path, {
        "revit-core/lessons/a.md": lesson("revit-core/a", revit='"soon"'),
        "revit-geometry/lessons/b.md": lesson("revit-geometry/b", triggers="\n  errors: [CS1061]"),
        "revit-params/lessons/c.md": lesson("revit-params/x"),
    }, cfg=config().replace("always: [revit-core]", "always: [revit-nope]"))
    problems = problems_of(root)
    assert len(problems) >= 4
    assert mentions(problems, "a.md") and mentions(problems, "b.md") and mentions(problems, "c.md")
    assert mentions(problems, "guidance.yaml")
    assert str(GuidanceError(problems)) == "; ".join(problems)


# -- selection on a synthetic library ------------------------------------------------

def _sized(lesson_id: str, size: int, **meta) -> str:
    """A lesson whose body is exactly ``size`` UTF-8 bytes (a newline included)."""
    return lesson(lesson_id, body="b" * (size - 1) + "\n", **meta)


def test_budget_items_boundary(tmp_path):
    files = {f"revit-core/lessons/l{i}.md": _sized(f"revit-core/l{i}", 10, order=str(i)) for i in range(1, 8)}
    library = load(make_root(tmp_path, files, cfg=config(items=5)))
    out = recommend(library, "orient")
    assert [x["id"] for x in out["items"]] == [f"revit-core/l{i}" for i in range(1, 6)]
    assert out["omitted"] == [{"id": f"revit-core/l{i}", "title": f"Lesson revit-core/l{i}", "reason": "budget"}
                              for i in (6, 7)]
    assert out["budget"] == {"items": 5, "utf8_bytes": 12000, "used_bytes": 50}


def test_budget_bytes_boundary_and_a_later_smaller_lesson(tmp_path):
    files = {
        "revit-core/lessons/a.md": _sized("revit-core/a", 60, order="1"),
        "revit-core/lessons/b.md": _sized("revit-core/b", 40, order="2"),   # 60 + 40 = 100: exactly the budget
        "revit-core/lessons/c.md": _sized("revit-core/c", 2, order="3"),    # the smallest body: over
    }
    library = load(make_root(tmp_path, files, cfg=config(utf8_bytes=100)))
    out = recommend(library, "orient")
    assert [x["id"] for x in out["items"]] == ["revit-core/a", "revit-core/b"]
    assert out["budget"]["used_bytes"] == 100 and [x["bytes"] for x in out["items"]] == [60, 40]
    assert out["omitted"] == [{"id": "revit-core/c", "title": "Lesson revit-core/c", "reason": "budget"}]

    files["revit-core/lessons/a.md"] = _sized("revit-core/a", 101, order="1")   # alone over the budget
    library = load(make_root(tmp_path / "2", files, cfg=config(utf8_bytes=100)))
    out = recommend(library, "orient")
    assert [x["id"] for x in out["items"]] == ["revit-core/b", "revit-core/c"]
    assert [x["reason"] for x in out["omitted"]] == ["budget"] and out["omitted"][0]["id"] == "revit-core/a"
    assert all(x["truncated"] is False for x in out["items"])


def test_seen_hashes_are_omitted_and_free(tmp_path):
    files = {f"revit-core/lessons/l{i}.md": _sized(f"revit-core/l{i}", 50, order=str(i)) for i in range(1, 4)}
    library = load(make_root(tmp_path, files, cfg=config(utf8_bytes=100)))
    first = library.get("revit-core/l1")
    out = recommend(library, "orient", seen_hashes=[first.hash, "0000000000000000"])
    assert [x["id"] for x in out["items"]] == ["revit-core/l2", "revit-core/l3"]
    assert out["omitted"] == [{"id": "revit-core/l1", "title": first.title, "reason": "seen"}]
    assert out["budget"]["used_bytes"] == 100


def test_inline_cuts_bodies_and_points_at_read(tmp_path):
    big = "## Scenario\n\n" + "".join(f"line {i:04d} of a long lesson body\n" for i in range(200))
    wide = "".join(chr(0x4E2D) for _ in range(1500)) + "\n"          # 3 UTF-8 bytes per character
    files = {
        "revit-geometry/lessons/a.md": lesson("revit-geometry/a", body=big, order="1"),
        "revit-geometry/lessons/b.md": lesson("revit-geometry/b", body=wide, order="2"),
        "revit-geometry/lessons/c.md": lesson("revit-geometry/c", order="3"),
    }
    library = load(make_root(tmp_path, files))
    out = recommend(library, "fix", errors=["CS1061"], symbols=["Wall"])
    assert len(out["items"]) == 3 and not any(x["truncated"] for x in out["items"])
    out = recommend(library, "fix", errors=["CS1061"], symbols=["Wall"], inline=True)
    assert [x["id"] for x in out["items"]] == ["revit-geometry/a", "revit-geometry/b"]
    assert out["omitted"] == [{"id": "revit-geometry/c", "title": "Lesson revit-geometry/c", "reason": "budget"}]
    assert out["budget"]["items"] == 2
    for item in out["items"]:
        assert item["truncated"] is True and item["bytes"] <= 2400
        assert item["bytes"] == len(item["body"].encode("utf-8"))
        assert item["body"].endswith(READ_HINT.format(id=item["id"]))
        assert f'get_guidance(mode="read", ids=["{item["id"]}"])' in item["body"]
    assert out["items"][0]["body"].startswith("## Scenario\n\nline 0000")
    assert set(out["items"][1]["body"].split("\n")[0]) == {chr(0x4E2D)}       # cut on a character boundary
    short = load(make_root(tmp_path / "s", {"revit-core/lessons/a.md": lesson("revit-core/a")}))
    item = recommend(short, "fix", symbols=["Wall"], inline=True)["items"][0]
    assert item["truncated"] is False and item["body"] == "## Scenario\n\nText.\n"


def test_symbols_match_whole_words_case_sensitively(tmp_path):
    library = load(make_root(tmp_path, {"revit-geometry/lessons/a.md": lesson(
        "revit-geometry/a", stage="[write]", triggers="\n  symbols: [LocationCurve]")}))
    hit = lambda symbols: [x["id"] for x in recommend(library, "write", symbols=symbols)["items"]]  # noqa: E731
    assert hit(["LocationCurve"]) == hit(["Wall.LocationCurve"]) == hit(["x as LocationCurve;"]) == ["revit-geometry/a"]
    assert hit(["LocationCurves"]) == hit(["locationcurve"]) == hit(["MyLocationCurve"]) == hit([]) == []


def test_exceptions_match_short_or_full_names(tmp_path):
    library = load(make_root(tmp_path, {
        "revit-core/lessons/a.md": lesson("revit-core/a", stage="[fix]",
                                          triggers="\n  exceptions: [InvalidOperationException]"),
        "revit-core/lessons/b.md": lesson("revit-core/b", stage="[fix]",
                                          triggers="\n  exceptions: [Autodesk.Revit.Exceptions.ArgumentException]"),
    }))
    ids = lambda exc: [x["id"] for x in recommend(library, "fix", exceptions=exc)["items"]]  # noqa: E731
    assert ids(["Autodesk.Revit.Exceptions.InvalidOperationException"]) == ["revit-core/a"]
    assert ids(["InvalidOperationException"]) == ["revit-core/a"]
    assert ids(["ArgumentException"]) == ["revit-core/b"]
    assert ids(["System.ArgumentException"]) == []          # two full names must be equal
    out = recommend(library, "fix", exceptions=["InvalidOperationException"])
    assert out["items"][0]["why"] == ["stage:fix", "exception:InvalidOperationException"]


def test_stage_mode_and_input_errors(seed):
    for stage in (None, ""):
        out = recommend(seed, stage)
        assert out["error"].startswith("stage_required:") and out["items"] == []
    assert recommend(seed, "deploy")["error"].startswith("invalid_stage:")
    assert respond(seed, "summary")["error"].startswith("invalid_mode:")
    assert respond(seed, "read")["error"].startswith("ids_required:")
    out = recommend(seed, "write", skill_categories=["revit-nope"])
    assert out["error"] is None and any("revit-nope" in w for w in out["warnings"])


# -- selection on the seed content (plan section 3) ----------------------------------

CORE_IDS = ["revit-core/snippet-contract", "revit-core/internal-units-feet", "revit-core/elementid-value",
            "revit-core/transaction-owned-by-bridge", "revit-core/runtime-matrix-2024-2027"]
WALL = "revit-geometry/wall-length-from-location"


def test_orient_reads_all_of_core_within_budget(seed):
    out = recommend(seed, "orient")
    assert [x["id"] for x in out["items"]] == CORE_IDS
    assert out["omitted"] == [] and out["error"] is None
    assert out["budget"]["items"] == 5 and out["budget"]["used_bytes"] <= 12000
    assert out["budget"]["used_bytes"] == sum(x["bytes"] for x in out["items"])
    for item in out["items"]:
        assert item["why"][:2] == ["stage:orient", "always:revit-core"]
        assert item["layer"] == "framework" and item["applicability"] == "unknown"


def test_write_with_symbols_reaches_geometry(seed):
    out = recommend(seed, "write", symbols=["LocationCurve"])
    assert WALL in [x["id"] for x in out["items"]]
    wall = next(x for x in out["items"] if x["id"] == WALL)
    assert wall["why"] == ["stage:write", "symbol:LocationCurve"]
    out = recommend(seed, "write", revit_categories=["OST_Walls"])
    assert [x["id"] for x in out["items"]] == [WALL] and out["items"][0]["why"] == ["stage:write", "category:OST_Walls"]
    out = recommend(seed, "write", skill_categories=["revit-geometry"])
    assert [x["id"] for x in out["items"]] == [WALL, "revit-geometry/curve-length-property",
                                               "revit-geometry/line-create-bound"]


def test_fix_error_codes_count_only_with_a_symbol(seed):
    out = recommend(seed, "fix", errors=["CS1061"])
    assert out["items"] == [] and out["omitted"] == [] and out["error"] is None
    out = recommend(seed, "fix", errors=["CS1061"], symbols=["LocationCurve"])
    assert [x["id"] for x in out["items"]] == [WALL]
    assert "error:CS1061+symbol:LocationCurve" in out["items"][0]["why"]


def test_acceptance_fix_wall_location_curve_through_the_tool():
    args = {"mode": "recommend", "stage": "fix", "errors": ["CS1061"], "symbols": ["Wall", "LocationCurve"]}
    out = json.loads(asyncio.run(server.mcp.call_tool("get_guidance", args)).content[0].text)
    assert WALL in [x["id"] for x in out["items"]]
    wall = next(x for x in out["items"] if x["id"] == WALL)
    assert any(w.startswith("error:CS1061+symbol:") for w in wall["why"])
    assert wall["why"][0] == "stage:fix"
    args.pop("symbols")
    out = json.loads(asyncio.run(server.mcp.call_tool("get_guidance", args)).content[0].text)
    assert WALL not in [x["id"] for x in out["items"]]


def test_revit_version_excludes_or_verifies(seed):
    out = recommend(seed, "orient", revit_version="2023")
    assert [x["id"] for x in out["items"]] == ["revit-core/snippet-contract", "revit-core/transaction-owned-by-bridge"]
    assert {x["reason"] for x in out["omitted"]} == {"version"} and len(out["omitted"]) == 3
    assert all(x["applicability"] == "verified" for x in out["items"])       # "any" holds 2023
    out = recommend(seed, "orient", revit_version="Autodesk Revit 2026.5")
    assert len(out["items"]) == 5 and all(x["applicability"] == "verified" for x in out["items"])
    out = recommend(seed, "orient", revit_version="next year")
    assert all(x["applicability"] == "unknown" for x in out["items"]) and len(out["items"]) == 5
    assert any("next year" in w for w in out["warnings"])
    assert recommend(seed, "orient", revit_version="2028")["omitted"][0]["reason"] == "version"


def test_read_ignores_budget_and_seen_and_lists_missing(seed):
    every = [x.id for x in seed.lessons]
    out = respond(seed, "read", ids=every + ["revit-core/nope"], seen_hashes=[x.hash for x in seed.lessons])
    assert [x["id"] for x in out["items"]] == every
    assert out["missing"] == ["revit-core/nope"] and out["omitted"] == []
    assert out["budget"]["used_bytes"] == sum(x.body_bytes for x in seed.lessons) > 12000
    assert all(x["truncated"] is False and x["body"] == seed.get(x["id"]).body for x in out["items"])
    out = read(seed, [WALL], revit_version="2023")
    assert out["items"][0]["applicability"] == "unknown" and any(WALL in w for w in out["warnings"])


def test_index_lists_every_lesson_without_bodies(seed):
    out = index(seed)
    assert [x["id"] for x in out["items"]] == [x.id for x in seed.lessons]
    for item in out["items"]:
        assert not {"body", "bytes", "truncated"} & set(item)
        assert item["summary"] and item["why"] == []
    wall = next(x for x in out["items"] if x["id"] == WALL)
    assert wall["summary"] == "`(wall.Location as LocationCurve).Curve`, not `wall.LocationCurve`"
    assert respond(seed, "index", revit_version="2023")["omitted"][0]["reason"] == "version"


def test_index_summary_falls_back_to_the_title(tmp_path):
    skill = "---\nname: revit-core\ndescription: d\n---\n\n## Lessons\n\n- [a](lessons/a.md) - first line\n"
    library = load(make_root(tmp_path, {"revit-core/lessons/a.md": lesson("revit-core/a"),
                                        "revit-core/lessons/b.md": lesson("revit-core/b")},
                             skills={"revit-core": skill}))
    assert [x["summary"] for x in index(library)["items"]] == ["first line", "Lesson revit-core/b"]


def test_same_input_same_json(seed):
    args = dict(stage="write", symbols=["Wall", "LocationCurve", "FilteredElementCollector", "ElementId"],
                revit_categories=["OST_Walls"], revit_version="2026")
    first = json.dumps(respond(seed, "recommend", **args), sort_keys=False)
    second = json.dumps(respond(seed, "recommend", **args), sort_keys=False)
    assert first == second
    shuffled = dict(args, symbols=list(reversed(args["symbols"])))
    assert json.dumps(respond(seed, "recommend", **shuffled)) == first
    again = load(CONTENT)
    assert json.dumps(respond(again, "recommend", **args)) == first
    a = asyncio.run(server.mcp.call_tool("get_guidance", dict(args, mode="recommend"))).content[0].text
    b = asyncio.run(server.mcp.call_tool("get_guidance", dict(args, mode="recommend"))).content[0].text
    assert a == b


# -- error hits first, error_symbols ------------------------------------------------

def test_get_guidance_puts_error_hits_first():
    call = lambda **args: json.loads(asyncio.run(server.mcp.call_tool("get_guidance", args)).content[0].text)  # noqa: E731
    args = {"stage": "fix", "symbols": ["UnitUtils", "LocationCurve"]}
    plain = call(**args)
    assert [x["id"] for x in plain["items"]] == ["revit-core/internal-units-feet", WALL]      # category order
    with_error = call(errors=["CS1061"], **args)
    assert [x["id"] for x in with_error["items"]] == [WALL, "revit-core/internal-units-feet"]
    assert with_error["items"][0]["why"][-1] == "error:CS1061+symbol:LocationCurve"
    assert not any(w.startswith("error:") for w in with_error["items"][1]["why"])
    # the tool still pairs error codes with the symbols it is given
    both = call(stage="fix", errors=["CS1061"], symbols=["ElementId", "LocationCurve", "UnitUtils"])
    assert [x["id"] for x in both["items"]] == ["revit-core/elementid-value", WALL, "revit-core/internal-units-feet"]
    assert "error:CS1061+symbol:ElementId" in both["items"][0]["why"]


def test_error_symbols_decide_what_an_error_code_pairs_with(seed):
    base = dict(errors=["CS1061"], symbols=["ElementId", "Wall", "LocationCurve"])
    def errors_of(**extra):
        out = recommend(seed, "fix", **base, **extra)
        return {x["id"]: [w for w in x["why"] if w.startswith("error:")] for x in out["items"]}
    assert errors_of() == {"revit-core/elementid-value": ["error:CS1061+symbol:ElementId"],
                           WALL: ["error:CS1061+symbol:Wall,LocationCurve"]}
    assert errors_of(error_symbols=["LocationCurve"]) == {"revit-core/elementid-value": [],
                                                          WALL: ["error:CS1061+symbol:LocationCurve"]}
    assert errors_of(error_symbols=[]) == {"revit-core/elementid-value": [], WALL: []}
    ids = [x["id"] for x in recommend(seed, "fix", **base, error_symbols=["LocationCurve"])["items"]]
    assert ids == [WALL, "revit-core/elementid-value"]
    pointed = guidance.pointers(seed, "fix", **base, error_symbols=["LocationCurve"])
    assert [x["id"] for x in pointed] == ids


def test_a_body_over_the_writing_rule_loads_with_a_warning(tmp_path, seed):
    library = load(make_root(tmp_path, {
        "revit-core/lessons/a.md": _sized("revit-core/a", 2300),
        "revit-core/lessons/b.md": _sized("revit-core/b", 2301),
    }))
    assert [x.id for x in library.lessons] == ["revit-core/a", "revit-core/b"]
    assert library.warnings == ("revit-core/lessons/b.md: body is 2301 UTF-8 bytes, over the 2300-byte writing rule",)
    over = sorted(w.split(":")[0] for w in seed.warnings)        # the seed lessons that break the rule today
    assert over == sorted(f"{x.path}" for x in seed.lessons if x.body_bytes > 2300)
    assert all(x.body_bytes <= 2400 for x in seed.lessons)
    core = (CONTENT / "revit-core" / "SKILL.md").read_text(encoding="utf-8")
    assert "## Writing lessons" in core and "at or under 2300 UTF-8 bytes" in core
    assert len(seed.category("revit-core").summaries) == 5            # the note does not join the index


# -- pointers and code symbols (check_code's guidance) -------------------------------

def test_pointers_are_recommend_without_bodies_and_without_the_byte_budget(tmp_path):
    files = {f"revit-geometry/lessons/l{i}.md": _sized(f"revit-geometry/l{i}", 300, order=str(i)) for i in range(1, 8)}
    files["revit-geometry/lessons/old.md"] = lesson("revit-geometry/old", order="0", revit='"<2024"')
    library = load(make_root(tmp_path, files, cfg=config(items=5, utf8_bytes=500)))
    budgeted = recommend(library, "write", symbols=["Wall"], revit_version="2026")
    assert len(budgeted["items"]) == 1                                  # 500 bytes hold one 300-byte body
    out = guidance.pointers(library, "write", symbols=["Wall"], revit_version="2026")
    assert [x["id"] for x in out] == [f"revit-geometry/l{i}" for i in range(1, 6)]   # items cap only; old excluded
    summary_keys = set(index(library)["items"][0])
    for item in out:
        assert set(item) == summary_keys and "body" not in item and item["summary"] == item["title"]
        assert item["why"] == ["stage:write", "symbol:Wall"] and item["applicability"] == "verified"
    assert guidance.pointers(library, "write", symbols=["Floor"]) == []
    with pytest.raises(ValueError):
        guidance.pointers(library, "draft", symbols=["Wall"])


def test_inline_bodies_end_with_the_read_line(tmp_path):
    body = "## Scenario\n\n" + "x" * 3000 + "\n"
    library = load(make_root(tmp_path, {"revit-core/lessons/a.md": lesson("revit-core/a", body=body)}))
    item = recommend(library, "fix", symbols=["Wall"], inline=True)["items"][0]
    assert item["truncated"] is True and item["bytes"] <= 2400
    assert item["body"].endswith('\n\n[truncated: get_guidance(mode="read", ids=["revit-core/a"])]\n')


def test_code_symbols_skip_comments_literals_keywords_and_the_wrapper():
    code = (
        "// Floor in a line comment\n"
        "/* Ceiling in a block\n   comment */\n"
        "var wall = document.GetElement(\"Roof\") as Wall;  // Room\n"
        "var a = @\"Stair \"\"Ramp\"\" Grid\"; var b = $\"Level {Railing.Name}\"; char c = 'Z';\n"
        "var r = \"\"\"Topography \"\"\"; double d = 1E5 + 0x1F + 2.5F;\n"
        "Execute(); CodeExecutor x; AIGeneratedCode.Foo y;\n"
        "List<Element> e = new List<Element>(); return wall.LocationCurve.Curve.Length;\n"
    )
    assert code_symbols(code) == ["GetElement", "Wall", "Foo", "List", "Element", "LocationCurve", "Curve", "Length"]
    assert code_symbols("") == code_symbols(None) == code_symbols("return 1;") == []
    assert code_symbols("int x = 0; string s = null;") == []                      # keywords are lower case
    assert scan.CSHARP_KEYWORDS >= {"var", "new", "return", "typeof", "using", "foreach", "await", "nameof"}
    assert scan.WRAPPER_NAMES == {"AIGeneratedCode", "CodeExecutor", "Execute"}
    assert code_symbols("var s = \"unterminated Wall;\nreturn Curve;") == ["Wall", "Curve"]


# -- the seed content ----------------------------------------------------------------

EXPECTED_LESSONS = {
    "revit-core": {"snippet-contract", "internal-units-feet", "elementid-value", "transaction-owned-by-bridge",
                   "runtime-matrix-2024-2027"},
    "revit-collect": {"collector-needs-filter", "instances-vs-types", "selection-get-element-ids"},
    "revit-params": {"lookup-parameter-storage-type", "builtin-parameter-first"},
    "revit-geometry": {"wall-length-from-location", "curve-length-property", "line-create-bound"},
}


def test_seed_config_is_the_frozen_one(seed):
    cfg = seed.config
    assert cfg.order == guidance.CATEGORIES and cfg.always == ("revit-core",)
    assert dataclasses.asdict(cfg.budget) == {"items": 5, "utf8_bytes": 12000,
                                              "fix_inline_items": 2, "fix_inline_bytes": 2400}


def test_every_category_has_a_skill_with_only_name_and_description(seed):
    dirs = sorted(p.name for p in CONTENT.iterdir() if p.is_dir())
    assert dirs == sorted(guidance.CATEGORIES)
    for name in dirs:
        meta, body = guidance.loader.read_skill(CONTENT / name / "SKILL.md")
        assert set(meta) == {"name", "description"}, name
        assert meta["name"] == name and 0 < len(meta["description"]) <= 1024
        assert "## Lessons" in body
        listed = {slug for slug, _ in guidance.loader.index_summaries(body)}
        on_disk = {p.stem for p in (CONTENT / name / "lessons").glob("*.md")}
        assert listed == on_disk == EXPECTED_LESSONS.get(name, set()), name
        if not on_disk:
            assert "None yet." in body and not (CONTENT / name / "lessons").exists()


def test_seed_lessons(seed):
    assert {x.id for x in seed.lessons} == {f"{c}/{s}" for c, slugs in EXPECTED_LESSONS.items() for s in slugs}
    for item in seed.lessons:
        headings = re.findall(r"^## (\w+)", item.body, flags=re.M)
        if item.id == "revit-core/runtime-matrix-2024-2027":          # a table of facts: no wrong/right code
            assert headings == ["Scenario", "Versions", "Source"], item.id
        else:
            assert headings == ["Scenario", "Wrong", "Right", "Versions", "Source"], item.id
        source = item.body.split("## Source", 1)[1].strip()
        assert source and ("revit-api-docs" in source or "revit-addin-dev" in source), item.id
        assert item.body_bytes <= 2400, item.id
        assert item.revit.text in ("any", ">=2024 <2028"), item.id
        if item.category == "revit-core":
            assert "orient" in item.stage, item.id
        right = item.body.split("## Right", 1)[1].split("## Versions", 1)[0] if "## Right" in item.body else ""
        for code in re.findall(r"```csharp\n(.*?)```", right, flags=re.S):
            safe, warnings = sandbox.review(code)
            assert safe, (item.id, warnings)
            assert "return" in code and "new Transaction(" not in code, item.id
    matrix = seed.get("revit-core/runtime-matrix-2024-2027")
    assert matrix.kind == "fact" and set(matrix.stage) == {"orient", "port"}
    assert seed.get(WALL).triggers.errors == ("CS1061",) and {"Wall", "LocationCurve"} <= set(seed.get(WALL).triggers.symbols)


def test_seed_files_keep_lf_line_endings():
    for path in CONTENT.rglob("*"):
        if path.is_file():
            assert b"\r" not in path.read_bytes(), path


def test_guidance_dir_resolves_to_the_repo_content():
    assert paths.guidance_dir() == CONTENT
    assert (paths.guidance_dir() / "guidance.yaml").is_file()


# -- the MCP tool and resource -------------------------------------------------------

ITEM_KEYS = {"id", "revision", "hash", "layer", "category", "title", "kind", "stage", "revit",
             "applicability", "why", "body", "bytes", "truncated"}


def test_get_guidance_tool_schema_and_reply_shape():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    tool = tools["get_guidance"]
    assert tool.annotations.model_dump(by_alias=True)["readOnlyHint"] is True
    assert set(tool.input_schema["properties"]) == {
        "mode", "stage", "skill_categories", "revit_categories", "symbols", "errors", "exceptions",
        "revit_version", "ids", "seen_hashes"}
    assert not tool.input_schema.get("required")
    out = json.loads(asyncio.run(server.mcp.call_tool("get_guidance", {"stage": "orient"})).content[0].text)
    assert set(out) == {"schema_version", "mode", "stage", "items", "omitted", "missing", "budget", "warnings", "error"}
    assert out["schema_version"] == 1 and out["mode"] == "recommend" and out["stage"] == "orient"
    assert all(set(item) == ITEM_KEYS for item in out["items"]) and len(out["items"]) == 5
    assert set(out["budget"]) == {"items", "utf8_bytes", "used_bytes"}
    out = json.loads(asyncio.run(server.mcp.call_tool("get_guidance", {"mode": "index"})).content[0].text)
    assert all(set(item) == ITEM_KEYS - {"body", "bytes", "truncated"} | {"summary"} for item in out["items"])
    out = json.loads(asyncio.run(server.mcp.call_tool(
        "get_guidance", {"mode": "read", "ids": [WALL, "x/y"]})).content[0].text)
    assert [x["id"] for x in out["items"]] == [WALL] and out["missing"] == ["x/y"]
    out = json.loads(asyncio.run(server.mcp.call_tool("get_guidance", {})).content[0].text)
    assert out["error"].startswith("stage_required:")


def test_get_guidance_reports_invalid_content_instead_of_raising(tmp_path, monkeypatch):
    bad = make_root(tmp_path, {"revit-core/lessons/a.md": lesson("revit-core/b")})
    monkeypatch.setattr(server, "guidance_dir", lambda: bad)
    monkeypatch.setattr(server, "_guidance_library", None)
    out = json.loads(asyncio.run(server.mcp.call_tool("get_guidance", {"stage": "orient"})).content[0].text)
    assert out["error"] == "guidance_invalid" and mentions(out["problems"], "does not match its path")
    assert set(out) == {"error", "problems"}
    text = server.guidance_index()
    assert text.startswith("guidance_invalid\n") and "does not match its path" in text


def test_the_library_is_loaded_once(monkeypatch):
    monkeypatch.setattr(server, "_guidance_library", None)
    calls = []
    real = guidance.load_layers

    def counting(root, **layers):
        calls.append((root, layers))
        return real(root, **layers)

    monkeypatch.setattr(server._guidance, "load_layers", counting)
    first = server.guidance_library()
    assert server.guidance_library() is first and len(calls) == 1
    root, layers = calls[0]
    assert root == paths.guidance_dir() and layers == paths.guidance_layer_dirs()


def test_guidance_index_resource(seed):
    contents = asyncio.run(server.mcp.read_resource("revit://guidance/index"))
    text = contents[0].content
    assert text == render_index(seed)
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    assert len(lines) == len(seed.lessons)
    for line, item in zip(lines, seed.lessons):
        assert line == f"{item.id} | framework | {item.revit.text} | {item.title}"


def test_the_confirmation_hook_leaves_get_guidance_alone():
    import importlib.util

    path = ROOT / "plugin" / "hooks" / "spec_gate.py"
    module_spec = importlib.util.spec_from_file_location("spec_gate", path)
    hook = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(hook)
    assert hook.decide("mcp__plugin_revit-bridge_revit-bridge__get_guidance", {"stage": "orient"}) is None


def test_the_guidance_module_reads_no_environment_and_imports_no_network():
    for path in (ROOT / "revit_bridge" / "guidance").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "os.environ" not in text and "getenv" not in text and "import os" not in text, path
        for name in ("socket", "urllib", "http", "requests", "httpx", "websockets", "asyncio"):
            assert f"import {name}" not in text and f"from {name}" not in text, (path, name)
