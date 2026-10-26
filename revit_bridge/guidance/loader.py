"""Load and validate guidance: one root (format v1), or the four layers (D11).

``load(root)`` reads ``guidance.yaml``, every category directory's
``SKILL.md`` and every ``<category>/lessons/<slug>.md``, checks them all and
returns an immutable ``Library``. Any problem rejects the whole root: a
``GuidanceError`` carries every problem found, not just the first.

Checks: ``guidance.yaml`` (schema_version 1, ``order`` names only known
categories, ``always`` is a subset of ``order``, positive budget numbers);
category directories are in the category table; per lesson: schema_version,
the id is ``<directory>/<file name without .md>`` with a ``[a-z0-9-]+`` slug,
every key's type and value, the version range syntax, the trigger rules (not
all four lists empty; ``errors`` only together with ``symbols``), unknown
keys, the optional one-line ``summary`` (at most 160 characters), body size;
across lessons: duplicate ids, ``supersedes`` / ``superseded_by`` targets
that exist and are not the lesson itself, no ``supersedes`` cycle.

``load_layers(framework, company=..., project=..., personal=...)`` stacks the
layers, lowest first. The framework root is loaded with ``load`` and must be
valid (else ``GuidanceError``). Every other layer is a directory with the
same structure and an optional ``guidance.yaml`` that may set only
``schema_version``, ``order`` and ``always`` (no ``budget``): a layer that is
not configured (None) or not a directory is skipped; one that fails
validation is rejected as a whole, with a ``layer_invalid: <layer>: <first
problem>`` warning, and the other layers still load. A higher layer's
``order`` reorders the categories it names (they take the slots those
categories had, in its order), appends the ones not yet listed and leaves the
rest in place; ``always`` only adds. The same id in several layers: the
highest layer's lesson replaces the others whole (they are kept in
``Library.overridden``), except that a company lesson with ``enforced: true``
is not replaced by project or personal (warning ``enforced_kept: <id> ignored
<layer>``). ``enforced`` in the framework layer is ignored with a warning; in
the project or personal layer it is a validation problem. ``supersedes`` and
``superseded_by`` targets may live in any loaded layer; a ``supersedes``
cycle among the lessons in force rejects the whole library.

Nothing here reads the environment or the network: the caller passes the
layer directories.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from revit_bridge.guidance.model import (
    BODY_WRITING_BYTES,
    CATEGORIES,
    CATEGORY_TRIGGER_RE,
    DEFAULT_ORDER,
    ERROR_RE,
    IDENTIFIER_RE,
    KINDS,
    LAYER_COMPANY,
    LAYER_FRAMEWORK,
    LAYER_INVALID,
    LAYER_LOADED,
    LAYER_MISSING,
    LAYER_PERSONAL,
    LAYER_PROJECT,
    LAYER_UNSET,
    LAYERS,
    MAX_BODY_BYTES,
    MAX_SUMMARY_CHARS,
    MAX_TITLE_CHARS,
    SCHEMA_VERSION,
    SLUG_RE,
    STAGES,
    TRIGGER_KEYS,
    Budget,
    Category,
    Config,
    Layer,
    Lesson,
    Triggers,
    VersionRange,
    parse_range,
)

CONFIG_FILE = "guidance.yaml"
SKILL_FILE = "SKILL.md"
LESSONS_DIR = "lessons"

LESSON_KEYS = frozenset({
    "schema_version", "id", "revision", "title", "summary", "stage", "kind", "revit", "triggers",
    "order", "supersedes", "superseded_by", "enforced",
})
REQUIRED_KEYS = ("schema_version", "id", "revision", "title", "stage", "kind", "revit", "triggers")
CONFIG_KEYS = frozenset({"schema_version", "order", "always", "budget"})
LAYER_CONFIG_KEYS = frozenset({"schema_version", "order", "always"})
BUDGET_KEYS = ("items", "utf8_bytes", "fix_inline_items", "fix_inline_bytes")

_LESSONS_HEADING = re.compile(r"^##\s+Lessons\s*$", re.IGNORECASE)
_NEXT_HEADING = re.compile(r"^#{1,2}\s")
_INDEX_LINE = re.compile(r"^\s*[-*]\s+\[[^\]]*\]\(lessons/([a-z0-9-]+)\.md\)\s*(?:[-:]\s*)?(.*?)\s*$")


class GuidanceError(Exception):
    """A guidance root failed validation; ``problems`` lists every problem found."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems) if self.problems else "invalid guidance")


@dataclass(frozen=True)
class Library:
    """A validated library: one root, or the layers merged. Lessons are the ones in force,
    sorted: category order, ``order``, ``id``.

    ``warnings`` are problems that do not stop the load: a body over the
    ``BODY_WRITING_BYTES`` writing rule, and the layer warnings.
    ``layer_warnings`` are only the layer warnings (``layer_invalid``,
    ``enforced_kept``, ``enforced_ignored``): the ones tool replies carry.
    ``layers`` says what happened to each layer; ``overridden`` holds each
    lesson replaced by a higher layer's lesson of the same id, with that
    layer's name.
    """

    root: str
    layer: str
    config: Config
    categories: tuple[Category, ...]
    lessons: tuple[Lesson, ...]
    warnings: tuple[str, ...] = ()
    layers: tuple[Layer, ...] = ()
    overridden: tuple[tuple[Lesson, str], ...] = ()
    layer_warnings: tuple[str, ...] = ()
    # Set in __post_init__; no default (Python 3.11 refuses an unhashable dataclass default).
    _by_id: Mapping[str, Lesson] = field(init=False, repr=False, compare=False)
    _superseded: Mapping[str, str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        by_id = {lesson.id: lesson for lesson in self.lessons}
        superseded: dict[str, str] = {}
        for lesson in self.lessons:                       # library order: the first one wins
            for target in lesson.supersedes:
                if target in by_id:
                    superseded.setdefault(target, lesson.id)
        object.__setattr__(self, "_by_id", MappingProxyType(by_id))
        object.__setattr__(self, "_superseded", MappingProxyType(superseded))

    def get(self, lesson_id: str) -> Lesson | None:
        return self._by_id.get(lesson_id)

    def superseded_by(self, lesson_id: str) -> str | None:
        """The id of the lesson in force whose ``supersedes`` names this one, or None."""
        return self._superseded.get(lesson_id)

    def category(self, category_id: str, layer: str | None = None) -> Category | None:
        """A category directory: the lowest layer's, or the given layer's."""
        for category in self.categories:
            if category.id == category_id and (layer is None or category.layer == layer):
                return category
        return None


# -- YAML ----------------------------------------------------------------------------

class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses a mapping with the same key twice."""


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False):
    loader.flatten_mapping(node)
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in seen
        except TypeError:
            continue                                   # unhashable: construct_mapping reports it
        if duplicate:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark)
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _yaml(text: str) -> Any:
    return yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - a SafeLoader subclass


def split_front_matter(text: str) -> tuple[str, str] | None:
    """``(front matter, body)`` of a Markdown file that starts with a ``---`` block, else None."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return None


def _clean_body(body: str) -> str:
    return body.lstrip("\n").rstrip() + "\n" if body.strip() else ""


# -- small type checks ---------------------------------------------------------------

def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _str_list(value: Any, what: str, problems: list[str], pattern: re.Pattern | None = None,
              describe: str = "") -> tuple[str, ...]:
    """A list of strings (None counts as empty); each problem is appended with ``what``."""
    if value is None:
        return ()
    if not isinstance(value, list):
        problems.append(f"{what} must be a list")
        return ()
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            problems.append(f"{what}: {item!r} is not a non-empty string")
            continue
        if pattern is not None and not pattern.match(item):
            problems.append(f"{what}: {item!r} is not {describe}")
            continue
        out.append(item)
    return tuple(out)


# -- guidance.yaml -------------------------------------------------------------------

def _read_config(root: Path, problems: list[str]) -> dict | None:
    path = root / CONFIG_FILE
    try:
        data = _yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        problems.append(f"{CONFIG_FILE}: cannot be read: {e}")
        return None
    if not isinstance(data, dict):
        problems.append(f"{CONFIG_FILE}: must be a mapping")
        return None
    return data


def _order_and_always(data: dict, local: list[str], *, order_required: bool) -> tuple[tuple, tuple]:
    order = _str_list(data.get("order"), "order", local)
    if order_required and "order" not in data:
        local.append("order is required")
    for category in order:
        if category not in CATEGORIES:
            local.append(f"order: unknown category {category!r}")
    if len(set(order)) != len(order):
        local.append("order: a category is listed twice")
    always = _str_list(data.get("always"), "always", local)
    if len(set(always)) != len(always):
        local.append("always: a category is listed twice")
    return order, always


def _load_config(root: Path, problems: list[str]) -> Config | None:
    """The framework's ``guidance.yaml``: required, the only one that sets ``budget``."""
    if not (root / CONFIG_FILE).is_file():
        problems.append(f"{CONFIG_FILE}: missing")
        return None
    data = _read_config(root, problems)
    if data is None:
        return None
    local: list[str] = []
    for key in sorted(set(data) - CONFIG_KEYS, key=str):
        local.append(f"unknown key {key!r}")
    if not (_is_int(data.get("schema_version")) and data.get("schema_version") == SCHEMA_VERSION):
        local.append(f"schema_version must be {SCHEMA_VERSION}")
    order, always = _order_and_always(data, local, order_required=True)
    for category in always:
        if category not in order:
            local.append(f"always: {category!r} is not in order")
    budget_data = data.get("budget", {})
    budget_values = {}
    if not isinstance(budget_data, dict):
        local.append("budget must be a mapping")
        budget_data = {}
    for key in sorted(set(budget_data) - set(BUDGET_KEYS), key=str):
        local.append(f"budget: unknown key {key!r}")
    defaults = Budget()
    for key in BUDGET_KEYS:
        value = budget_data.get(key, getattr(defaults, key))
        if not _is_int(value) or value < 1:
            local.append(f"budget.{key} must be a positive integer")
            value = getattr(defaults, key)
        budget_values[key] = value
    problems.extend(f"{CONFIG_FILE}: {p}" for p in local)
    if local:
        return None
    return Config(schema_version=SCHEMA_VERSION, order=order, always=always, budget=Budget(**budget_values))


def _load_layer_config(root: Path, problems: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """A higher layer's optional ``guidance.yaml``: ``(order, always)``, both empty without one."""
    if not (root / CONFIG_FILE).is_file():
        return (), ()
    data = _read_config(root, problems)
    if data is None:
        return (), ()
    local: list[str] = []
    if "budget" in data:
        local.append("budget is set only in the framework layer")
    for key in sorted(set(data) - LAYER_CONFIG_KEYS - {"budget"}, key=str):
        local.append(f"unknown key {key!r}")
    if not (_is_int(data.get("schema_version")) and data.get("schema_version") == SCHEMA_VERSION):
        local.append(f"schema_version must be {SCHEMA_VERSION}")
    order, always = _order_and_always(data, local, order_required=False)
    for category in always:
        if category not in CATEGORIES:
            local.append(f"always: unknown category {category!r}")
    problems.extend(f"{CONFIG_FILE}: {p}" for p in local)
    return order, always


def merge_order(lower: tuple[str, ...], higher: tuple[str, ...]) -> tuple[str, ...]:
    """A higher layer's ``order`` on top of the lower one: the categories it names that are
    already listed take the slots they had, in its order; the others are appended; the
    categories it does not name stay where they are."""
    named = [category for category in higher if category in lower]
    slots = iter(named)
    merged = [next(slots) if category in named else category for category in lower]
    return tuple(merged) + tuple(category for category in higher if category not in lower)


def merge_always(lower: tuple[str, ...], higher: tuple[str, ...]) -> tuple[str, ...]:
    """``always`` only grows: the lower list, then what the higher one adds."""
    return tuple(lower) + tuple(category for category in higher if category not in lower)


# -- SKILL.md ------------------------------------------------------------------------

def read_skill(path: Path) -> tuple[dict, str] | None:
    """The front matter mapping and body of a SKILL.md, or None when it has none."""
    try:
        split = split_front_matter(path.read_text(encoding="utf-8"))
        if split is None:
            return None
        meta = _yaml(split[0])
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    return (meta, split[1]) if isinstance(meta, dict) else None


def index_summaries(body: str) -> tuple[tuple[str, str], ...]:
    """``(slug, summary)`` for each ``- [..](lessons/<slug>.md) - summary`` line under ``## Lessons``."""
    out = []
    inside = False
    for line in body.split("\n"):
        if _LESSONS_HEADING.match(line):
            inside = True
            continue
        if inside and _NEXT_HEADING.match(line):
            break
        if inside:
            match = _INDEX_LINE.match(line)
            if match and match.group(2):
                out.append((match.group(1), match.group(2)))
    return tuple(out)


def _load_category(directory: Path, layer: str) -> Category:
    skill = read_skill(directory / SKILL_FILE)
    if skill is None:
        return Category(id=directory.name, name=directory.name, description="", layer=layer)
    meta, body = skill
    return Category(id=directory.name, name=str(meta.get("name") or directory.name),
                    description=str(meta.get("description") or ""), summaries=index_summaries(body),
                    layer=layer)


# -- lessons -------------------------------------------------------------------------

def load_lesson(root: Path, path: Path, layer: str,
                problems: list[str]) -> tuple[Lesson | None, dict | None]:
    """One lesson file checked on its own: the lesson (None when it has problems, which are
    appended to ``problems``) and its parsed front matter (None when unreadable)."""
    rel = path.relative_to(root).as_posix()
    local: list[str] = []
    category, slug = path.parent.parent.name, path.stem
    if category not in CATEGORIES:
        local.append(f"category {category!r} is not in the category table")
    if not SLUG_RE.match(slug):
        local.append(f"file name {path.name!r}: the slug must match [a-z0-9-]+")
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        problems.append(f"{rel}: cannot be read: {e}")
        return None, None
    split = split_front_matter(text)
    if split is None:
        problems.append(f"{rel}: no front matter (the file must start with a --- block)")
        return None, None
    try:
        meta = _yaml(split[0])
    except yaml.YAMLError as e:
        problems.append(f"{rel}: front matter is not valid YAML: {e}")
        return None, None
    if not isinstance(meta, dict):
        problems.append(f"{rel}: front matter must be a mapping")
        return None, None

    for key in sorted(set(meta) - LESSON_KEYS, key=str):
        local.append(f"unknown key {key!r}")
    for key in REQUIRED_KEYS:
        if key not in meta:
            local.append(f"{key} is required")

    if "schema_version" in meta and not (_is_int(meta["schema_version"]) and meta["schema_version"] == SCHEMA_VERSION):
        local.append(f"schema_version must be {SCHEMA_VERSION}")

    lesson_id = meta.get("id")
    expected = f"{category}/{slug}"
    if "id" in meta:
        if not isinstance(lesson_id, str):
            local.append("id must be a string")
            lesson_id = None
        elif lesson_id != expected:
            local.append(f"id {lesson_id!r} does not match its path (expected {expected!r})")

    revision = meta.get("revision")
    if "revision" in meta and not (_is_int(revision) and revision >= 1):
        local.append("revision must be an integer >= 1")

    title = meta.get("title")
    if "title" in meta:
        if not isinstance(title, str) or not title.strip():
            local.append("title must be a non-empty string")
        elif len(title) > MAX_TITLE_CHARS:
            local.append(f"title is longer than {MAX_TITLE_CHARS} characters")

    summary = meta.get("summary")
    if "summary" in meta:
        if not isinstance(summary, str) or not summary.strip():
            local.append("summary must be a non-empty string")
            summary = None
        elif summary.splitlines()[0] != summary:
            local.append("summary must be one line")
        elif len(summary.strip()) > MAX_SUMMARY_CHARS:
            local.append(f"summary is longer than {MAX_SUMMARY_CHARS} characters")

    stage: tuple[str, ...] = ()
    if "stage" in meta:
        stage = _str_list(meta["stage"], "stage", local)
        if not meta["stage"]:
            local.append("stage must not be empty")
        for item in stage:
            if item not in STAGES:
                local.append(f"stage: unknown stage {item!r} (allowed: {', '.join(STAGES)})")
        if len(set(stage)) != len(stage):
            local.append("stage: a stage is listed twice")

    kind = meta.get("kind")
    if "kind" in meta and kind not in KINDS:
        local.append(f"kind must be one of {', '.join(KINDS)}")

    revit = meta.get("revit")
    if "revit" in meta and parse_range(revit) is None:
        local.append(f"revit: bad version range {revit!r} (use 'any' or comparisons such as '>=2024 <2027')")

    triggers = Triggers()
    if "triggers" in meta:
        data = meta["triggers"]
        if not isinstance(data, dict):
            local.append("triggers must be a mapping")
        else:
            for key in sorted(set(data) - set(TRIGGER_KEYS), key=str):
                local.append(f"triggers: unknown key {key!r}")
            triggers = Triggers(
                categories=_str_list(data.get("categories"), "triggers.categories", local,
                                     CATEGORY_TRIGGER_RE, "an OST_* category name"),
                symbols=_str_list(data.get("symbols"), "triggers.symbols", local,
                                  IDENTIFIER_RE, "an API identifier"),
                errors=_str_list(data.get("errors"), "triggers.errors", local,
                                 ERROR_RE, "a compiler error code such as CS1061"),
                exceptions=_str_list(data.get("exceptions"), "triggers.exceptions", local,
                                     IDENTIFIER_RE, "an exception type name"),
            )
            if not any(data.get(key) for key in TRIGGER_KEYS):
                local.append("triggers: categories, symbols, errors and exceptions are all empty")
            if data.get("errors") and not data.get("symbols"):
                local.append("triggers: errors need symbols (an error code counts only together with a symbol)")

    order = meta.get("order", DEFAULT_ORDER)
    if not _is_int(order):
        local.append("order must be an integer")
        order = DEFAULT_ORDER

    supersedes = _str_list(meta.get("supersedes"), "supersedes", local)
    superseded_by = meta.get("superseded_by")
    if superseded_by is not None and (not isinstance(superseded_by, str) or not superseded_by.strip()):
        local.append("superseded_by must be a lesson id or null")
        superseded_by = None

    enforced = meta.get("enforced", False)
    if not isinstance(enforced, bool):
        local.append("enforced must be true or false")
        enforced = False
    elif enforced and layer in (LAYER_PROJECT, LAYER_PERSONAL):
        local.append(f"enforced: true is allowed only in the company layer, not the {layer} layer")

    body = _clean_body(split[1])
    size = len(body.encode("utf-8"))
    if size > MAX_BODY_BYTES:
        local.append(f"body is {size} UTF-8 bytes; the limit is {MAX_BODY_BYTES}")

    problems.extend(f"{rel}: {p}" for p in local)
    if local:
        return None, meta
    lesson = Lesson(
        id=lesson_id, category=category, slug=slug, revision=revision, title=title.strip(),
        stage=stage, kind=kind, revit=VersionRange.parse(revit), triggers=triggers, order=order,
        supersedes=supersedes, superseded_by=superseded_by, enforced=enforced, body=body,
        hash=hashlib.sha256(raw).hexdigest()[:16], layer=layer, path=rel,
        summary=summary.strip() if isinstance(summary, str) else None,
    )
    return lesson, meta


def _own_id(meta: dict | None) -> str | None:
    value = meta.get("id") if meta else None
    return value if isinstance(value, str) else None


def _references(meta: dict | None) -> list[tuple[str, str]]:
    """``(key, target)`` for each ``supersedes`` / ``superseded_by`` target a file declares."""
    if not meta:
        return []
    supersedes = meta.get("supersedes") if isinstance(meta.get("supersedes"), list) else []
    out = [("supersedes", t) for t in supersedes if isinstance(t, str)]
    if isinstance(meta.get("superseded_by"), str):
        out.append(("superseded_by", meta["superseded_by"]))
    return out


def _duplicates_and_self(declared: list[tuple[str, dict | None]], problems: list[str]) -> set[str]:
    """Duplicate ids and references to the file's own id, on what each file declares (a file
    with other problems still takes part); returns the declared ids."""
    where: dict[str, list[str]] = {}
    for rel, meta in declared:
        if _own_id(meta) is not None:
            where.setdefault(_own_id(meta), []).append(rel)
    for lesson_id, paths in sorted(where.items()):
        if len(paths) > 1:
            problems.append(f"duplicate id {lesson_id!r} in {', '.join(paths)}")
    for rel, meta in declared:
        for key, target in _references(meta):
            if target == _own_id(meta):
                problems.append(f"{rel}: supersedes itself" if key == "supersedes"
                                else f"{rel}: superseded_by points at itself")
    return set(where)


def _dangling(declared: list[tuple[str, dict | None]], known: set[str]) -> list[str]:
    """References to ids that no loaded lesson declares."""
    problems = []
    for rel, meta in declared:
        for key, target in _references(meta):
            if target != _own_id(meta) and target not in known:
                problems.append(f"{rel}: {key} unknown id {target!r}")
    return problems


def supersedes_cycles(lessons: Iterable[Lesson]) -> list[str]:
    """A problem for each group of lessons whose ``supersedes`` form a cycle."""
    edges = {lesson.id: tuple(lesson.supersedes) for lesson in lessons}
    edges = {node: tuple(t for t in targets if t in edges) for node, targets in edges.items()}

    def reach(start: str) -> set[str]:
        seen: set[str] = set()
        stack = list(edges[start])
        while stack:
            node = stack.pop()
            if node not in seen:
                seen.add(node)
                stack.extend(edges[node])
        return seen

    reachable = {node: reach(node) for node in sorted(edges)}
    problems, done = [], set()
    for node in sorted(edges):
        if node in done or node not in reachable[node]:
            continue
        group = sorted(other for other in reachable[node] if node in reachable[other])
        done.update(group)
        problems.append(f"supersedes form a cycle: {', '.join(group)}")
    return problems


def _writing_warnings(lessons: Iterable[Lesson]) -> list[str]:
    return [f"{lesson.path}: body is {lesson.body_bytes} UTF-8 bytes, over the "
            f"{BODY_WRITING_BYTES}-byte writing rule" for lesson in lessons
            if lesson.body_bytes > BODY_WRITING_BYTES]


def scan(root: Path, layer: str, problems: list[str]) -> tuple[list[Category], list[Lesson], list]:
    """Category directories and lesson files of one root, each file checked on its own:
    ``(categories, valid lessons, [(path, front matter)] of every lesson file)``."""
    categories: list[Category] = []
    lesson_paths: list[Path] = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        if directory.name.startswith((".", "__")):
            continue
        if directory.name not in CATEGORIES:
            problems.append(f"{directory.name}/: not a category in the category table")
        else:
            categories.append(_load_category(directory, layer))
        lessons_dir = directory / LESSONS_DIR
        if lessons_dir.is_dir():
            lesson_paths.extend(sorted(lessons_dir.glob("*.md")))
    lessons: list[Lesson] = []
    declared: list[tuple[str, dict | None]] = []
    for path in lesson_paths:
        lesson, meta = load_lesson(root, path, layer, problems)
        declared.append((path.relative_to(root).as_posix(), meta))
        if lesson is not None:
            lessons.append(lesson)
    return categories, lessons, declared


def load(root: str | Path, layer: str = LAYER_FRAMEWORK) -> Library:
    """Validate and load one guidance root; raises ``GuidanceError`` with every problem."""
    root = Path(root)
    problems: list[str] = []
    if not root.is_dir():
        raise GuidanceError([f"{root}: not a directory"])

    config = _load_config(root, problems)
    categories, lessons, declared = scan(root, layer, problems)
    known = _duplicates_and_self(declared, problems)
    problems.extend(_dangling(declared, known))
    if not problems:
        problems.extend(supersedes_cycles(lessons))

    if problems:
        raise GuidanceError(problems)
    assert config is not None
    lessons.sort(key=lambda lesson: (config.category_rank(lesson.category), lesson.order, lesson.id))
    categories.sort(key=lambda category: (config.category_rank(category.id), category.id))
    return Library(root=str(root), layer=layer, config=config, categories=tuple(categories),
                   lessons=tuple(lessons), warnings=tuple(_writing_warnings(lessons)),
                   layers=(Layer(layer, str(root), LAYER_LOADED, len(lessons)),))


# -- layers --------------------------------------------------------------------------

@dataclass
class _Upper:
    """A higher layer that passed its own checks."""

    name: str
    root: Path
    order: tuple[str, ...]
    always: tuple[str, ...]
    categories: list[Category]
    lessons: list[Lesson]
    declared: list[tuple[str, dict | None]]


def _read_upper(root: Path, name: str, problems: list[str]) -> _Upper:
    order, always = _load_layer_config(root, problems)
    categories, lessons, declared = scan(root, name, problems)
    _duplicates_and_self(declared, problems)
    return _Upper(name=name, root=root, order=order, always=always, categories=categories,
                  lessons=lessons, declared=declared)


def load_layers(framework: str | Path, *, company: str | Path | None = None,
                project: str | Path | None = None, personal: str | Path | None = None) -> Library:
    """The four layers merged (see the module doc). Raises ``GuidanceError`` only when the
    framework root is invalid or the lessons in force form a ``supersedes`` cycle."""
    base = load(framework, LAYER_FRAMEWORK)
    states: dict[str, Layer] = {LAYER_FRAMEWORK: base.layers[0]}
    invalid: dict[str, list[str]] = {}
    read: list[tuple[_Upper, list[str]]] = []
    for name, root in ((LAYER_COMPANY, company), (LAYER_PROJECT, project), (LAYER_PERSONAL, personal)):
        if root is None:
            states[name] = Layer(name, None, LAYER_UNSET)
            continue
        path = Path(root)
        if not path.is_dir():
            states[name] = Layer(name, str(path), LAYER_MISSING)
            continue
        problems: list[str] = []
        read.append((_read_upper(path, name, problems), problems))
    declared_anywhere = {lesson.id for lesson in base.lessons}
    for upper, _ in read:
        declared_anywhere.update(i for i in (_own_id(meta) for _, meta in upper.declared) if i)
    uppers: list[_Upper] = []
    for upper, problems in read:
        if problems:                                   # rejected already: list every problem it has
            problems.extend(_dangling(upper.declared, declared_anywhere))
            invalid[upper.name] = problems
            states[upper.name] = Layer(upper.name, str(upper.root), LAYER_INVALID, 0, tuple(problems))
        else:
            uppers.append(upper)

    # Checks that need the other layers: reference targets and always-in-order. Rejecting a
    # layer can break a higher one, so start over until every remaining layer passes.
    while True:
        known = {lesson.id for lesson in base.lessons}
        for upper in uppers:
            known.update(lesson.id for lesson in upper.lessons)
        order, always = base.config.order, base.config.always
        rejected = None
        for upper in uppers:
            problems = _dangling(upper.declared, known)
            merged = merge_order(order, upper.order)
            problems.extend(f"{CONFIG_FILE}: always: {category!r} is not in order"
                            for category in upper.always if category not in merged)
            if problems:
                rejected = upper
                invalid[upper.name] = problems
                states[upper.name] = Layer(upper.name, str(upper.root), LAYER_INVALID, 0, tuple(problems))
                break
            order, always = merged, merge_always(always, upper.always)
        if rejected is None:
            break
        uppers.remove(rejected)

    # The lessons in force: the highest layer's lesson of each id, except over an enforced
    # company lesson.
    kept_warnings: list[str] = []
    in_force: dict[str, Lesson] = {lesson.id: lesson for lesson in base.lessons}
    replaced: dict[str, list[Lesson]] = {}
    for upper in uppers:
        for lesson in sorted(upper.lessons, key=lambda x: x.id):
            current = in_force.get(lesson.id)
            if current is not None and current.layer == LAYER_COMPANY and current.enforced:
                kept_warnings.append(f"enforced_kept: {lesson.id} ignored {upper.name}")
                continue
            if current is not None:
                replaced.setdefault(lesson.id, []).append(current)
            in_force[lesson.id] = lesson
        states[upper.name] = Layer(upper.name, str(upper.root), LAYER_LOADED, len(upper.lessons))

    cycles = supersedes_cycles(in_force.values())
    if cycles:
        raise GuidanceError(cycles)

    config = Config(schema_version=SCHEMA_VERSION, order=order, always=always, budget=base.config.budget)
    lessons = sorted(in_force.values(),
                     key=lambda lesson: (config.category_rank(lesson.category), lesson.order, lesson.id))
    categories = list(base.categories)
    for upper in uppers:
        categories.extend(upper.categories)
    categories.sort(key=lambda c: (config.category_rank(c.id), LAYERS.index(c.layer), c.id))
    overridden = tuple((old, in_force[lesson_id].layer)
                       for lesson_id in sorted(replaced) for old in replaced[lesson_id])

    layer_warnings = [f"enforced_ignored: {lesson.id}: enforced counts only in the company layer"
                      for lesson in base.lessons if lesson.enforced]
    layer_warnings.extend(f"layer_invalid: {name}: {invalid[name][0]}" for name in LAYERS if name in invalid)
    layer_warnings.extend(kept_warnings)
    writing = list(base.warnings)
    for upper in uppers:
        writing.extend(f"{upper.name}: {w}" for w in _writing_warnings(upper.lessons))
    return Library(
        root=base.root, layer=LAYER_FRAMEWORK, config=config, categories=tuple(categories),
        lessons=tuple(lessons), warnings=tuple(writing + layer_warnings),
        layers=tuple(states[name] for name in LAYERS), overridden=overridden,
        layer_warnings=tuple(layer_warnings),
    )
