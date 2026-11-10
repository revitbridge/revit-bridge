"""How the CLI subcommands write: one rule for ``check``, ``guidance`` and ``skills``.

The rule: stdout and stderr are UTF-8 with ``errors="backslashreplace"``;
``check``'s JSON is ASCII (``ensure_ascii``) unless stdout is an interactive
console.

Who reads each stream decides it:

- an interactive console: on Windows Python writes through the console's
  wide-character API, so every character shows whatever the code page (UTF-8
  is what that path expects anyway), and JSON stays readable - a Chinese
  document title shows as itself;
- a pipe or a file: left alone, Python encodes with the ANSI code page (GBK on
  a Chinese Windows), which cannot encode every character - a document title
  outside GBK made ``check`` raise with nothing printed - while the hosts and
  agents that capture the bytes decode UTF-8. ``backslashreplace`` leaves no
  character that can make a write fail;
- JSON on a pipe or a file is ASCII: the same bytes in every code page, so it
  parses whatever the reader decodes with (Windows PowerShell 5.1 decodes a
  native command's output with the console's OEM code page; UTF-8 read that
  way can swallow the backslash of an escape and stop being JSON), and its
  escapes still give back the exact characters.

``serve`` does not come here: its stdout is the MCP transport.
"""
from __future__ import annotations

import json
import sys


def configure() -> None:
    """Apply the rule to ``sys.stdout`` and ``sys.stderr``. A stream that cannot be
    reconfigured is left as it is; ``write`` and ``write_json`` still never raise."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError, OSError):    # not a text stream we can change, or closed
            pass


def interactive() -> bool:
    """True when stdout is an interactive console (not a pipe, not a file)."""
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _emit(text: str, fallback: str) -> None:
    """``text`` on stdout, or ``fallback`` (ASCII) when the stream cannot encode it. No stdout
    (pythonw, a host without a console: None), a closed one or a broken pipe: nothing is
    written and nothing is raised - output never fails the command."""
    out = sys.stdout
    if out is None:
        return
    try:
        try:
            out.write(text)
        except UnicodeEncodeError:
            out.write(fallback)
    except (AttributeError, ValueError, OSError):        # not a stream, closed, or a pipe gone away
        pass


def write(text: str = "") -> None:
    """One line on stdout; a stream that cannot encode a character gets its escape."""
    _emit(text + "\n", text.encode("ascii", "backslashreplace").decode("ascii") + "\n")


def write_json(data) -> None:
    """``data`` as indented JSON on stdout: readable on a console, ASCII anywhere else."""
    _emit(json.dumps(data, indent=2, ensure_ascii=not interactive()) + "\n",
          json.dumps(data, indent=2, ensure_ascii=True) + "\n")   # a console whose encoding stayed narrow
