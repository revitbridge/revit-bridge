"""Lexical boundaries for {parameter} substitution in a C# snippet."""
from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from typing import Iterator

PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_]\w*)\}")
_NEWLINE = re.compile(r"\r\n|[\r\n\x85\u2028\u2029]")
# At-sign forms always use verbatim quoting, even before a run of quotes.
_OPEN = r"""//|/\*|(?:\$@|@\$|@)"|(?<!\$)\$+"{3,}|"{3,}|\$?"|'|#"""
_TOKEN = re.compile(_OPEN)
_EXPRESSION = re.compile(_OPEN + r"|::|[][(){}:]")
_BRACES = re.compile(r"\{+")
_QUOTES = re.compile(r'"+')
_BRANCH = re.compile(r"(?:\A|(?<=[\r\n\x85\u2028\u2029]))[^\S\r\n\x85\u2028\u2029]*"
                     r"#[^\S\r\n\x85\u2028\u2029]*(?:else|elif|endif)\b")
_CONDITIONAL = re.compile(r"#\s*(if|endif)\b")

CODE = "code"
STRING = "ordinary string literal"
NUMERIC_TYPES = frozenset({"double", "number", "float", "int", "integer"})
BOOLEAN_TYPES = frozenset({"bool", "boolean"})
BARE_TYPES = NUMERIC_TYPES | BOOLEAN_TYPES


@dataclass(frozen=True)
class Token:
    context: str
    start: int
    end: int


@dataclass(frozen=True)
class Slot(Token):
    name: str


class Lexer:
    """Emit code spans and opaque literals/comments, including whole interpolations."""

    def __init__(self, code: str):
        self.code = code
        self.native_holes: set[int] = set()

    def tokens(self) -> Iterator[Token]:
        pos = 0
        conditionals = 0
        while match := _TOKEN.search(self.code, pos):
            if pos < match.start():
                yield Token(CODE, pos, match.start())
            try:
                token = self._token(match)
            except RecursionError:
                token = Token("unresolved interpolation nesting", match.start(), len(self.code))
            # A disabled preprocessor branch can contain arbitrary non-C# text.
            # If it hid a branch boundary from this scan, subsequent quotes are ambiguous.
            if token.context == "preprocessor directive":
                directive = _CONDITIONAL.match(self.code, token.start, token.end)
                if directive:
                    conditionals += 1 if directive[1] == "if" else -1
            elif conditionals and (boundary := _BRANCH.search(self.code, token.start, token.end)):
                yield Token(token.context, token.start, boundary.start())
                yield Token("ambiguous preprocessor context", boundary.start(), len(self.code))
                return
            yield token
            pos = token.end
        if pos < len(self.code):
            yield Token(CODE, pos, len(self.code))

    def _token(self, match: re.Match) -> Token:
        code, opening, pos = self.code, match.group(), match.end()
        if opening in ("//", "#"):
            newline = _NEWLINE.search(code, pos)
            end = newline.start() if newline else len(code)
            kind = "line comment" if opening == "//" else "preprocessor directive"
        elif opening == "/*":
            close = code.find("*/", pos)
            end = close + 2 if close >= 0 else len(code)
            kind = "block comment"
        else:
            verbatim = "@" in opening
            quotes = opening.count('"')
            dollars = opening.count("$")
            if quotes >= 3:
                kind = "interpolated raw string literal" if dollars else "raw string literal"
            elif dollars:
                kind = "interpolated verbatim string literal" if verbatim else "interpolated string literal"
            else:
                kind = "verbatim string literal" if verbatim else STRING if quotes else "char literal"
            end = self._literal_end(pos, opening[-1], quotes, verbatim, dollars)
        return Token(kind, match.start(), end)

    def _literal_end(self, pos: int, quote: str, quotes: int, verbatim: bool, dollars: int) -> int:
        code = self.code
        while pos < len(code):
            ch = code[pos]
            if ch == quote:
                if quotes >= 3:
                    end = _QUOTES.match(code, pos).end()
                    if end - pos >= quotes:
                        return end
                    pos = end
                elif verbatim and code.startswith('""', pos):
                    pos += 2
                else:
                    return pos + 1
            elif quotes < 3 and not verbatim and ch == "\\":
                if _NEWLINE.match(code, pos + 1):
                    return pos + 1
                pos += 2
            elif dollars and ch == "{":
                end = _BRACES.match(code, pos).end()
                count = end - pos
                pos = end
                # In regular interpolation pairs are literal. In a raw string
                # the last N braces open the hole, where N is the dollar count.
                opens_hole = count >= dollars if quotes >= 3 else count % 2 == 1
                if opens_hole:
                    hole = PLACEHOLDER_RE.match(code, pos - 1)
                    if hole and code.startswith("}" * dollars, hole.end() - 1):
                        self.native_holes.add(pos - 1)
                    pos = self._hole_end(pos, dollars)
            elif not verbatim and quotes < 3 and _NEWLINE.match(code, pos):
                return pos
            else:
                pos += 1
        return len(code)

    def _hole_end(self, pos: int, dollars: int) -> int:
        """Skip an expression, shielding nested literals/comments and balanced groups."""
        code = self.code
        groups = 0
        while match := _EXPRESSION.search(code, pos):
            symbol, pos = match.group(), match.end()
            if symbol in ("(", "[", "{"):
                groups += 1
            elif symbol in (")", "]", "}"):
                if groups:
                    groups -= 1
                elif symbol == "}" and code.startswith("}" * dollars, pos - 1):
                    return pos - 1 + dollars
            elif symbol == ":" and not groups:
                # Format text is part of the string. Quotes and comment markers
                # here do not open C# tokens inside the expression.
                end = code.find("}" * dollars, pos)
                return len(code) if end < 0 else end + dollars
            elif symbol not in (":", "::"):
                pos = self._token(match).end
        return len(code)


def template_slots(code: str, declared: dict[str, dict]) -> tuple[list[Slot], list[str]]:
    """Return only original substitution sites. Native C# interpolation locals pass through."""
    if not isinstance(code, str):
        return [], ["code_template: must be a string"]
    matches = iter(PLACEHOLDER_RE.finditer(code))
    match = next(matches, None)
    if match is None:
        return [], []
    starts = [0, *(m.end() for m in _NEWLINE.finditer(code))]
    slots, errors = [], []
    lexer = Lexer(code)
    for token in lexer.tokens():
        while match and match.start() < token.end:
            name, pos = match[1], match.start()
            context = token.context
            param = declared.get(name)
            if param is None and pos in lexer.native_holes:
                match = next(matches, None)
                continue
            # A template backslash must not consume the first backslash of an
            # escaped value. An even run is already a complete C# escape.
            if context == STRING:
                i = pos
                while i and code[i - 1] == "\\":
                    i -= 1
                if (pos - i) % 2:
                    context = "escape sequence in an ordinary string literal"
            reason = ""
            if param is None:
                reason = "is not a declared parameter"
            elif context == CODE and str(param.get("type", "string")).lower() in BARE_TYPES:
                if param.get("default") == "":
                    reason = "has an empty default in bare code"
            elif context != STRING:
                reason = "cannot be substituted here"
            if reason:
                line = bisect_right(starts, pos)
                errors.append(f"code_template: placeholder {{{name}}} {reason} at line {line}, "
                              f"column {pos - starts[line - 1] + 1} ({context}). "
                              f'Move it into an ordinary string literal: string x = "{{{name}}}";')
            else:
                slots.append(Slot(context, pos, match.end(), name))
            match = next(matches, None)
        if match is None:
            break
    return slots, errors
