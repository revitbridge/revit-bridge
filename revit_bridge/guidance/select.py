"""Choose, order and budget lessons: the ``get_guidance`` replies (contract v1).

Deterministic and model-free: the same library and the same inputs give the
same reply, byte for byte once serialized.

``recommend``
    1. Candidates are the lessons whose ``stage`` list holds the stage.
    2. A candidate is chosen when anything hits; ``why`` lists every hit
       after ``stage:<stage>``: ``always:<category>`` (an ``always``
       category at ``orient``), ``skill_category:<id>``, ``category:<OST_*>``,
       ``symbol:<name>`` (a trigger symbol found as a whole word, case
       sensitive, in one of the given symbols), ``error:<CSxxxx>+symbol:<a,b>``
       (an error code counts only when a symbol hits too) and
       ``exception:<name>`` (short names compare by their last segment).
    3. Order: ``guidance.yaml`` category order, the lesson's ``order``, its id.
    4. A lesson whose ``revit`` range excludes ``revit_version`` goes to
       ``omitted`` (``version``); one whose hash is in ``seen_hashes`` goes to
       ``omitted`` (``seen``) and uses no budget. The rest are taken in order
       while they fit ``budget.items`` and ``budget.utf8_bytes``; one that
       does not fit goes to ``omitted`` (``budget``) and a later, smaller one
       may still fit. Bodies are never cut, except with ``inline=True``.
    5. ``inline=True`` (compile failures, D12): at most ``fix_inline_items``
       lessons, each body cut to ``fix_inline_bytes`` UTF-8 bytes including a
       note that ``get_guidance(mode="read", ids=[...])`` returns the full
       text; such items have ``truncated: true``.

``index``  every lesson without its body (plus ``summary``), in order.
``read``   the full text of the given ids, ignoring budget and seen hashes;
           unknown ids are listed in ``missing``.

``applicability`` is ``verified`` when ``revit_version`` is given and inside
the lesson's range, else ``unknown``.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from revit_bridge.guidance.loader import Library
from revit_bridge.guidance.model import CATEGORIES, SCHEMA_VERSION, STAGES, Lesson, parse_version

MODES = ("recommend", "index", "read")
VERIFIED = "verified"
UNKNOWN = "unknown"

READ_HINT = '\n\n[truncated: get_guidance(mode="read", ids=["{id}"]) returns the full text]\n'


def _strings(values: Iterable | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    out = []
    for value in values:
        if isinstance(value, str) and value.strip() and value.strip() not in out:
            out.append(value.strip())
    return out


def _word(symbol: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(symbol) + r"(?![A-Za-z0-9_])")


def _exception_matches(trigger: str, given: str) -> bool:
    if "." in trigger and "." in given:
        return trigger == given
    return trigger.rsplit(".", 1)[-1] == given.rsplit(".", 1)[-1]


def _hits(lesson: Lesson, stage: str, always: tuple[str, ...], skill_categories: list[str],
          revit_categories: list[str], symbols: list[str], errors: list[str],
          exceptions: list[str]) -> list[str]:
    hits = []
    if stage == "orient" and lesson.category in always:
        hits.append(f"always:{lesson.category}")
    if lesson.category in skill_categories:
        hits.append(f"skill_category:{lesson.category}")
    for category in lesson.triggers.categories:
        if category in revit_categories:
            hits.append(f"category:{category}")
    matched = [s for s in lesson.triggers.symbols if any(_word(s).search(given) for given in symbols)]
    hits.extend(f"symbol:{s}" for s in matched)
    if matched:
        for code in lesson.triggers.errors:
            if code in errors:
                hits.append(f"error:{code}+symbol:{','.join(matched)}")
    for name in lesson.triggers.exceptions:
        if any(_exception_matches(name, given) for given in exceptions):
            hits.append(f"exception:{name}")
    return hits


def _applicability(lesson: Lesson, version) -> str:
    return VERIFIED if version is not None and lesson.revit.contains(version) else UNKNOWN


def _excluded(lesson: Lesson, version) -> bool:
    return version is not None and not lesson.revit.contains(version)


def _head(lesson: Lesson, applicability: str, why: list[str]) -> dict:
    return {
        "id": lesson.id,
        "revision": lesson.revision,
        "hash": lesson.hash,
        "layer": lesson.layer,
        "category": lesson.category,
        "title": lesson.title,
        "kind": lesson.kind,
        "stage": list(lesson.stage),
        "revit": lesson.revit.text,
        "applicability": applicability,
        "why": why,
    }


def _item(lesson: Lesson, applicability: str, why: list[str], body: str, truncated: bool) -> dict:
    item = _head(lesson, applicability, why)
    item.update({"body": body, "bytes": len(body.encode("utf-8")), "truncated": truncated})
    return item


def _omitted(lesson: Lesson, reason: str) -> dict:
    return {"id": lesson.id, "title": lesson.title, "reason": reason}


def truncate(lesson: Lesson, limit: int) -> tuple[str, bool]:
    """The body cut to ``limit`` UTF-8 bytes with the read-the-full-text note, or the whole body."""
    body = lesson.body
    if len(body.encode("utf-8")) <= limit:
        return body, False
    hint = READ_HINT.format(id=lesson.id)
    room = max(0, limit - len(hint.encode("utf-8")))
    cut = body.encode("utf-8")[:room].decode("utf-8", errors="ignore")
    newline = cut.rfind("\n")
    if newline > room // 2:                     # end on a whole line when that keeps most of it
        cut = cut[:newline]
    return cut.rstrip() + hint, True


def _reply(mode: str, stage: str | None, library: Library | None = None, *, items=None, omitted=None,
           missing=None, items_budget: int | None = None, bytes_budget: int | None = None,
           used: int = 0, warnings=None, error: str | None = None) -> dict:
    budget = library.config.budget if library is not None else None
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "stage": stage,
        "items": items or [],
        "omitted": omitted or [],
        "missing": missing or [],
        "budget": {
            "items": items_budget if items_budget is not None else (budget.items if budget else 0),
            "utf8_bytes": bytes_budget if bytes_budget is not None else (budget.utf8_bytes if budget else 0),
            "used_bytes": used,
        },
        "warnings": warnings or [],
        "error": error,
    }


def _version(revit_version: str | None, warnings: list[str]):
    if revit_version is None or (isinstance(revit_version, str) and not revit_version.strip()):
        return None
    version = parse_version(revit_version)
    if version is None:
        warnings.append(f"revit_version {revit_version!r} is not a Revit version such as 2026 or "
                        f"2026.5: applicability is unknown")
    return version


def recommend(library: Library, stage: str | None, *, skill_categories: Iterable[str] | None = None,
              revit_categories: Iterable[str] | None = None, symbols: Iterable[str] | None = None,
              errors: Iterable[str] | None = None, exceptions: Iterable[str] | None = None,
              revit_version: str | None = None, seen_hashes: Iterable[str] | None = None,
              inline: bool = False) -> dict:
    """The lessons for one moment of a task, chosen, ordered and budgeted (see the module doc)."""
    if not stage:
        return _reply("recommend", stage, library,
                      error=f'stage_required: mode="recommend" needs stage ({", ".join(STAGES)})')
    if stage not in STAGES:
        return _reply("recommend", stage, library,
                      error=f"invalid_stage: {stage!r} is not one of {', '.join(STAGES)}")
    warnings: list[str] = []
    skill = _strings(skill_categories)
    for name in skill:
        if name not in CATEGORIES:
            warnings.append(f"skill_categories: {name!r} is not a guidance category")
    revit_cats = _strings(revit_categories)
    syms = _strings(symbols)
    codes = [code.upper() for code in _strings(errors)]
    excs = _strings(exceptions)
    seen = set(_strings(seen_hashes))
    version = _version(revit_version, warnings)

    budget = library.config.budget
    max_items = min(budget.items, budget.fix_inline_items) if inline else budget.items
    max_bytes = budget.utf8_bytes
    items, omitted, used = [], [], 0
    for lesson in library.lessons:
        if stage not in lesson.stage:
            continue
        hits = _hits(lesson, stage, library.config.always, skill, revit_cats, syms, codes, excs)
        if not hits:
            continue
        if _excluded(lesson, version):
            omitted.append(_omitted(lesson, "version"))
            continue
        if lesson.hash in seen:
            omitted.append(_omitted(lesson, "seen"))
            continue
        body, truncated = truncate(lesson, budget.fix_inline_bytes) if inline else (lesson.body, False)
        size = len(body.encode("utf-8"))
        if len(items) >= max_items or used + size > max_bytes:
            omitted.append(_omitted(lesson, "budget"))
            continue
        items.append(_item(lesson, _applicability(lesson, version), [f"stage:{stage}"] + hits, body, truncated))
        used += size
    return _reply("recommend", stage, library, items=items, omitted=omitted, items_budget=max_items,
                  bytes_budget=max_bytes, used=used, warnings=warnings)


def summary(library: Library, lesson: Lesson) -> str:
    """The lesson's line in its category's SKILL.md index, or its title."""
    category = library.category(lesson.category)
    text = category.summary_of(lesson.slug) if category is not None else None
    return text or lesson.title


def index(library: Library, *, revit_version: str | None = None, stage: str | None = None) -> dict:
    """Every lesson without its body, in order; a version that a range excludes goes to ``omitted``."""
    warnings: list[str] = []
    version = _version(revit_version, warnings)
    items, omitted = [], []
    for lesson in library.lessons:
        if _excluded(lesson, version):
            omitted.append(_omitted(lesson, "version"))
            continue
        item = _head(lesson, _applicability(lesson, version), [])
        item["summary"] = summary(library, lesson)
        items.append(item)
    return _reply("index", stage, library, items=items, omitted=omitted, warnings=warnings)


def read(library: Library, ids: Iterable[str] | None, *, revit_version: str | None = None,
         stage: str | None = None) -> dict:
    """The full text of each id, in the order given; unknown ids go to ``missing``."""
    wanted = _strings(ids)
    if not wanted:
        return _reply("read", stage, library, error='ids_required: mode="read" needs ids')
    warnings: list[str] = []
    version = _version(revit_version, warnings)
    items, missing, used = [], [], 0
    for lesson_id in wanted:
        lesson = library.get(lesson_id)
        if lesson is None:
            missing.append(lesson_id)
            continue
        if _excluded(lesson, version):
            warnings.append(f"{lesson.id}: its revit range {lesson.revit.text!r} excludes {revit_version!r}")
        items.append(_item(lesson, _applicability(lesson, version), ["read"], lesson.body, False))
        used += len(lesson.body.encode("utf-8"))
    return _reply("read", stage, library, items=items, missing=missing, used=used, warnings=warnings)


def respond(library: Library, mode: str = "recommend", *, stage: str | None = None,
            skill_categories: Iterable[str] | None = None, revit_categories: Iterable[str] | None = None,
            symbols: Iterable[str] | None = None, errors: Iterable[str] | None = None,
            exceptions: Iterable[str] | None = None, revit_version: str | None = None,
            ids: Iterable[str] | None = None, seen_hashes: Iterable[str] | None = None) -> dict:
    """The ``get_guidance`` tool: dispatch on ``mode``."""
    if mode == "recommend":
        return recommend(library, stage, skill_categories=skill_categories, revit_categories=revit_categories,
                         symbols=symbols, errors=errors, exceptions=exceptions, revit_version=revit_version,
                         seen_hashes=seen_hashes)
    if mode == "index":
        return index(library, revit_version=revit_version, stage=stage)
    if mode == "read":
        return read(library, ids, revit_version=revit_version, stage=stage)
    return _reply(str(mode), stage, library, error=f"invalid_mode: {mode!r} is not one of {', '.join(MODES)}")


def render_index(library: Library) -> str:
    """The ``revit://guidance/index`` text: one line per lesson - id, layer, revit range, title."""
    lines = [
        f"# guidance index (schema_version {SCHEMA_VERSION}): {len(library.lessons)} lessons",
        f"# category order: {', '.join(library.config.order)}; always: {', '.join(library.config.always)}",
        "# id | layer | revit | title",
    ]
    lines.extend(f"{lesson.id} | {lesson.layer} | {lesson.revit.text} | {lesson.title}"
                 for lesson in library.lessons)
    return "\n".join(lines) + "\n"
