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
       (an error code counts only when a symbol hits too: one of
       ``error_symbols`` when they are given - ``check_code`` passes the
       diagnostics' own symbols - otherwise one of ``symbols``) and
       ``exception:<name>`` (short names compare by their last segment).
    3. Order: lessons with an ``error:`` hit first, then the rest; inside
       each group ``guidance.yaml`` category order, the lesson's ``order``,
       its id.
    4. A lesson that a lesson in force supersedes goes to ``omitted``
       (``superseded_by:<id>``); one whose ``revit`` range excludes
       ``revit_version`` goes to ``omitted`` (``version``); one whose hash is
       in ``seen_hashes`` goes to ``omitted`` (``seen``) and uses no budget.
       The rest are taken in order while they fit ``budget.items`` and
       ``budget.utf8_bytes``; one that does not fit goes to ``omitted``
       (``budget``) and a later, smaller one may still fit. Bodies are never
       cut, except with ``inline=True``.
    5. ``inline=True`` (compile failures, D12): at most ``fix_inline_items``
       lessons, each body cut to ``fix_inline_bytes`` UTF-8 bytes including a
       last line ``[truncated: get_guidance(mode="read", ids=["<id>"])]``;
       such items have ``truncated: true``.

``pointers`` the lessons ``recommend`` would choose, without bodies: the
           ``index`` item (``summary``) with ``why``, at most ``budget.items``;
           body sizes do not count and a superseded or version-excluded
           lesson is left out.
``index``  every lesson in force without its body (plus ``summary``), in
           order; a lesson a higher layer replaced is in ``omitted``
           (``overridden_by:<layer>``).
``read``   the full text of the given ids, ignoring budget and seen hashes;
           unknown ids are listed in ``missing``.

``applicability`` is ``verified`` when ``revit_version`` is given and inside
the lesson's range, else ``unknown``. ``layer`` and ``hash`` are those of the
lesson in force. Every reply's ``warnings`` start with the library's layer
warnings (``layer_invalid``, ``enforced_kept``, ``enforced_ignored``).
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from revit_bridge.guidance.loader import Library
from revit_bridge.guidance.model import CATEGORIES, SCHEMA_VERSION, STAGES, Lesson, parse_version

MODES = ("recommend", "index", "read")
VERIFIED = "verified"
UNKNOWN = "unknown"

# The last line of a body cut by inline=True (S2b contract of check_code's guidance).
READ_HINT = '\n\n[truncated: get_guidance(mode="read", ids=["{id}"])]\n'


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


def _matched(lesson: Lesson, given: list[str]) -> list[str]:
    """The lesson's trigger symbols found as whole words in ``given``, in trigger order."""
    return [s for s in lesson.triggers.symbols if any(_word(s).search(text) for text in given)]


def _hits(lesson: Lesson, stage: str, always: tuple[str, ...], skill_categories: list[str],
          revit_categories: list[str], symbols: list[str], errors: list[str],
          exceptions: list[str], error_symbols: list[str] | None) -> list[str]:
    hits = []
    if stage == "orient" and lesson.category in always:
        hits.append(f"always:{lesson.category}")
    if lesson.category in skill_categories:
        hits.append(f"skill_category:{lesson.category}")
    for category in lesson.triggers.categories:
        if category in revit_categories:
            hits.append(f"category:{category}")
    matched = _matched(lesson, symbols)
    hits.extend(f"symbol:{s}" for s in matched)
    paired = matched if error_symbols is None else _matched(lesson, error_symbols)
    if paired:
        for code in lesson.triggers.errors:
            if code in errors:
                hits.append(f"error:{code}+symbol:{','.join(paired)}")
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
        "warnings": (list(library.layer_warnings) if library is not None else []) + (warnings or []),
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


def _signals(skill_categories, revit_categories, symbols, errors, exceptions, error_symbols,
             warnings: list[str]) -> tuple:
    """The recommend inputs cleaned up: (skill, revit categories, symbols, error codes,
    exceptions, error symbols - None when not given, so errors pair with ``symbols``)."""
    skill = _strings(skill_categories)
    for name in skill:
        if name not in CATEGORIES:
            warnings.append(f"skill_categories: {name!r} is not a guidance category")
    codes = [code.upper() for code in _strings(errors)]
    paired = None if error_symbols is None else _strings(error_symbols)
    return skill, _strings(revit_categories), _strings(symbols), codes, _strings(exceptions), paired


def _is_error_hit(hits: list[str]) -> bool:
    return any(hit.startswith("error:") for hit in hits)


def _matches(library: Library, stage: str, signals: tuple) -> list[tuple[Lesson, list[str]]]:
    """``(lesson, hits)`` for every lesson of ``stage`` that something hits: lessons with an
    error hit first, each group in library order (category order, ``order``, id)."""
    found = []
    for lesson in library.lessons:
        if stage not in lesson.stage:
            continue
        hits = _hits(lesson, stage, library.config.always, *signals)
        if hits:
            found.append((lesson, hits))
    return sorted(found, key=lambda pair: not _is_error_hit(pair[1]))      # stable: keeps library order


def recommend(library: Library, stage: str | None, *, skill_categories: Iterable[str] | None = None,
              revit_categories: Iterable[str] | None = None, symbols: Iterable[str] | None = None,
              errors: Iterable[str] | None = None, exceptions: Iterable[str] | None = None,
              revit_version: str | None = None, seen_hashes: Iterable[str] | None = None,
              inline: bool = False, error_symbols: Iterable[str] | None = None) -> dict:
    """The lessons for one moment of a task, chosen, ordered and budgeted (see the module doc).

    ``error_symbols`` (package only, not a ``get_guidance`` parameter): the symbols an error
    code pairs with; None pairs it with ``symbols``.
    """
    if not stage:
        return _reply("recommend", stage, library,
                      error=f'stage_required: mode="recommend" needs stage ({", ".join(STAGES)})')
    if stage not in STAGES:
        return _reply("recommend", stage, library,
                      error=f"invalid_stage: {stage!r} is not one of {', '.join(STAGES)}")
    warnings: list[str] = []
    signals = _signals(skill_categories, revit_categories, symbols, errors, exceptions, error_symbols, warnings)
    seen = set(_strings(seen_hashes))
    version = _version(revit_version, warnings)

    budget = library.config.budget
    max_items = min(budget.items, budget.fix_inline_items) if inline else budget.items
    max_bytes = budget.utf8_bytes
    items, omitted, used = [], [], 0
    for lesson, hits in _matches(library, stage, signals):
        superseder = library.superseded_by(lesson.id)
        if superseder is not None:
            omitted.append(_omitted(lesson, f"superseded_by:{superseder}"))
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
    """The lesson's ``summary`` key, else its line in the SKILL.md index of its category in its
    own layer, else its title. (``guidance index`` writes the index lines from the key.)"""
    if lesson.summary:
        return lesson.summary
    category = library.category(lesson.category, lesson.layer)
    text = category.summary_of(lesson.slug) if category is not None else None
    return text or lesson.title


def pointers(library: Library, stage: str, *, skill_categories: Iterable[str] | None = None,
             revit_categories: Iterable[str] | None = None, symbols: Iterable[str] | None = None,
             errors: Iterable[str] | None = None, exceptions: Iterable[str] | None = None,
             revit_version: str | None = None, error_symbols: Iterable[str] | None = None) -> list[dict]:
    """The lessons ``recommend`` would choose, as pointers: each the ``index`` item (``summary``,
    no ``body``) with ``why``, at most ``budget.items``. Body sizes do not count; a superseded
    lesson and one whose range excludes ``revit_version`` are left out.
    ``get_guidance(mode="read")`` reads one."""
    if stage not in STAGES:
        raise ValueError(f"invalid_stage: {stage!r} is not one of {', '.join(STAGES)}")
    signals = _signals(skill_categories, revit_categories, symbols, errors, exceptions, error_symbols, [])
    version = _version(revit_version, [])
    items: list[dict] = []
    for lesson, hits in _matches(library, stage, signals):
        if library.superseded_by(lesson.id) is not None or _excluded(lesson, version):
            continue
        if len(items) >= library.config.budget.items:
            break
        item = _head(lesson, _applicability(lesson, version), [f"stage:{stage}"] + hits)
        item["summary"] = summary(library, lesson)
        items.append(item)
    return items


def index(library: Library, *, revit_version: str | None = None, stage: str | None = None) -> dict:
    """Every lesson in force without its body, in order; one whose range excludes the version goes
    to ``omitted`` (``version``), and so does each lesson a higher layer replaced
    (``overridden_by:<layer>``)."""
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
    omitted.extend(_omitted(lesson, f"overridden_by:{layer}") for lesson, layer in library.overridden)
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
