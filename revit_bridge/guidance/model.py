"""Guidance format v1: the frozen vocabulary, the data classes and version ranges.

A *lesson* is one Markdown file ``<category>/lessons/<slug>.md`` with a YAML
front matter; a *category* is one directory with a ``SKILL.md``; the
*config* is ``guidance.yaml`` at the root. Everything here is plain data:
no I/O, no environment, no network.

Version ranges (the lesson's ``revit`` key) are ``any`` or space-separated
comparisons such as ``>=2024 <2027``, ``==2026`` or ``<=2025.5``: a 4-digit
year, optionally ``.5``. A host-reported version (``revit_version``) is read
more loosely - the first ``YYYY`` or ``YYYY.N`` in the text, so
``"2026.5"``, ``"2026"`` and ``"Autodesk Revit 2026.5"`` all work.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SCHEMA_VERSION = 1
LAYER_FRAMEWORK = "framework"

# D13: the category table. Category ids are the namespace of lesson ids, so
# renaming one is a migration; new categories go through planning.
CATEGORIES: tuple[str, ...] = (
    "revit-core",
    "revit-collect",
    "revit-params",
    "revit-geometry",
    "revit-families",
    "revit-elements",
    "revit-structure",
    "revit-mep",
    "revit-views",
    "revit-addin",
    "revit-verify",
)

STAGES: tuple[str, ...] = ("orient", "write", "fix", "port", "verify")
KINDS: tuple[str, ...] = ("pitfall", "quirk", "convention", "fact")
TRIGGER_KEYS: tuple[str, ...] = ("categories", "symbols", "errors", "exceptions")

DEFAULT_ORDER = 100
MAX_TITLE_CHARS = 120
MAX_BODY_BYTES = 16384

SLUG_RE = re.compile(r"^[a-z0-9-]+$")
CATEGORY_TRIGGER_RE = re.compile(r"^OST_[A-Za-z0-9_]+$")
ERROR_RE = re.compile(r"^CS\d{4}$")
# An API identifier, optionally dotted (Namespace.Type, Type.Member).
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")

_TERM_RE = re.compile(r"^(>=|<=|==|<|>)(\d{4})(?:\.(5))?$")
_HOST_VERSION_RE = re.compile(r"(?<!\d)(\d{4})(?:\.(\d+))?(?!\d)")

Version = tuple[int, int]


def parse_range(text: str) -> tuple[tuple[str, Version], ...] | None:
    """The comparisons of a version range; ``()`` for ``any``; None when malformed."""
    if not isinstance(text, str):
        return None
    words = text.split()
    if words == ["any"]:
        return ()
    if not words:
        return None
    terms = []
    for word in words:
        match = _TERM_RE.match(word)
        if match is None:
            return None
        terms.append((match.group(1), (int(match.group(2)), 5 if match.group(3) else 0)))
    return tuple(terms)


def parse_version(text: str | None) -> Version | None:
    """A host-reported Revit version as ``(year, update)``; None when there is none."""
    if not isinstance(text, str):
        return None
    match = _HOST_VERSION_RE.search(text)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2) or 0)


def _compare(op: str, left: Version, right: Version) -> bool:
    if op == ">=":
        return left >= right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == "<":
        return left < right
    return left == right


@dataclass(frozen=True)
class VersionRange:
    """A parsed ``revit`` range; ``terms`` is empty for ``any``."""

    text: str
    terms: tuple[tuple[str, Version], ...]

    @classmethod
    def parse(cls, text: str) -> "VersionRange":
        terms = parse_range(text)
        if terms is None:
            raise ValueError(f"bad version range: {text!r}")
        return cls(text=text, terms=terms)

    @property
    def is_any(self) -> bool:
        return not self.terms

    def contains(self, version: Version | str) -> bool:
        """True when every comparison holds for ``version`` (always for ``any``)."""
        if isinstance(version, str):
            parsed = parse_version(version)
            if parsed is None:
                raise ValueError(f"not a Revit version: {version!r}")
            version = parsed
        return all(_compare(op, version, bound) for op, bound in self.terms)


@dataclass(frozen=True)
class Triggers:
    categories: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    exceptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Lesson:
    """One lesson file. ``hash`` is the first 16 hex digits of sha256 over the file's bytes."""

    id: str
    category: str
    slug: str
    revision: int
    title: str
    stage: tuple[str, ...]
    kind: str
    revit: VersionRange
    triggers: Triggers
    order: int
    supersedes: tuple[str, ...]
    superseded_by: str | None
    enforced: bool
    body: str
    hash: str
    layer: str
    path: str                     # relative to the root, "/"-separated

    @property
    def body_bytes(self) -> int:
        return len(self.body.encode("utf-8"))


@dataclass(frozen=True)
class Category:
    """A category directory: its SKILL.md front matter and the one-line summaries of its index."""

    id: str
    name: str
    description: str
    summaries: tuple[tuple[str, str], ...] = ()   # (slug, summary) from the "Lessons" section

    def summary_of(self, slug: str) -> str | None:
        for key, text in self.summaries:
            if key == slug:
                return text
        return None


@dataclass(frozen=True)
class Budget:
    items: int = 5
    utf8_bytes: int = 12000
    fix_inline_items: int = 2
    fix_inline_bytes: int = 2400


@dataclass(frozen=True)
class Config:
    """``guidance.yaml``."""

    schema_version: int
    order: tuple[str, ...]
    always: tuple[str, ...]
    budget: Budget = field(default_factory=Budget)

    def category_rank(self, category: str) -> int:
        """Sort key: position in ``order``; categories not listed follow, in table order."""
        if category in self.order:
            return self.order.index(category)
        return len(self.order) + (CATEGORIES.index(category) if category in CATEGORIES else len(CATEGORIES))
