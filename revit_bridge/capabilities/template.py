"""Locate parameter sites in C# code and ordinary string literals.

The scanner shields character, verbatim, raw and interpolated literals, line
and block comments, and pragma/nullable lines. Interpolation expressions can
contain identifiers, numbers, operators, nested literals/comments, balanced
groups, alignment and format text. This is a lexical check, not a compiler.
Unsupported or ambiguous lexical constructs invalidate the entire template.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from revit_bridge.capabilities.scalars import BARE_TYPES, bare_literal

PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_]\w*)\}")
_NEWLINE = re.compile(r"\r\n|[\r\n\x85\u2028\u2029]")
# At-sign forms always use verbatim quoting, even before a run of quotes.
_OPEN = r"""//|/\*|(?:\$@|@\$|@)"|\$+"{3,}|"{3,}|\$?"|'|#|\$+"""
_TOKEN = re.compile(_OPEN)
_SPACE = r" \t\v\f\x1a\ufeff\xa0\u1680\u2000-\u200a\u202f\u205f\u3000"
_DIRECTIVE_SPACE = re.compile(f"[{_SPACE}]")
_ALLOWED_DIRECTIVE = re.compile(rf"#[{_SPACE}]*(?:pragma|nullable)(?=[{_SPACE}\r\n\x85\u2028\u2029]|//|/\*|\Z)")
# The only directive shapes accepted. Roslyn lexes the rest of a directive line with its
# own rules (a run of quotes there opens a raw string that swallows the following lines),
# so anything else on the line, and any quote character at all, refuses the template.
_DIRECTIVE_LINE = re.compile(
    rf"#[{_SPACE}]*(?:"
    rf"pragma[{_SPACE}]+warning[{_SPACE}]+(?:disable|restore)"
    rf"(?:[{_SPACE}]+[A-Za-z0-9_]+(?:[{_SPACE}]*,[{_SPACE}]*[A-Za-z0-9_]+)*)?"
    rf"|nullable[{_SPACE}]+(?:enable|disable|restore)(?:[{_SPACE}]+(?:annotations|warnings))?"
    rf")[{_SPACE}]*(?://[^\"']*)?"
)
_LITERAL_OPEN = re.compile(r'(?:\$@|@\$|@|\$+)?"')
_RAW_FIRST_LINE = re.compile(rf"[{_SPACE}]*(?:{_NEWLINE.pattern})")
# The escapes of C# 12, the language of the Roslyn the add-in ships (no \e).
_ESCAPE = re.compile(r"""\\(?:['"\\0abfnrtv]|x[0-9a-fA-F]{1,4}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8})""")
_EXPRESSION = re.compile(
    rf"[{_SPACE}\r\n\x85\u2028\u2029]+|@?[A-Za-z_]\w*|[0-9]+|[.,?+*/%&|^!~=<>-]"
)
_BRACES = re.compile(r"\{+")
_CLOSE_BRACES = re.compile(r"\}+")
_QUOTES = re.compile(r'"+')
_CLOSERS = {"(": ")", "[": "]", "{": "}"}

CODE = "code"
STRING = "ordinary string literal"


@dataclass(frozen=True)
class Token:
    context: str
    start: int
    end: int


@dataclass(frozen=True)
class Slot(Token):
    name: str


class _LexError(ValueError):
    def __init__(self, pos: int, reason: str):
        super().__init__(reason)
        self.pos = pos


def _at_line_start(code: str, pos: int) -> bool:
    i = pos
    while i > 0 and _DIRECTIVE_SPACE.fullmatch(code[i - 1]):
        i -= 1
    return not i or _NEWLINE.match(code, i - 1) is not None


def _directive_end(code: str, pos: int) -> int | None:
    """Apply one physical-line rule both before scanning and at each # token."""
    if not _at_line_start(code, pos):
        return None
    if not _ALLOWED_DIRECTIVE.match(code, pos):
        raise _LexError(pos, "conditional compilation and other directives are not allowed "
                             "in tool code (only #pragma and #nullable are supported)")
    newline = _NEWLINE.search(code, pos)
    end = newline.start() if newline else len(code)
    if not _DIRECTIVE_LINE.fullmatch(code, pos, end):
        raise _LexError(pos, "only '#pragma warning disable|restore <ids>' and "
                             "'#nullable enable|disable|restore' are supported, with no quotes on the line")
    return end


class Lexer:
    """Emit code spans and opaque literals/comments, including whole interpolations."""

    def __init__(self, code: str):
        self.code = code
        self.native_holes: set[int] = set()
        self.escape_slots: set[int] = set()

    def tokens(self) -> Iterator[Token]:
        # Check physical lines even inside opaque or otherwise unreachable text.
        for marker in re.finditer("#", self.code):
            _directive_end(self.code, marker.start())
        pos = 0
        while match := _TOKEN.search(self.code, pos):
            if pos < match.start():
                yield Token(CODE, pos, match.start())
            try:
                token = self._token(match)
            except RecursionError:
                raise _LexError(match.start(), "interpolation nesting limit exceeded") from None
            yield token
            pos = token.end
        if pos < len(self.code):
            yield Token(CODE, pos, len(self.code))

    def _token(self, match: re.Match) -> Token:
        code, opening, pos = self.code, match.group(), match.end()
        if opening.endswith("$"):
            raise _LexError(match.start(), "unsupported interpolated literal prefix")
        if opening == "#":
            end = _directive_end(code, match.start())
            if end is None:
                raise _LexError(match.start(), "unexpected mid-line # in C# code")
            kind = "preprocessor directive"
        elif opening == "//":
            newline = _NEWLINE.search(code, pos)
            end = newline.start() if newline else len(code)
            kind = "line comment"
        elif opening == "/*":
            close = code.find("*/", pos)
            if close < 0:
                raise _LexError(match.start(), "unterminated block comment")
            end = close + 2
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
            # Two literals that touch are never valid C#, and a value that renders as
            # nothing would let their quotes merge into a longer (raw) delimiter.
            if _LITERAL_OPEN.match(code, end):
                raise _LexError(end, "a string literal directly follows another literal")
        return Token(kind, match.start(), end)

    def _literal_end(self, pos: int, quote: str, quotes: int, verbatim: bool, dollars: int) -> int:
        code = self.code
        multiline_raw = quotes >= 3 and _RAW_FIRST_LINE.match(code, pos)
        while pos < len(code):
            ch = code[pos]
            if ch == quote:
                if quotes >= 3:
                    end = _QUOTES.match(code, pos).end()
                    if end - pos > quotes:
                        raise _LexError(pos, "ambiguous raw string delimiter")
                    if end - pos == quotes:
                        if multiline_raw and not _at_line_start(code, pos):
                            raise _LexError(pos, "misplaced multiline raw string delimiter")
                        return end
                    pos = end
                elif verbatim and code.startswith('""', pos):
                    pos += 2
                else:
                    return pos + 1
            elif quotes < 3 and not verbatim and ch == "\\":
                escape = _ESCAPE.match(code, pos)
                if escape:
                    # A following hex digit could extend a short C# \x escape.
                    if code[pos + 1] == "x" and escape.end() - pos < 6:
                        self.escape_slots.add(escape.end())
                    pos = escape.end()
                elif not dollars and quote == '"' and PLACEHOLDER_RE.match(code, pos + 1):
                    self.escape_slots.add(pos + 1)
                    pos += 1
                else:
                    raise _LexError(pos, "unsupported or unterminated literal escape")
            elif dollars and ch == "{":
                end = _BRACES.match(code, pos).end()
                count = end - pos
                pos = end
                # In regular interpolation pairs are literal. In a raw string
                # the last N braces open the hole, where N is the dollar count.
                opens_hole = count >= dollars if quotes >= 3 else count % 2 == 1
                if opens_hole:
                    if quotes >= 3 and count >= 2 * dollars:
                        raise _LexError(pos - count, "ambiguous raw interpolation braces")
                    hole = PLACEHOLDER_RE.match(code, pos - 1)
                    if hole and code.startswith("}" * dollars, hole.end() - 1):
                        self.native_holes.add(pos - 1)
                    pos = self._hole_end(pos, dollars, verbatim, quotes)
            elif dollars and ch == "}":
                end = _CLOSE_BRACES.match(code, pos).end()
                count = end - pos
                if (quotes >= 3 and count >= dollars) or (quotes < 3 and count % 2):
                    raise _LexError(pos, "unbalanced interpolation braces")
                pos = end
            elif not verbatim and not multiline_raw and _NEWLINE.match(code, pos):
                raise _LexError(pos, "unterminated literal before line end")
            else:
                pos += 1
        raise _LexError(pos, "unterminated literal")

    def _hole_end(self, pos: int, dollars: int, verbatim: bool, quotes: int) -> int:
        """Skip an expression, shielding nested literals/comments and balanced groups."""
        code = self.code
        groups = []
        while pos < len(code):
            symbol = code[pos]
            if opening := _TOKEN.match(code, pos):
                pos = self._token(opening).end
            elif symbol in _CLOSERS:
                groups.append(_CLOSERS[symbol])
                pos += 1
            elif symbol in (")", "]", "}"):
                if not groups and code.startswith("}" * dollars, pos):
                    return pos + dollars
                if not groups or groups.pop() != symbol:
                    raise _LexError(pos, "unbalanced interpolation expression")
                pos += 1
            elif symbol == ":":
                if not groups:
                    return self._format_end(pos + 1, dollars, verbatim, quotes)
                pos += 1
            elif token := _EXPRESSION.match(code, pos):
                pos = token.end()
            else:
                raise _LexError(pos, "unrecognized token in interpolation expression")
        raise _LexError(pos, "unterminated interpolation expression")

    def _format_end(self, pos: int, dollars: int, verbatim: bool, quotes: int) -> int:
        code = self.code
        while pos < len(code):
            if code.startswith("}" * dollars, pos):
                return pos + dollars
            if verbatim and code.startswith('""', pos):
                pos += 2
            elif quotes < 3 and not verbatim and (escape := _ESCAPE.match(code, pos)):
                pos = escape.end()
            elif (code[pos] in '{}"' or (not verbatim and _NEWLINE.match(code, pos))
                  or (quotes < 3 and not verbatim and code[pos] == "\\")):
                raise _LexError(pos, "unsupported or unterminated interpolation format")
            else:
                pos += 1
        raise _LexError(pos, "unterminated interpolation format")


def template_slots(code: str, declared: Mapping[str, dict]) -> tuple[list[Slot], list[str]]:
    """Return only original substitution sites. Native C# interpolation locals pass through."""
    if not isinstance(code, str):
        return [], ["code_template: must be a string"]
    try:
        lexer = Lexer(code)
        return _collect_slots(lexer, declared)
    except _LexError as exc:
        line = 1 + sum(1 for _ in _NEWLINE.finditer(code, 0, exc.pos))
        return [], [f"code_template: {exc} at line {line}"]


def _collect_slots(lexer: Lexer, declared: Mapping[str, dict]) -> tuple[list[Slot], list[str]]:
    code = lexer.code
    matches = iter(PLACEHOLDER_RE.finditer(code))
    match = next(matches, None)
    starts = [0, *(m.end() for m in _NEWLINE.finditer(code))]
    slots, errors = [], []
    for token in lexer.tokens():
        while match and match.start() < token.end:
            name, pos = match[1], match.start()
            context = token.context
            param = declared.get(name)
            if param is None and pos in lexer.native_holes:
                match = next(matches, None)
                continue
            if context == STRING and pos in lexer.escape_slots:
                context = "escape sequence in an ordinary string literal"
            reason = ""
            if param is None:
                reason = "is not a declared parameter"
            elif context == CODE and (ptype := str(param.get("type", "string")).lower()) in BARE_TYPES:
                if "default" in param:
                    try:
                        bare_literal(param["default"], ptype, declared_default=True)
                    except ValueError as exc:
                        reason = f"has an invalid default in bare code ({exc})"
            elif context != STRING:
                reason = "cannot be substituted in bare code" if context == CODE else "cannot be substituted here"
            if reason:
                line = bisect_right(starts, pos)
                errors.append(f"code_template: placeholder {{{name}}} {reason} at line {line}, "
                              f"column {pos - starts[line - 1] + 1} ({context}). "
                              f'Move it into an ordinary string literal: string x = "{{{name}}}";')
            else:
                slots.append(Slot(context, pos, match.end(), name))
            match = next(matches, None)
    return slots, errors
