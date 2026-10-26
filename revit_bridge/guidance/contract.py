"""The execute_code snippet contract, from its one source (P2): the lesson
``revit-core/snippet-contract`` of the framework layer.

Four places render it instead of repeating it:

- the "Snippet contract" note of the model instructions (both renderings),
  from the lesson's ``## Contract`` section: at most six lines, rendered as
  one Markdown list item (``note()``: the first line after ``- ``, the
  others indented by two spaces);
- the contract sentence of the ``check_code`` and ``execute_code`` tool
  descriptions, from the lesson's ``summary`` key;
- the block between ``<!-- snippet-contract:begin -->`` and
  ``<!-- snippet-contract:end -->`` in the ``revit-bridge`` skill, written by
  ``revit-bridge guidance index`` (``replace_marked``);
- the usings named in ``check_code``'s CS0246 hint (``usings()``: the list
  after the word "usings" up to the next semicolon, the lines joined).

``load_contract(root)`` reads only that one file (checked like any lesson)
and raises ``GuidanceError`` when it is missing, invalid, without
``summary`` or without a usable ``## Contract`` section.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from revit_bridge.guidance.loader import GuidanceError, load_lesson
from revit_bridge.guidance.model import LAYER_FRAMEWORK

CONTRACT_ID = "revit-core/snippet-contract"
CONTRACT_PATH = "revit-core/lessons/snippet-contract.md"
MAX_CONTRACT_LINES = 6
BEGIN_MARK = "<!-- snippet-contract:begin -->"
END_MARK = "<!-- snippet-contract:end -->"

_USINGS = re.compile(r"\busings\s+([^;]+);")
_CONTRACT_HEADING = re.compile(r"^##\s+Contract\s*$")
_NEXT_HEADING = re.compile(r"^#{1,2}\s")


@dataclass(frozen=True)
class SnippetContract:
    """The contract as the lesson states it: ``summary`` and the ``## Contract`` lines."""

    summary: str
    lines: tuple[str, ...]
    path: str

    def note(self) -> str:
        """The ``## Contract`` lines as one Markdown list item, ending with a newline."""
        return "".join(("- " if i == 0 else "  ") + line + "\n" for i, line in enumerate(self.lines))

    def usings(self) -> str | None:
        """The default usings as the contract lists them (``System, System.Linq, ...``), or None."""
        found = _USINGS.search(" ".join(line.strip() for line in self.lines))
        return " ".join(found.group(1).split()) if found else None


def contract_lines(body: str) -> tuple[str, ...] | None:
    """The non-empty lines of the ``## Contract`` section of a lesson body, or None without one."""
    lines = body.split("\n")
    start = next((i for i, line in enumerate(lines) if _CONTRACT_HEADING.match(line)), None)
    if start is None:
        return None
    out = []
    for line in lines[start + 1:]:
        if _NEXT_HEADING.match(line):
            break
        if line.strip():
            out.append(line.rstrip())
    return tuple(out)


def load_contract(root: str | Path) -> SnippetContract:
    """The snippet contract of the guidance root ``root``; raises ``GuidanceError``."""
    root = Path(root)
    path = root / CONTRACT_PATH
    if not path.is_file():
        raise GuidanceError([f"{CONTRACT_PATH}: missing"])
    problems: list[str] = []
    lesson, _ = load_lesson(root, path, LAYER_FRAMEWORK, problems)
    if lesson is None:
        raise GuidanceError(problems)
    if not lesson.summary:
        problems.append(f"{CONTRACT_PATH}: summary is required (the tool descriptions use it)")
    lines = contract_lines(lesson.body)
    if lines is None:
        problems.append(f"{CONTRACT_PATH}: no ## Contract section")
    elif not lines or len(lines) > MAX_CONTRACT_LINES:
        problems.append(f"{CONTRACT_PATH}: the ## Contract section has {len(lines)} lines "
                        f"(1 to {MAX_CONTRACT_LINES})")
    if problems:
        raise GuidanceError(problems)
    return SnippetContract(summary=lesson.summary, lines=lines, path=str(path))


def replace_marked(text: str, note: str) -> str | None:
    """``text`` (LF line ends) with the lines between the two markers replaced by ``note``;
    None when the markers are missing or out of order."""
    begin = text.find(BEGIN_MARK)
    end = text.find(END_MARK, begin + len(BEGIN_MARK)) if begin >= 0 else -1
    if begin < 0 or end < 0:
        return None
    return text[:begin + len(BEGIN_MARK)] + "\n" + note + text[end:]
