"""The bundled skills hold together: names, descriptions, the Components table, links that resolve, ASCII scripts."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from revit_bridge.guidance.loader import read_skill

SKILLS = Path(__file__).resolve().parents[1] / "plugin" / "skills"
FOLDERS = sorted(p for p in SKILLS.iterdir() if p.is_dir())
PHASES = ["revit-task-brief", "revit-addin-dev", "revit-addin-test", "revit-addin-release"]
ADDIN = ["revit-addin-dev", "revit-addin-test", "revit-addin-release"]

LINK = re.compile(r"\]\(([^)\s]+)\)")
# A file in another skill is named, never linked: "the `revit-addin-test` skill, `references/x.md`".
NAMED = re.compile(r"the `(revit-[a-z-]+)` skill, `((?:references|scripts|assets)/[^`]+)`")


def markdown_files():
    return sorted(p for p in SKILLS.rglob("*.md") if "__pycache__" not in p.parts)


def owner(path: Path) -> Path:
    return SKILLS / path.relative_to(SKILLS).parts[0]


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda p: p.name)
def test_every_skill_names_itself_and_keeps_its_description_short(folder):
    meta = read_skill(folder / "SKILL.md")
    assert meta, folder
    front = meta[0]
    assert front["name"] == folder.name
    description = " ".join(str(front["description"]).split())
    assert description and len(description) <= 1024, len(description)
    assert "<" not in description and ">" not in description


def test_the_skill_set_is_the_six_phase_skills():
    assert [p.name for p in FOLDERS] == ["revit-addin-dev", "revit-addin-release", "revit-addin-test", "revit-bridge",
                                         "revit-core", "revit-task-brief"]


@pytest.mark.parametrize("name", PHASES)
def test_every_phase_skill_says_what_it_does_without_and_with_the_bridge(name):
    text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
    heading = "## Use the bridge where it helps" if name == "revit-addin-dev" else "## Components"
    assert text.count(heading) == 1, name
    section = text.split(heading, 1)[1].split("\n## ", 1)[0]
    assert "| Without revit-bridge | With revit-bridge components |" in section, name
    assert "Without revit-bridge" in text and "With revit-bridge" in text


@pytest.mark.parametrize("name", ADDIN)
def test_the_add_in_skills_describe_themselves_in_both_languages_and_point_at_each_other(name):
    description = str(read_skill(SKILLS / name / "SKILL.md")[0]["description"])
    assert re.search(r"[一-鿿]", description) and re.search(r"[A-Za-z]{4,}", description), name
    for other in ADDIN:
        if other != name:
            assert other in description, (name, other)


def test_relative_links_resolve_inside_their_own_skill():
    # `](references/...)`, `](scripts/...)`, `](assets/...)` and same-folder links, resolved
    # from the linking file: the target exists and lies in the same skill folder, because a
    # skill may be installed without the others.
    broken, checked = [], set()
    for md in markdown_files():
        root = owner(md).resolve()
        for target in LINK.findall(md.read_text(encoding="utf-8")):
            if re.match(r"[a-z]+:", target) or target.startswith("#"):
                continue                                              # a URL or an anchor
            path = (md.parent / target.split("#", 1)[0]).resolve()
            checked.add(target.split("/", 1)[0])
            if not path.exists() or root not in path.parents:
                broken.append(f"{md.relative_to(SKILLS).as_posix()}: {target}")
    assert not broken, broken
    assert {"references", "assets"} <= checked                        # the check is not vacuous


def test_files_named_in_another_skill_exist_there():
    missing, named = [], 0
    for md in markdown_files():
        for skill, target in NAMED.findall(md.read_text(encoding="utf-8")):
            named += 1
            if not (SKILLS / skill / target).exists():
                missing.append(f"{md.relative_to(SKILLS).as_posix()}: {skill} {target}")
    assert not missing, missing
    assert named


def test_scripts_are_ascii_without_a_bom():
    scripts = sorted(p for p in SKILLS.glob("*/scripts/*") if p.is_file())
    assert scripts
    for script in scripts:
        data = script.read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf"), script
        assert all(byte < 128 for byte in data), script
