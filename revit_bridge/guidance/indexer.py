"""The generated parts of a guidance root: what ``revit-bridge guidance index`` writes.

``plan_index(root)`` computes, without writing, every file that differs from
what the generator would produce; ``apply(changes)`` writes them.

Every category directory's ``SKILL.md`` gets its ``## Lessons`` section
rebuilt from the lesson files: one line per lesson, in ``order`` then id
order, ``- [<slug>](lessons/<slug>.md) - <summary>`` where the summary is
the lesson's ``summary`` key or else its title; ``None yet.`` for a category
without lessons. Everything outside that section is kept as it is (a
SKILL.md without the section gets it appended). Line endings: a file keeps
its own; the comparison ignores them.

When the root sits in a plugin layout - a ``skills`` directory next to it,
as ``plugin/guidance`` and ``plugin/skills`` in the repository or
``guidance_content`` and ``skills`` in the wheel - two more parts follow:

- the native export (D9): the ``revit-core`` category directory, as the
  index makes it, copied byte for byte to ``../skills/revit-core/`` (files
  the source no longer has are removed); no other category is exported;
- the snippet contract block (P2): the lines between
  ``<!-- snippet-contract:begin -->`` and ``<!-- snippet-contract:end -->``
  in ``../skills/revit-bridge/SKILL.md`` become the ``## Contract`` note of
  ``revit-core/lessons/snippet-contract.md``.

The lessons are checked one by one first (the same checks as ``load``); a
root with a problem gets no changes, only the problems.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from revit_bridge.guidance.contract import BEGIN_MARK, CONTRACT_PATH, END_MARK, load_contract, replace_marked
from revit_bridge.guidance.loader import _LESSONS_HEADING, _NEXT_HEADING, SKILL_FILE, GuidanceError, scan
from revit_bridge.guidance.model import LAYER_FRAMEWORK, Lesson

NONE_YET = "None yet."
EXPORTED_CATEGORY = "revit-core"
SKILLS_DIR = "skills"
BRIDGE_SKILL = "revit-bridge"


@dataclass(frozen=True)
class Change:
    """A file the generator would write (``content``) or remove (``content`` None)."""

    path: Path
    content: bytes | None
    what: str                       # "lessons index", ...


def index_line(lesson: Lesson) -> str:
    """The lesson's line in its category's ``## Lessons`` section."""
    return f"- [{lesson.slug}](lessons/{lesson.slug}.md) - {lesson.summary or lesson.title}"


def lessons_section(lessons: list[Lesson]) -> list[str]:
    """The lines of a ``## Lessons`` section (without the heading) for one category's lessons."""
    ordered = sorted(lessons, key=lambda lesson: (lesson.order, lesson.id))
    return [index_line(lesson) for lesson in ordered] or [NONE_YET]


def _split_newlines(text: str) -> tuple[str, str]:
    """``(text with LF line ends, the file's own line end)``."""
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), newline


def regenerate_skill(text: str, lessons: list[Lesson]) -> str:
    """``text`` (a SKILL.md, LF line ends) with its ``## Lessons`` section rebuilt."""
    entries = lessons_section(lessons)
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if _LESSONS_HEADING.match(line)), None)
    if start is None:
        return text.rstrip("\n") + "\n\n## Lessons\n\n" + "\n".join(entries) + "\n"
    end = next((j for j in range(start + 1, len(lines)) if _NEXT_HEADING.match(lines[j])), None)
    if end is None:
        return "\n".join(lines[:start + 1] + [""] + entries + [""])
    return "\n".join(lines[:start + 1] + [""] + entries + [""] + lines[end:])


def _text_change(path: Path, desired_lf: str, current: str | None, newline: str, what: str) -> Change | None:
    """A change when ``desired_lf`` differs from the current text (line ends ignored)."""
    if current is not None and current.replace("\r\n", "\n") == desired_lf:
        return None
    return Change(path, desired_lf.replace("\n", newline).encode("utf-8"), what)


def _skill_changes(root: Path, lessons: list[Lesson]) -> tuple[list[Change], dict[str, str]]:
    """The ``## Lessons`` sections; also each category's desired SKILL.md text (LF)."""
    changes: list[Change] = []
    desired: dict[str, str] = {}
    by_category: dict[str, list[Lesson]] = {}
    for lesson in lessons:
        by_category.setdefault(lesson.category, []).append(lesson)
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        skill = directory / SKILL_FILE
        if directory.name.startswith((".", "__")) or not skill.is_file():
            continue
        current, newline = _split_newlines(skill.read_bytes().decode("utf-8"))
        desired[directory.name] = regenerate_skill(current, by_category.get(directory.name, []))
        change = _text_change(skill, desired[directory.name], current, newline, "lessons index")
        if change is not None:
            changes.append(change)
    return changes, desired


def _export_changes(root: Path, skills: Path, desired_skill: str | None) -> list[Change]:
    """``revit-core/`` copied to ``skills/revit-core/``, its SKILL.md as the index makes it."""
    source, target = root / EXPORTED_CATEGORY, skills / EXPORTED_CATEGORY
    wanted: dict[str, bytes] = {}
    for path in sorted((p for p in source.rglob("*") if p.is_file()),
                       key=lambda p: p.relative_to(source).as_posix()):    # the same order on every OS
        rel = path.relative_to(source).as_posix()
        content = path.read_bytes()
        if rel == SKILL_FILE and desired_skill is not None:
            newline = "\r\n" if b"\r\n" in content else "\n"
            content = desired_skill.replace("\n", newline).encode("utf-8")
        wanted[rel] = content
    changes = []
    for rel, content in wanted.items():
        path = target / rel
        if not path.is_file() or path.read_bytes() != content:
            changes.append(Change(path, content, "revit-core export"))
    if target.is_dir():
        for path in sorted((p for p in target.rglob("*") if p.is_file()),
                           key=lambda p: p.relative_to(target).as_posix()):
            if path.relative_to(target).as_posix() not in wanted:
                changes.append(Change(path, None, "revit-core export"))
    return changes


def _contract_change(root: Path, skill: Path, problems: list[str]) -> Change | None:
    """The snippet contract block of the revit-bridge skill."""
    try:
        contract = load_contract(root)
    except GuidanceError as e:
        problems.extend(e.problems)
        return None
    current, newline = _split_newlines(skill.read_bytes().decode("utf-8"))
    desired = replace_marked(current, contract.note())
    if desired is None:
        problems.append(f"{skill}: the {BEGIN_MARK} ... {END_MARK} markers are missing")
        return None
    return _text_change(skill, desired, current, newline, "snippet contract")


def plan_index(root: str | Path) -> tuple[list[Change], list[str]]:
    """``(changes, problems)`` for a guidance root: every generated file that is not up to date.
    With problems there are no changes."""
    root = Path(root)
    if not root.is_dir():
        return [], [f"{root}: not a directory"]
    problems: list[str] = []
    _, lessons, _ = scan(root, LAYER_FRAMEWORK, problems)
    if problems:
        return [], problems
    changes, desired = _skill_changes(root, lessons)
    skills = root.parent / SKILLS_DIR
    if skills.is_dir() and (root / EXPORTED_CATEGORY / SKILL_FILE).is_file():
        changes.extend(_export_changes(root, skills, desired.get(EXPORTED_CATEGORY)))
    bridge_skill = skills / BRIDGE_SKILL / SKILL_FILE
    if bridge_skill.is_file() and (root / CONTRACT_PATH).is_file():
        change = _contract_change(root, bridge_skill, problems)
        if change is not None:
            changes.append(change)
    return ([], problems) if problems else (changes, [])


def apply(changes: list[Change]) -> None:
    """Write (or remove) every planned file."""
    for change in changes:
        if change.content is None:
            if change.path.exists():
                change.path.unlink()
            continue
        change.path.parent.mkdir(parents=True, exist_ok=True)
        change.path.write_bytes(change.content)
