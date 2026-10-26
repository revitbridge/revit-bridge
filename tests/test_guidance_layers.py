"""Guidance layers (D11): framework, company, project, personal - override, enforced,
supersedes, a bad layer rejected alone, order merging, the server's cached entry."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

import revit_bridge.mcp_server as server
from revit_bridge import guidance, paths
from revit_bridge.guidance import GuidanceError, index, load, load_layers, pointers, recommend, respond
from revit_bridge.guidance.loader import merge_always, merge_order
from tests.test_guidance import CONTENT, config, lesson, make_root

WALL = "revit-geometry/wall-length-from-location"
WALL_FILE = "revit-geometry/lessons/wall-length-from-location.md"


def layer_root(tmp_path: Path, name: str, lessons: dict[str, str], cfg: str | None = None) -> Path:
    """A higher layer: lesson files and, when given, a guidance.yaml."""
    root = tmp_path / name
    root.mkdir(parents=True)
    if cfg is not None:
        (root / "guidance.yaml").write_text(cfg, encoding="utf-8", newline="\n")
    for rel, text in lessons.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return root


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def ids(out: dict) -> list[str]:
    return [x["id"] for x in out["items"]]


@pytest.fixture
def framework(tmp_path):
    return make_root(tmp_path / "fw", {
        "revit-core/lessons/a.md": lesson("revit-core/a", title='"framework a"', order="1"),
        "revit-core/lessons/b.md": lesson("revit-core/b", title='"framework b"', order="2"),
        "revit-geometry/lessons/w.md": lesson("revit-geometry/w", title='"framework w"',
                                              triggers="\n  symbols: [Wall]\n  categories: [OST_Walls]"),
    })


# -- order merging -------------------------------------------------------------------

def test_a_higher_order_reorders_the_categories_it_names_and_appends_new_ones():
    lower = ("a", "b", "c", "d", "e")
    assert merge_order(lower, ("d", "b")) == ("a", "d", "c", "b", "e")
    assert merge_order(lower, ("b",)) == lower                      # naming one category moves nothing
    assert merge_order(lower, ()) == lower                          # nothing is ever deleted
    assert merge_order(lower, ("x", "e", "a", "y")) == ("e", "b", "c", "d", "a", "x", "y")
    assert merge_always(("a",), ("b", "a")) == ("a", "b")


def test_layer_orders_apply_lowest_first(tmp_path):
    cfg = config().replace(
        "order: [revit-core, revit-collect, revit-params, revit-geometry, revit-families,\n"
        "        revit-elements, revit-structure, revit-mep, revit-views, revit-addin, revit-verify]",
        "order: [revit-core, revit-collect, revit-params, revit-geometry]")
    fw = make_root(tmp_path / "fw", {
        "revit-core/lessons/a.md": lesson("revit-core/a"),
        "revit-geometry/lessons/g.md": lesson("revit-geometry/g"),
        "revit-collect/lessons/c.md": lesson("revit-collect/c"),
    }, cfg=cfg)
    company = layer_root(tmp_path, "company", {"revit-views/lessons/v.md": lesson("revit-views/v")},
                         cfg="schema_version: 1\norder: [revit-geometry, revit-core, revit-views]\n"
                             "always: [revit-views]\n")
    project = layer_root(tmp_path, "project", {}, cfg="schema_version: 1\norder: [revit-collect]\n")
    library = load_layers(fw, company=company, project=project)
    assert library.config.order == ("revit-geometry", "revit-collect", "revit-params", "revit-core", "revit-views")
    assert library.config.always == ("revit-core", "revit-views")
    assert [x.id for x in library.lessons] == ["revit-geometry/g", "revit-collect/c", "revit-core/a", "revit-views/v"]
    out = recommend(library, "orient")
    assert ids(out) == ["revit-core/a", "revit-views/v"]
    assert out["items"][1]["why"][:2] == ["stage:orient", "always:revit-views"]
    assert library.layer_warnings == () and out["warnings"] == []


def test_always_must_be_in_the_merged_order(tmp_path):
    cfg = config().replace(
        "order: [revit-core, revit-collect, revit-params, revit-geometry, revit-families,\n"
        "        revit-elements, revit-structure, revit-mep, revit-views, revit-addin, revit-verify]",
        "order: [revit-core]")
    fw = make_root(tmp_path / "fw", {"revit-core/lessons/a.md": lesson("revit-core/a")}, cfg=cfg)
    company = layer_root(tmp_path, "company", {}, cfg="schema_version: 1\nalways: [revit-views]\n")
    library = load_layers(fw, company=company)
    assert library.layer_warnings == (
        "layer_invalid: company: guidance.yaml: always: 'revit-views' is not in order",)
    assert library.config.always == ("revit-core",)


# -- override ------------------------------------------------------------------------

def test_the_highest_layer_replaces_a_lesson_whole(tmp_path, framework):
    company = layer_root(tmp_path, "company", {
        "revit-core/lessons/a.md": lesson("revit-core/a", title='"company a"', order="1"),
        "revit-geometry/lessons/w.md": lesson("revit-geometry/w", title='"company w"',
                                              triggers="\n  symbols: [Wall]\n  categories: [OST_Walls]"),
    })
    project = layer_root(tmp_path, "project", {
        "revit-core/lessons/a.md": lesson("revit-core/a", title='"project a"', order="1"),
    })
    personal = layer_root(tmp_path, "personal", {
        "revit-geometry/lessons/w.md": lesson("revit-geometry/w", title='"personal w"',
                                              triggers="\n  symbols: [Curve]"),
    })
    library = load_layers(framework, company=company, project=project, personal=personal)
    a, b, w = library.get("revit-core/a"), library.get("revit-core/b"), library.get("revit-geometry/w")
    assert (a.layer, a.title) == ("project", "project a")
    assert a.hash == file_hash(project / "revit-core/lessons/a.md") != file_hash(framework / "revit-core/lessons/a.md")
    assert (b.layer, b.title) == ("framework", "framework b")
    assert (w.layer, w.title) == ("personal", "personal w")
    assert w.triggers.categories == () and w.triggers.symbols == ("Curve",)        # no field merging
    assert [(old.id, old.layer, by) for old, by in library.overridden] == [
        ("revit-core/a", "framework", "project"), ("revit-core/a", "company", "project"),
        ("revit-geometry/w", "framework", "personal"), ("revit-geometry/w", "company", "personal")]
    assert [x.state for x in library.layers] == ["loaded"] * 4
    assert [x.lessons for x in library.layers] == [3, 2, 1, 1]

    out = recommend(library, "write", symbols=["Wall"], revit_categories=["OST_Walls"])
    assert ids(out) == ["revit-core/a", "revit-core/b"] and out["omitted"] == []   # w's triggers are personal's
    out = recommend(library, "write", symbols=["Curve"])
    assert ids(out) == ["revit-geometry/w"] and out["items"][0]["layer"] == "personal"
    assert out["items"][0]["hash"] == w.hash and out["items"][0]["title"] == "personal w"
    out = recommend(library, "orient")
    assert [(x["id"], x["layer"]) for x in out["items"]] == [("revit-core/a", "project"), ("revit-core/b", "framework")]
    assert not any(o["reason"].startswith("overridden_by") for o in out["omitted"])

    listed = index(library)
    assert [(x["id"], x["layer"]) for x in listed["items"]] == [
        ("revit-core/a", "project"), ("revit-core/b", "framework"), ("revit-geometry/w", "personal")]
    assert listed["omitted"] == [
        {"id": "revit-core/a", "title": "framework a", "reason": "overridden_by:project"},
        {"id": "revit-core/a", "title": "company a", "reason": "overridden_by:project"},
        {"id": "revit-geometry/w", "title": "framework w", "reason": "overridden_by:personal"},
        {"id": "revit-geometry/w", "title": "company w", "reason": "overridden_by:personal"}]
    assert respond(library, "read", ids=["revit-core/a"])["items"][0]["layer"] == "project"


def test_an_enforced_company_lesson_is_not_replaced(tmp_path, framework):
    company = layer_root(tmp_path, "company", {
        "revit-core/lessons/a.md": lesson("revit-core/a", title='"company a"', enforced="true"),
        "revit-core/lessons/b.md": lesson("revit-core/b", title='"company b"'),
    })
    project = layer_root(tmp_path, "project", {"revit-core/lessons/a.md": lesson("revit-core/a", title='"project a"')})
    personal = layer_root(tmp_path, "personal", {
        "revit-core/lessons/a.md": lesson("revit-core/a", title='"personal a"'),
        "revit-core/lessons/b.md": lesson("revit-core/b", title='"personal b"'),
    })
    library = load_layers(framework, company=company, project=project, personal=personal)
    assert library.get("revit-core/a").layer == "company" and library.get("revit-core/a").title == "company a"
    assert library.get("revit-core/b").layer == "personal"                 # not enforced: replaced
    assert library.layer_warnings == ("enforced_kept: revit-core/a ignored project",
                                      "enforced_kept: revit-core/a ignored personal")
    assert set(library.layer_warnings) <= set(library.warnings)
    out = recommend(library, "orient")
    assert out["warnings"][:2] == list(library.layer_warnings)
    assert index(library)["warnings"] == list(library.layer_warnings)
    assert [(o.layer, by) for o, by in library.overridden] == [("framework", "company"), ("framework", "personal"),
                                                               ("company", "personal")]


def test_enforced_is_ignored_in_the_framework_and_refused_in_project_and_personal(tmp_path):
    fw = make_root(tmp_path / "fw", {"revit-core/lessons/a.md": lesson("revit-core/a", enforced="true")})
    company = layer_root(tmp_path, "company", {"revit-core/lessons/a.md": lesson("revit-core/a", title='"company a"')})
    project = layer_root(tmp_path, "project", {"revit-core/lessons/p.md": lesson("revit-core/p", enforced="true")})
    personal = layer_root(tmp_path, "personal", {
        "revit-core/lessons/q.md": lesson("revit-core/q", enforced="true"),
        "revit-core/lessons/r.md": lesson("revit-core/r", enforced="false"),
    })
    library = load_layers(fw, company=company, project=project, personal=personal)
    assert library.get("revit-core/a").layer == "company"                 # framework enforced counts for nothing
    assert library.get("revit-core/p") is None and library.get("revit-core/q") is None
    assert library.get("revit-core/r") is None                             # the whole layer is rejected
    assert library.layer_warnings == (
        "enforced_ignored: revit-core/a: enforced counts only in the company layer",
        "layer_invalid: project: revit-core/lessons/p.md: enforced: true is allowed only in the company layer, "
        "not the project layer",
        "layer_invalid: personal: revit-core/lessons/q.md: enforced: true is allowed only in the company layer, "
        "not the personal layer")
    states = {x.name: x.state for x in library.layers}
    assert states == {"framework": "loaded", "company": "loaded", "project": "invalid", "personal": "invalid"}


# -- a bad layer ---------------------------------------------------------------------

def test_a_bad_layer_is_rejected_alone(tmp_path, framework):
    company = layer_root(tmp_path, "company", {
        "revit-core/lessons/a.md": lesson("revit-core/a", title='"company a"'),
        "revit-core/lessons/x.md": lesson("revit-core/y"),                   # id does not match the path
        "revit-core/lessons/z.md": lesson("revit-core/z", revit='"soon"'),
    })
    project = layer_root(tmp_path, "project", {"revit-core/lessons/p.md": lesson("revit-core/p")})
    library = load_layers(framework, company=company, project=project)
    assert library.get("revit-core/a").layer == "framework"                 # no company lesson at all
    assert library.get("revit-core/p").layer == "project"
    (warning,) = library.layer_warnings
    assert warning.startswith("layer_invalid: company: revit-core/lessons/x.md: id 'revit-core/y' does not match")
    company_state = library.layers[1]
    assert company_state.state == "invalid" and len(company_state.problems) == 2
    assert company_state.root == str(company)
    out = recommend(library, "orient")
    assert ids(out) == ["revit-core/a", "revit-core/b", "revit-core/p"] and out["warnings"] == [warning]
    assert out["error"] is None


def test_a_higher_layer_may_not_set_the_budget_or_unknown_keys(tmp_path, framework):
    company = layer_root(tmp_path, "company", {"revit-core/lessons/c.md": lesson("revit-core/c")},
                         cfg="schema_version: 1\nbudget:\n  items: 50\n")
    project = layer_root(tmp_path, "project", {}, cfg="schema_version: 1\ncolour: red\norder: [revit-nope]\n")
    personal = layer_root(tmp_path, "personal", {}, cfg="order: [revit-core]\n")
    library = load_layers(framework, company=company, project=project, personal=personal)
    assert library.config.budget.items == 5 and library.get("revit-core/c") is None
    assert library.layer_warnings == (
        "layer_invalid: company: guidance.yaml: budget is set only in the framework layer",
        "layer_invalid: project: guidance.yaml: unknown key 'colour'",
        "layer_invalid: personal: guidance.yaml: schema_version must be 1")
    assert "guidance.yaml: order: unknown category 'revit-nope'" in library.layers[2].problems


def test_unset_and_missing_layers_are_skipped(tmp_path, framework):
    library = load_layers(framework, company=tmp_path / "nowhere", project=None, personal=tmp_path / "nor-here")
    assert [(x.name, x.state) for x in library.layers] == [
        ("framework", "loaded"), ("company", "missing"), ("project", "unset"), ("personal", "missing")]
    assert library.layers[1].root == str(tmp_path / "nowhere") and library.layers[2].root is None
    assert library.warnings == () and library.layer_warnings == ()
    assert [x.id for x in library.lessons] == [x.id for x in load(framework).lessons]


def test_an_invalid_framework_still_rejects_everything(tmp_path):
    bad = make_root(tmp_path / "fw", {"revit-core/lessons/a.md": lesson("revit-core/b")})
    company = layer_root(tmp_path, "company", {"revit-core/lessons/c.md": lesson("revit-core/c")})
    with pytest.raises(GuidanceError) as info:
        load_layers(bad, company=company)
    assert any("does not match its path" in p for p in info.value.problems)


# -- supersedes ----------------------------------------------------------------------

def test_supersedes_works_across_layers(tmp_path, framework):
    company = layer_root(tmp_path, "company", {
        "revit-core/lessons/c.md": lesson("revit-core/c", supersedes="[revit-core/a]", order="3"),
    })
    personal = layer_root(tmp_path, "personal", {
        "revit-core/lessons/d.md": lesson("revit-core/d", superseded_by="revit-core/c", order="4"),
    })
    library = load_layers(framework, company=company, personal=personal)
    assert library.superseded_by("revit-core/a") == "revit-core/c" and library.superseded_by("revit-core/c") is None
    out = recommend(library, "orient")
    assert ids(out) == ["revit-core/b", "revit-core/c", "revit-core/d"]
    assert out["omitted"] == [{"id": "revit-core/a", "title": "framework a", "reason": "superseded_by:revit-core/c"}]
    assert [x["id"] for x in pointers(library, "orient")] == ids(out)
    assert "revit-core/a" in ids(index(library))                       # superseded only drops out of recommend
    assert respond(library, "read", ids=["revit-core/a"])["items"][0]["id"] == "revit-core/a"


def test_supersedes_in_a_single_root(tmp_path):
    root = make_root(tmp_path, {
        "revit-core/lessons/a.md": lesson("revit-core/a", supersedes="[revit-core/b]"),
        "revit-core/lessons/b.md": lesson("revit-core/b"),
    })
    out = recommend(load(root), "orient")
    assert ids(out) == ["revit-core/a"] and out["omitted"][0]["reason"] == "superseded_by:revit-core/a"


def test_dangling_references_reject_the_layer_and_cascade(tmp_path, framework):
    company = layer_root(tmp_path, "company", {"revit-core/lessons/c.md": lesson("revit-core/c")},
                         cfg="schema_version: 1\nbudget:\n  items: 1\n")
    project = layer_root(tmp_path, "project", {
        "revit-core/lessons/p.md": lesson("revit-core/p", supersedes="[revit-core/c]"),   # c lives in company
    })
    personal = layer_root(tmp_path, "personal", {
        "revit-core/lessons/q.md": lesson("revit-core/q", superseded_by="revit-core/nowhere"),
        "revit-core/lessons/r.md": lesson("revit-core/r", supersedes="[revit-core/r]"),
    })
    library = load_layers(framework, company=company, project=project, personal=personal)
    assert [x.state for x in library.layers] == ["loaded", "invalid", "invalid", "invalid"]
    assert library.layer_warnings == (
        "layer_invalid: company: guidance.yaml: budget is set only in the framework layer",
        "layer_invalid: project: revit-core/lessons/p.md: supersedes unknown id 'revit-core/c'",
        "layer_invalid: personal: revit-core/lessons/r.md: supersedes itself")
    assert library.layers[3].problems == (            # every problem of the layer, not only the first
        "revit-core/lessons/r.md: supersedes itself",
        "revit-core/lessons/q.md: superseded_by unknown id 'revit-core/nowhere'")


def test_a_supersedes_cycle_rejects_the_whole_library(tmp_path, framework):
    company = layer_root(tmp_path, "company", {"revit-core/lessons/x.md": lesson("revit-core/x", supersedes="[revit-core/y]")})
    project = layer_root(tmp_path, "project", {"revit-core/lessons/y.md": lesson("revit-core/y", supersedes="[revit-core/x]")})
    with pytest.raises(GuidanceError) as info:
        load_layers(framework, company=company, project=project)
    assert info.value.problems == ["supersedes form a cycle: revit-core/x, revit-core/y"]
    # a single root: a three-step cycle
    root = make_root(tmp_path / "one", {
        "revit-core/lessons/a.md": lesson("revit-core/a", supersedes="[revit-core/b]"),
        "revit-core/lessons/b.md": lesson("revit-core/b", supersedes="[revit-core/c]"),
        "revit-core/lessons/c.md": lesson("revit-core/c", supersedes="[revit-core/a]"),
        "revit-core/lessons/d.md": lesson("revit-core/d", supersedes="[revit-core/a]"),
    })
    with pytest.raises(GuidanceError) as info:
        load(root)
    assert info.value.problems == ["supersedes form a cycle: revit-core/a, revit-core/b, revit-core/c"]


def test_only_the_lessons_in_force_can_form_a_cycle(tmp_path):
    fw = make_root(tmp_path / "fw", {
        "revit-core/lessons/x.md": lesson("revit-core/x"),
        "revit-core/lessons/y.md": lesson("revit-core/y", supersedes="[revit-core/x]"),
    })
    company = layer_root(tmp_path, "company", {
        "revit-core/lessons/x.md": lesson("revit-core/x", supersedes="[revit-core/y]"),
        "revit-core/lessons/y.md": lesson("revit-core/y", title='"company y"'),       # drops the old supersedes
    })
    library = load_layers(fw, company=company)
    out = recommend(library, "orient")
    assert ids(out) == ["revit-core/x"] and out["omitted"][0] == {
        "id": "revit-core/y", "title": "company y", "reason": "superseded_by:revit-core/x"}


# -- summary key, determinism --------------------------------------------------------

def test_the_optional_summary_key(tmp_path):
    root = make_root(tmp_path / "ok", {
        "revit-core/lessons/a.md": lesson("revit-core/a", summary='"the short line"'),
        "revit-core/lessons/b.md": lesson("revit-core/b"),
    }, skills={"revit-core": "---\nname: revit-core\ndescription: d\n---\n\n## Lessons\n\n"
                             "- [a](lessons/a.md) - an older line\n- [b](lessons/b.md) - from the index\n"})
    library = load(root)
    assert library.get("revit-core/a").summary == "the short line" and library.get("revit-core/b").summary is None
    assert [x["summary"] for x in index(library)["items"]] == ["the short line", "from the index"]
    bad = make_root(tmp_path / "bad", {
        "revit-core/lessons/a.md": lesson("revit-core/a", summary="|\n  two\n  lines"),
        "revit-core/lessons/b.md": lesson("revit-core/b", summary='"' + "s" * 161 + '"'),
        "revit-core/lessons/c.md": lesson("revit-core/c", summary="42"),
        "revit-core/lessons/d.md": lesson("revit-core/d", summary='"' + "s" * 160 + '"'),
    })
    with pytest.raises(GuidanceError) as info:
        load(bad)
    problems = info.value.problems
    assert "revit-core/lessons/a.md: summary must be one line" in problems
    assert "revit-core/lessons/b.md: summary is longer than 160 characters" in problems
    assert "revit-core/lessons/c.md: summary must be a non-empty string" in problems
    assert not any(p.startswith("revit-core/lessons/d.md") for p in problems)


def test_the_index_summary_of_a_higher_layer_comes_from_its_own_skill(tmp_path, framework):
    (framework / "revit-core" / "SKILL.md").write_text(
        "---\nname: revit-core\ndescription: d\n---\n\n## Lessons\n\n- [a](lessons/a.md) - framework line\n",
        encoding="utf-8", newline="\n")
    company = layer_root(tmp_path, "company", {
        "revit-core/lessons/a.md": lesson("revit-core/a", title='"company a"'),
        "revit-core/SKILL.md": "---\nname: revit-core\ndescription: d\n---\n\n## Lessons\n\n- [a](lessons/a.md) - company line\n",
    })
    personal = layer_root(tmp_path, "personal", {"revit-core/lessons/b.md": lesson("revit-core/b", title='"personal b"')})
    library = load_layers(framework, company=company, personal=personal)
    summaries = {x["id"]: x["summary"] for x in index(library)["items"]}
    assert summaries["revit-core/a"] == "company line" and summaries["revit-core/b"] == "personal b"
    assert library.category("revit-core").layer == "framework"
    assert library.category("revit-core", "company").summary_of("a") == "company line"


def test_layered_libraries_are_deterministic(tmp_path, framework):
    company = layer_root(tmp_path, "company", {"revit-core/lessons/a.md": lesson("revit-core/a", title='"company a"')})
    personal = layer_root(tmp_path, "personal", {"revit-core/lessons/b.md": lesson("revit-core/b", title='"mine"')})
    first = load_layers(framework, company=company, personal=personal)
    second = load_layers(framework, company=company, personal=personal)
    for mode, args in (("recommend", {"stage": "orient"}), ("index", {}), ("read", {"ids": ["revit-core/a"]})):
        assert json.dumps(respond(first, mode, **args)) == json.dumps(respond(second, mode, **args))
    assert first.layers == second.layers and first.warnings == second.warnings
    assert first.overridden == second.overridden


def test_the_seed_content_loads_as_a_layered_library_unchanged():
    single, layered = load(CONTENT), load_layers(CONTENT)
    assert [x.id for x in layered.lessons] == [x.id for x in single.lessons]
    assert layered.config == single.config and layered.overridden == () and layered.layer_warnings == ()
    for mode, args in (("recommend", {"stage": "orient"}), ("index", {})):
        assert json.dumps(respond(layered, mode, **args)) == json.dumps(respond(single, mode, **args))


# -- the environment, the server -----------------------------------------------------

def test_layer_directories_come_from_the_environment(tmp_path):
    env = {"REVIT_BRIDGE_DATA_DIR": str(tmp_path / "data"), "REVIT_BRIDGE_GUIDANCE_COMPANY": str(tmp_path / "c"),
           "REVIT_BRIDGE_GUIDANCE_PROJECT": "  "}
    assert paths.guidance_layer_dirs(env) == {
        "company": tmp_path / "c", "project": None, "personal": tmp_path / "data" / "guidance"}
    assert paths.personal_guidance_dir(env) == tmp_path / "data" / "guidance"


def _call(name: str, args: dict) -> dict:
    return json.loads(asyncio.run(server.mcp.call_tool(name, args)).content[0].text)


def _wall_copy(title: str, **meta) -> str:
    """The seed wall lesson with another title (and front matter changes)."""
    text = (CONTENT / WALL_FILE).read_text(encoding="utf-8")
    head, body = text.split("\n---\n", 1)
    lines = [line for line in head.split("\n") if not line.startswith(("title:",) + tuple(f"{k}:" for k in meta))]
    lines.insert(4, f'title: "{title}"')
    lines.extend(f"{k}: {v}" for k, v in meta.items())
    return "\n".join(lines) + "\n---\n" + body


def test_acceptance_a_company_lesson_replaces_the_framework_one(tmp_path, monkeypatch):
    company = layer_root(tmp_path, "company", {WALL_FILE: _wall_copy("Company wall rule")})
    monkeypatch.setenv("REVIT_BRIDGE_GUIDANCE_COMPANY", str(company))
    monkeypatch.setattr(server, "_guidance_library", None)
    out = _call("get_guidance", {"stage": "fix", "errors": ["CS1061"], "symbols": ["Wall", "LocationCurve"]})
    first = out["items"][0]
    assert first["id"] == WALL and first["layer"] == "company" and first["title"] == "Company wall rule"
    assert first["hash"] == file_hash(company / WALL_FILE) and out["warnings"] == []
    listed = _call("get_guidance", {"mode": "index"})
    assert {"id": WALL, "title": guidance.load(CONTENT).get(WALL).title,
            "reason": "overridden_by:company"} in listed["omitted"]
    assert next(x for x in listed["items"] if x["id"] == WALL)["layer"] == "company"
    assert server.guidance_library().layers[1].state == "loaded"


def test_acceptance_enforced_company_lesson_beats_personal(tmp_path, monkeypatch):
    company = layer_root(tmp_path, "company", {WALL_FILE: _wall_copy("Company wall rule", enforced="true")})
    personal = paths.personal_guidance_dir()
    personal.mkdir(parents=True)
    (personal / "revit-geometry" / "lessons").mkdir(parents=True)
    (personal / WALL_FILE).write_text(_wall_copy("My wall rule"), encoding="utf-8", newline="\n")
    monkeypatch.setenv("REVIT_BRIDGE_GUIDANCE_COMPANY", str(company))
    monkeypatch.setattr(server, "_guidance_library", None)
    out = _call("get_guidance", {"stage": "fix", "errors": ["CS1061"], "symbols": ["Wall", "LocationCurve"]})
    assert out["items"][0]["layer"] == "company" and out["items"][0]["title"] == "Company wall rule"
    assert f"enforced_kept: {WALL} ignored personal" in out["warnings"]


def test_acceptance_a_bad_company_layer_is_rejected_alone(tmp_path, monkeypatch):
    company = layer_root(tmp_path, "company", {
        WALL_FILE: _wall_copy("Company wall rule"),
        "revit-geometry/lessons/broken.md": "no front matter\n",
    })
    monkeypatch.setenv("REVIT_BRIDGE_GUIDANCE_COMPANY", str(company))
    monkeypatch.setattr(server, "_guidance_library", None)
    out = _call("get_guidance", {"stage": "fix", "errors": ["CS1061"], "symbols": ["Wall", "LocationCurve"]})
    assert out["error"] is None and out["items"][0]["id"] == WALL and out["items"][0]["layer"] == "framework"
    assert len(out["warnings"]) == 1 and out["warnings"][0].startswith("layer_invalid: company: ")
    assert "revit-geometry/lessons/broken.md: no front matter" in out["warnings"][0]
    orient = _call("get_guidance", {"stage": "orient"})
    assert len(orient["items"]) == 5 and orient["warnings"] == out["warnings"]
    assert server.guidance_index().startswith("# guidance index")
