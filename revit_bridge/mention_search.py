"""Rank and page mention rows on the host after Revit returns its model data."""
import json
import re
from collections import Counter
from importlib.resources import files

ALIASES = json.loads(files(__package__).joinpath("mention_aliases.json").read_text(encoding="utf-8"))
_WORDS = re.compile(r"[^\W_]+")


def category_terms(terms):
    """The intersection of all category constraints, and remaining name terms."""
    matched = None
    remaining = []
    for term in terms:
        categories = set()
        if len(term) >= 3 or any("\u3400" <= c <= "\u9fff" for c in term):
            categories = {key for key, aliases in ALIASES.items() if any(a.startswith(term) for a in aliases)}
        if categories:
            matched = categories if matched is None else matched & categories
        else:
            remaining.append(term)
    return matched, remaining


def _near(a, b):
    """At most one insertion, deletion or substitution, in linear time."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    for i, char in enumerate(a):
        if char != b[i]:
            return a[i + (len(a) == len(b)):] == b[i + 1:]
    return True


def _rank(term, fields, words, *, exact_number):
    if term in fields:
        return 0
    if any(field.startswith(term) for field in fields):
        return 1
    if any(word.startswith(term) for word in words):
        return 2
    if any(term in field for field in fields):
        return 3
    if len(term) >= 3 and not (exact_number and term.isdecimal()):
        if any(_near(term, word) for word in words):
            return 4
        for field in fields:
            chars = iter(field)
            if all(c in chars for c in term):
                return 4
    return None


def rank_items(items, terms, *, name_only=False):
    if not terms:
        return items
    ranked = []
    for item in items:
        fields = [item.get("name")]
        if not name_only:
            fields.extend([item.get("family"), item.get("category"), item.get("category_label")])
            if item["kind"] == "type":
                fields.append(item.get("detail"))
            else:
                fields.append(item.get("label"))
            fields.extend(ALIASES.get(item.get("category"), ()))
        fields = [f.casefold() for f in fields if f]
        words = [word for field in fields for word in _WORDS.findall(field)]
        scores = [_rank(term, fields, words, exact_number=name_only) for term in terms]
        if None not in scores:
            key = (max(scores), sum(scores), (item.get("name") or "").casefold(), item["id"])
            ranked.append((key, item))
    return [item for _, item in sorted(ranked, key=lambda pair: pair[0])]


def facets(items):
    counts = Counter()
    labels = {}
    for item in items:
        category = item.get("category")
        if category:
            counts[category] += 1
            labels[category] = item.get("category_label") or item.get("detail") or category
    return [{"category": cat, "label": labels[cat], "count": counts[cat]} for cat in sorted(counts)]
