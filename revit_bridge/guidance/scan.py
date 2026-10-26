"""API-looking names in C# code, for matching lesson triggers without a model.

``code_symbols(code)`` is what ``check_code`` passes to ``recommend`` as
``symbols`` (and what an ``explain`` step can reuse):

1. Comments and string / char literals are blanked out first - a simple
   pass, not a C# lexer: ``//`` and ``/* */`` comments, regular, verbatim
   (``@"..."``), interpolated (``$"..."``, holes included) and raw
   (triple-quoted) strings, char literals. An unterminated literal is left
   as code.
2. Every identifier shaped ``[A-Z][A-Za-z0-9_]*`` that does not continue a
   word or a number (so ``1E5`` and ``0x1F`` give nothing) is kept once, in
   order of first appearance.
3. C# keywords (``CSHARP_KEYWORDS``, reserved and contextual) and the
   ``execute_code`` wrapper's own names (``WRAPPER_NAMES``) are dropped. All
   C# keywords are lower case, so with the capitalized pattern the keyword
   table is a guard; the wrapper names are what it removes in practice.

``recommend`` then matches the names against ``triggers.symbols`` as whole
words, case sensitively.
"""
from __future__ import annotations

import re

# Reserved keywords and contextual keywords (C# 13).
CSHARP_KEYWORDS: frozenset[str] = frozenset("""
abstract as base bool break byte case catch char checked class const continue decimal default
delegate do double else enum event explicit extern false finally fixed float for foreach goto if
implicit in int interface internal is lock long namespace new null object operator out override
params private protected public readonly ref return sbyte sealed short sizeof stackalloc static
string struct switch this throw true try typeof uint ulong unchecked unsafe ushort using virtual
void volatile while
add alias allows and args ascending async await by descending dynamic equals extension field file
from get global group init into join let managed nameof nint not notnull nuint on or orderby
partial record remove required scoped select set unmanaged value var when where with yield
""".split())

# The names the execute_code wrapper declares around the snippet: its
# namespace and class and the method the snippet is the body of
# (``AIGeneratedCode.CodeExecutor.Execute(Document document, object[] parameters)``).
WRAPPER_NAMES: frozenset[str] = frozenset({"AIGeneratedCode", "CodeExecutor", "Execute"})

_LITERALS_AND_COMMENTS = re.compile(
    r'//[^\n]*'                              # line comment
    r'|/\*.*?(?:\*/|\Z)'                     # block comment (an unclosed one runs to the end)
    r'|\$*"""[\s\S]*?"""'                    # raw string, C# 11
    r'|(?:\$@|@\$|@)"(?:[^"]|"")*"'          # verbatim string, "" escapes a quote
    r'|\$?"(?:[^"\\\n]|\\.)*"'               # regular or interpolated string
    r"|'(?:[^'\\\n]|\\.)*'",                 # char literal
    re.DOTALL,
)
_SYMBOL = re.compile(r"(?<![A-Za-z0-9_])[A-Z][A-Za-z0-9_]*")


def strip_literals_and_comments(code: str) -> str:
    """``code`` with every comment and string / char literal replaced by a space."""
    return _LITERALS_AND_COMMENTS.sub(" ", code or "")


def code_symbols(code: str | None) -> list[str]:
    """The distinct capitalized identifiers of ``code`` outside comments and literals,
    in order of first appearance, without C# keywords and the wrapper's names."""
    if not isinstance(code, str) or not code:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for name in _SYMBOL.findall(strip_literals_and_comments(code)):
        if name in CSHARP_KEYWORDS or name in WRAPPER_NAMES or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names
