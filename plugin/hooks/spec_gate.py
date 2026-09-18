#!/usr/bin/env python3
"""PreToolUse gate for the revit-bridge plugin (hook v0).

Reads the hook JSON from stdin. When the tool is revit-bridge's execute_code
or run_tool and tool_input.spec_confirmed is not true, the call is denied with
a reason that tells the model to confirm the task spec with the designer first.
Everything else is allowed. Standard library only, ASCII only. Any unexpected
error fails open (exit 0) so a bug here never blocks unrelated work.
"""

import json
import re
import sys

TOOL_PATTERN = re.compile(r"^mcp__(plugin_revit-bridge_)?revit-bridge__(execute_code|run_tool)$")

REASON = (
    "revit-bridge spec gate: spec_confirmed is not true. First show the designer "
    "the task spec (every parameter with its value and its source: the designer's "
    "words, a tool result, an answer to your question, or preference:<name>), get "
    "an explicit confirmation, then call again with spec_confirmed=true. Never set "
    "spec_confirmed=true on your own."
)


def decide(tool_name, tool_input):
    """Return a deny reason, or None to allow."""
    if not TOOL_PATTERN.match(tool_name or ""):
        return None
    value = (tool_input or {}).get("spec_confirmed")
    if value is True:
        return None
    if isinstance(value, str) and value.strip().lower() == "true":
        return None
    return REASON


def main():
    try:
        # Bytes in, decoded as UTF-8; a leading BOM (PowerShell pipes) is tolerated.
        raw = sys.stdin.buffer.read().decode("utf-8-sig")
        data = json.loads(raw) if raw.strip() else {}
        reason = decide(data.get("tool_name", ""), data.get("tool_input") or {})
    except Exception as exc:  # fail open
        sys.stderr.write("spec_gate: error, allowing: %r\n" % (exc,))
        return 0
    if reason:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
        sys.stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
