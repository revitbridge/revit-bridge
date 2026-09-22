"""The two renderings of the model instructions: MCP server and confirming host."""
from __future__ import annotations

import re

import revit_bridge
import revit_bridge.mcp_server as server
from revit_bridge.instructions import host_instructions, server_instructions


def lines(text: str) -> list[str]:
    return text.strip().splitlines()


def test_both_renderings_fit_the_spec_budget():
    assert len(lines(server_instructions())) <= 60
    assert len(lines(host_instructions())) <= 60
    assert server.SERVER_INSTRUCTIONS == server_instructions()
    assert revit_bridge.host_instructions() == host_instructions()


def test_the_host_rendering_proposes_and_never_executes():
    """The difference is the execution paragraph; the flow, spec and notes are shared."""
    srv, host = server_instructions(), host_instructions()
    # server: the model confirms and executes with the token
    assert "confirm_spec(spec)" in srv and "run_tool(name, params, token)" in srv
    assert "execute_code(code, parameters, token)" in srv
    # host: the model proposes; confirmation and execution are the interface's
    assert "propose_spec(spec)" in host
    for call in ("confirm_spec(", "run_tool(", "execute_code("):
        assert call not in host, call
    assert "have no confirm_spec, run_tool or execute_code; you only propose the spec." in host
    # phase 6 spec 10.10 c: the host reports the execution in a user message, not a tool message
    assert "comes back to you as a message from the host" in host
    assert "tool message" not in host and "tool message" not in srv
    assert "propose_spec" not in srv
    # everything else is the same text
    for shared in ("## Flow (always, in this order)", "1. get_project_snapshot", "5. Show the spec card",
                   "## TaskSpec", 'default (evidence = "default:<tool>"', "## Never",
                   "- Guess a level, type, element id or coordinate: query, then ask.", "## Notes",
                   "Revit internal units are feet"):
        assert shared in srv and shared in host, shared
    # every numbered step is present once in each
    for text in (srv, host):
        assert [int(m) for m in re.findall(r"^(\d)\. ", text, flags=re.M)] == [1, 2, 3, 4, 5, 6, 7, 8]
