"""
Code Sandbox - static security review for generated C# code.

Blocks dangerous patterns (file I/O, network, process execution, reflection)
before sending code to Revit for execution.

IMPORTANT: This static blacklist is only ONE layer of defense-in-depth. It is
inherently easy to bypass (encoding tricks, reflection chains, aliasing, etc.)
and must NOT be relied upon as the security boundary. The real isolation MUST
live on the Revit plugin side (assembly whitelist + human confirmation +
symbol-level static checks before Roslyn compilation). Treat any match here as
a strong signal to reject, but never assume a "safe" result means the code is
actually safe.
"""
from __future__ import annotations

import re
import unicodedata


# Patterns that should NOT appear in generated Revit code
_BLACKLIST: list[tuple[str, str]] = [
    (r"\bSystem\.IO\b", "System.IO (file operations)"),
    (r"\bFile\.(Read|Write|Delete|Copy|Move|Create|Open|Append)", "File I/O operations"),
    (r"\bDirectory\.(Create|Delete|Move|GetFiles)", "Directory operations"),
    (r"\bStreamReader\b|\bStreamWriter\b", "Stream file I/O"),
    (r"\bSystem\.Net\b", "System.Net (network operations)"),
    (r"\bHttpClient\b|\bWebClient\b|\bWebRequest\b", "HTTP/network client"),
    (r"\bSystem\.Diagnostics\.Process\b", "Process execution"),
    (r"\bProcess\.Start\b", "Process.Start"),
    (r"\bAssembly\.Load", "Dynamic assembly loading"),
    (r"\.Assembly\b", "Assembly reflection access"),
    (r"\bActivator\b", "Dynamic type instantiation via Activator"),
    (r"\bType\.GetType\b", "Reflection type lookup"),
    (r"\bSystem\.Reflection\b", "Reflection namespace"),
    (r"\bAppDomain\b", "AppDomain manipulation"),
    (r"\bMarshal\b", "System.Runtime.InteropServices.Marshal"),
    (r"\bGCHandle\b", "GCHandle (unmanaged memory pinning)"),
    (r"\bDllImport\b", "P/Invoke via DllImport"),
    (r"\bdynamic\b", "dynamic keyword (bypasses static analysis)"),
    (r"\bunsafe\b", "unsafe code block"),
    (r"\bSystem\.Threading\b", "System.Threading (thread manipulation)"),
    (r"\bEnvironment\.(Exit|GetEnvironmentVariable|SetEnvironmentVariable)",
     "Environment manipulation"),
    (r"\bRegistry\b", "Windows Registry access"),
]


_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})|\\U([0-9a-fA-F]{8})")


def _decode_escape(match: re.Match) -> str:
    point = int(match.group(1) or match.group(2), 16)
    # A surrogate half or an out-of-range value names no character on its own.
    return chr(point) if point < 0x110000 and not 0xD800 <= point <= 0xDFFF else match.group()


def _without_format_chars(text: str) -> str:
    if text.isascii():
        return text
    return "".join(char for char in text if char.isascii() or unicodedata.category(char) != "Cf")


def review(code: str) -> tuple[bool, list[str]]:
    """
    Review generated C# code for dangerous patterns.

    Returns:
        (safe, warnings) - safe=True if no dangerous patterns found.
    """
    # C# reads a unicode escape in an identifier as the character it names and drops format
    # characters (a soft hyphen, a zero-width joiner) from identifiers, and a rendered value
    # is written with unicode escapes. Match the text as the compiler reads it as well.
    readable = _without_format_chars(_UNICODE_ESCAPE.sub(_decode_escape, code))
    warnings: list[str] = []
    for pattern, description in _BLACKLIST:
        if re.search(pattern, code) or re.search(pattern, readable):
            warnings.append(f"Blocked pattern: {description}")

    return (len(warnings) == 0, warnings)
