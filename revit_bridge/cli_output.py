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

Before any of that, ``safe_streams`` makes the standard streams safe for the whole CLI call
(``mcp_server.main``): a ``sys.stdout`` or ``sys.stderr`` that is None (pythonw, a host
without a console) or closed is replaced by a sink and put back afterwards, so nothing that
writes or probes them can raise - argparse's help, version and error output, and on Python
3.14 its colour probe, which calls ``fileno()`` and ``isatty()`` on the stream while the
parser is being built. A usable stream is never touched: ``serve`` keeps its real stdout and
a terminal keeps its colours.
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import contextmanager


class _Sink(io.TextIOBase):
    """Takes every write and drops it; no terminal, no file descriptor (``fileno`` raises
    ``io.UnsupportedOperation``, as for any in-memory stream), never closed."""

    encoding = "utf-8"
    errors = "strict"

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        return len(text)

    def isatty(self) -> bool:
        return False


def usable(stream) -> bool:
    """True for a standard stream output can go to: present and not closed."""
    if stream is None:
        return False
    try:
        return not getattr(stream, "closed", False)
    except Exception:  # noqa: BLE001 - a stream that cannot even say whether it is closed
        return False


@contextmanager
def safe_streams():
    """For one CLI call: ``sys.stdout`` / ``sys.stderr`` that is None or closed is replaced by
    a sink and the caller's object put back afterwards; a usable one is left exactly as it is.
    Yields the names of the streams that were replaced (``{"stdout"}``, ...)."""
    replaced = {}
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if not usable(stream):
            replaced[name] = stream
            setattr(sys, name, _Sink())
    try:
        yield set(replaced)
    finally:
        for name, stream in replaced.items():
            setattr(sys, name, stream)


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
