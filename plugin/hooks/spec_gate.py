#!/usr/bin/env python3
"""PreToolUse gate for the revit-bridge plugin (hook v1).

Reads the hook JSON from stdin. When the tool is revit-bridge's execute_code
or run_tool and tool_input.token is not a non-empty string, the call is
denied with a reason that tells the model to obtain a confirmation token from
confirm_spec first. Everything else is allowed. The server checks the token
itself (one-time, expiry, bound to the exact tool and parameters); this hook
only stops calls that never asked. Standard library only, ASCII only. Any
unexpected error fails open (exit 0) so a bug here never blocks unrelated work.
"""

import json
import re
import sys

TOOL_PATTERN = re.compile(r"^mcp__(plugin_revit-bridge_)?revit-bridge__(execute_code|run_tool)$")

REASON = (
    "revit-bridge gate: no confirmation token. First show the designer the spec "
    "card (every parameter with its value and its source: the designer's words, "
    "a tool result, an answer to your question, preference:<name> or a declared "
    "default), get an explicit confirmation, call confirm_spec with the TaskSpec "
    "to obtain a token, then call again with token=<token>. Never invent a token."
)


def decide(tool_name, tool_input):
    """Return a deny reason, or None to allow."""
    if not TOOL_PATTERN.match(tool_name or ""):
        return None
    token = (tool_input or {}).get("token")
    if isinstance(token, str) and token.strip():
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
