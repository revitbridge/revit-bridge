"""Load and validate one guidance root (format v1).

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
keys, body size; across lessons: duplicate ids, ``supersedes`` /
``superseded_by`` targets that exist and are not the lesson itself.

Only the framework layer exists in S3a; ``layer`` is recorded on every
lesson so the output carries it from the start. Nothing here reads the
environment or the network.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from revit_bridge.guidance.model import (
    CATEGORIES,
    CATEGORY_TRIGGER_RE,
    DEFAULT_ORDER,
    ERROR_RE,
    IDENTIFIER_RE,
    KINDS,
    LAYER_FRAMEWORK,
    MAX_BODY_BYTES,
    MAX_TITLE_CHARS,
    SCHEMA_VERSION,
    SLUG_RE,
    STAGES,
    TRIGGER_KEYS,
    Budget,
    Category,
    Config,
    Lesson,
    Triggers,
    VersionRange,
    parse_range,
)

CONFIG_FILE = "guidance.yaml"
SKILL_FILE = "SKILL.md"
LESSONS_DIR = "lessons"

LESSON_KEYS = frozenset({
    "schema_version", "id", "revision", "title", "stage", "kind", "revit", "triggers",
    "order", "supersedes", "superseded_by", "enforced",
})
REQUIRED_KEYS = ("schema_version", "id", "revision", "title", "stage", "kind", "revit", "triggers")
CONFIG_KEYS = frozenset({"schema_version", "order", "always", "budget"})
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
    """One validated guidance root. Lessons are sorted: category order, ``order``, ``id``."""

    root: str
    layer: str
    config: Config
    categories: tuple[Category, ...]
    lessons: tuple[Lesson, ...]
    _by_id: Mapping[str, Lesson] = field(default=MappingProxyType({}), repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_id", MappingProxyType({lesson.id: lesson for lesson in self.lessons}))

    def get(self, lesson_id: str) -> Lesson | None:
        return self._by_id.get(lesson_id)

    def category(self, category_id: str) -> Category | None:
        for category in self.categories:
            if category.id == category_id:
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

def _load_config(root: Path, problems: list[str]) -> Config | None:
    path = root / CONFIG_FILE
    if not path.is_file():
        problems.append(f"{CONFIG_FILE}: missing")
        return None
    where = CONFIG_FILE
    try:
        data = _yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        problems.append(f"{where}: cannot be read: {e}")
        return None
    if not isinstance(data, dict):
        problems.append(f"{where}: must be a mapping")
        return None
    local: list[str] = []
    for key in sorted(set(data) - CONFIG_KEYS, key=str):
        local.append(f"unknown key {key!r}")
    if not (_is_int(data.get("schema_version")) and data.get("schema_version") == SCHEMA_VERSION):
        local.append(f"schema_version must be {SCHEMA_VERSION}")
    order = _str_list(data.get("order"), "order", local)
    if "order" not in data:
        local.append("order is required")
    for category in order:
        if category not in CATEGORIES:
            local.append(f"order: unknown category {category!r}")
    if len(set(order)) != len(order):
        local.append("order: a category is listed twice")
    always = _str_list(data.get("always"), "always", local)
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
    problems.extend(f"{where}: {p}" for p in local)
    if local:
        return None
    return Config(schema_version=SCHEMA_VERSION, order=order, always=always, budget=Budget(**budget_values))


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


def _load_category(directory: Path) -> Category:
    skill = read_skill(directory / SKILL_FILE)
    if skill is None:
        return Category(id=directory.name, name=directory.name, description="")
    meta, body = skill
    return Category(id=directory.name, name=str(meta.get("name") or directory.name),
                    description=str(meta.get("description") or ""), summaries=index_summaries(body))


# -- lessons -------------------------------------------------------------------------

def _load_lesson(root: Path, path: Path, layer: str,
                 problems: list[str]) -> tuple[Lesson | None, dict | None]:
    """The lesson (None when it has problems) and its parsed front matter (None when unreadable)."""
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
    )
    return lesson, meta


def _cross_check(declared: list[tuple[str, dict | None]], problems: list[str]) -> None:
    """Duplicate ids and ``supersedes`` / ``superseded_by`` targets, on what each file declares
    (a file with other problems still takes part)."""
    def own_id(meta):
        value = meta.get("id") if meta else None
        return value if isinstance(value, str) else None

    where: dict[str, list[str]] = {}
    for rel, meta in declared:
        if own_id(meta) is not None:
            where.setdefault(own_id(meta), []).append(rel)
    for lesson_id, paths in sorted(where.items()):
        if len(paths) > 1:
            problems.append(f"duplicate id {lesson_id!r} in {', '.join(paths)}")
    for rel, meta in declared:
        if not meta:
            continue
        own = own_id(meta)
        supersedes = meta.get("supersedes") if isinstance(meta.get("supersedes"), list) else []
        for target in (t for t in supersedes if isinstance(t, str)):
            if target == own:
                problems.append(f"{rel}: supersedes itself")
            elif target not in where:
                problems.append(f"{rel}: supersedes unknown id {target!r}")
        superseded_by = meta.get("superseded_by")
        if isinstance(superseded_by, str):
            if superseded_by == own:
                problems.append(f"{rel}: superseded_by points at itself")
            elif superseded_by not in where:
                problems.append(f"{rel}: superseded_by unknown id {superseded_by!r}")


def load(root: str | Path, layer: str = LAYER_FRAMEWORK) -> Library:
    """Validate and load one guidance root; raises ``GuidanceError`` with every problem."""
    root = Path(root)
    problems: list[str] = []
    if not root.is_dir():
        raise GuidanceError([f"{root}: not a directory"])

    config = _load_config(root, problems)

    categories: list[Category] = []
    lesson_paths: list[Path] = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        if directory.name.startswith((".", "__")):
            continue
        if directory.name not in CATEGORIES:
            problems.append(f"{directory.name}/: not a category in the category table")
        else:
            categories.append(_load_category(directory))
        lessons_dir = directory / LESSONS_DIR
        if lessons_dir.is_dir():
            lesson_paths.extend(sorted(lessons_dir.glob("*.md")))

    lessons: list[Lesson] = []
    declared: list[tuple[str, dict | None]] = []
    for path in lesson_paths:
        lesson, meta = _load_lesson(root, path, layer, problems)
        declared.append((path.relative_to(root).as_posix(), meta))
        if lesson is not None:
            lessons.append(lesson)
    _cross_check(declared, problems)

    if problems:
        raise GuidanceError(problems)
    assert config is not None
    lessons.sort(key=lambda lesson: (config.category_rank(lesson.category), lesson.order, lesson.id))
    categories.sort(key=lambda category: (config.category_rank(category.id), category.id))
    return Library(root=str(root), layer=layer, config=config, categories=tuple(categories),
                   lessons=tuple(lessons))
