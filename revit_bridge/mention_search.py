"""Rank and page mention rows on the host after Revit returns its model data."""
import json
import re
from collections import Counter
from importlib.resources import files

ALIASES = json.loads(files(__package__).joinpath("mention_aliases.json").read_text(encoding="utf-8"))
_WORDS = re.compile(r"[^\W_]+")
_TERMS = re.compile(r"[\u3400-\u9fff]+|[^\u3400-\u9fff\s]+")
MAX_TERMS = 8


def search_terms(query):
    terms = {}
    for match in _TERMS.finditer(query.casefold()):
        terms[match.group()] = None
        if len(terms) > MAX_TERMS:
            raise ValueError(f"q accepts at most {MAX_TERMS} distinct terms")
    return list(terms)


def category_terms(terms):
    """The intersection of all category constraints, and remaining name terms."""
    matched = None
    remaining = []
    for term in terms:
        categories = set()
        if len(term) >= 3 or any("\u3400" <= c <= "\u9fff" for c in term):
            categories = {key for key, aliases in ALIASES.items() if any(
                a.startswith(term) or (a.isascii() and term in (a + "s", a + "es")) for a in aliases)}
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


def _rank(term, fields, words, fuzzy_words):
    if term in fields:
        return 0
    if any(field.startswith(term) for field in fields):
        return 1
    if any(word.startswith(term) for word in words):
        return 2
    if any(term in field for field in fields):
        return 3
    if len(term) >= 3 and not term.isdecimal():
        if any(_near(term, word) for word in fuzzy_words):
            return 4
        for word in fuzzy_words:
            chars = iter(word)
            if all(c in chars for c in term):
                return 4
    return None


def rank_items(items, terms, *, name_and_family_only=False):
    if not terms:
        return items
    ranked = []
    for item in items:
        fields = [v.casefold() for v in (item.get("name"), item.get("family")) if v]
        fuzzy_words = [word for field in fields for word in _WORDS.findall(field)]
        words = fuzzy_words
        if not name_and_family_only:
            extra = [item.get("category"), item.get("category_label")]
            if item["kind"] == "type":
                extra.append(item.get("detail"))
            else:
                extra.append(item.get("label"))
            extra.extend(ALIASES.get(item.get("category"), ()))
            extra = [v.casefold() for v in extra if v]
            words = fuzzy_words + [word for field in extra for word in _WORDS.findall(field)]
            fields.extend(extra)
        scores = []
        for term in terms:
            score = _rank(term, fields, words, fuzzy_words)
            if score is None:
                break
            scores.append(score)
        else:
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
