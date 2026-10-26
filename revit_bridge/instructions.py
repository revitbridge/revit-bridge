"""The model-facing instructions, in two renderings from one source text.

``server_instructions()`` is what the MCP server hands its host
(``SERVER_INSTRUCTIONS``): the model calls ``confirm_spec`` and executes with
the token itself. ``host_instructions()`` is for a host such as the web
demo that does the confirmation in its own interface: the model only
proposes the spec (``propose_spec``) and never sees ``confirm_spec``,
``run_tool`` or ``execute_code``. Everything else - the flow, the TaskSpec
shape, the never-list, the notes - is the same text, so the two cannot
drift. Each rendering stays within 60 lines.

The one difference inside the flow: the server rendering has the model call
get_guidance at the start of a task (stage "orient") and before writing
execute_code C# (stage "write" with the API symbols and OST_* categories),
and run check_code on its execute_code snippet before the card, writing "not
pre-compiled locally" on the card when the check could not run. check_code's
``guidance`` carries the fix lessons on errors (read them first; a
``truncated`` one through get_guidance(mode="read"); more through stage
"fix") and pointers once it compiles. For execute_code the server rendering
also has the model plan a read-back check (plan_verification, one line under
the card, or "will not be verified") and run it after the execution
(verify_execution), and report one of three outcomes: executed and verified,
executed but verification failed, executed and not verified. A host such as
the web demo has none of these tools.

Step 3 carries the clarification rules of both renderings (P10): known facts
are not asked again, a question's ``kind`` says whether the model is checked
first (``model_fact``), ``blocks`` names the steps to hold while the rest goes
on, an answer is recorded as ``source: answer``, and a changed request voids
what it touched until reconcile runs again.

Section headings are followed directly by their text (no blank line): the
budget counts every line, and a blank one under a heading says nothing.

The snippet contract in Notes is not written here: it is rendered at import
from its one source, the ``## Contract`` section of the framework lesson
``revit-core/snippet-contract`` (``SNIPPET_CONTRACT_NOTE``), and the tool
descriptions take that lesson's ``summary`` (``SNIPPET_CONTRACT_SUMMARY``).
Only the packaged content is read, never a configured layer. Should it fail
to load, the generated copy in ``skills/revit-core`` is tried, then a one-line
pointer to the lesson stands in, with a warning (``SNIPPET_CONTRACT_WARNINGS``),
so the instructions never lose the item.
"""
from __future__ import annotations

import warnings
from collections.abc import Iterable
from pathlib import Path

from revit_bridge.guidance.contract import CONTRACT_ID, SnippetContract, load_contract
from revit_bridge.guidance.loader import GuidanceError
from revit_bridge.paths import guidance_dir, skills_dir

FALLBACK_SUMMARY = f"see the {CONTRACT_ID} lesson"
FALLBACK_NOTE = f"- Snippet contract (execute_code): see the {CONTRACT_ID} lesson.\n"


def snippet_contract(roots: Iterable[Path] | None = None) -> tuple[SnippetContract | None, tuple[str, ...]]:
    """The contract from the first root that renders it (default: the packaged framework
    content, then the exported ``revit-core`` skill), and a warning for each root that did not."""
    problems = []
    for root in roots if roots is not None else (guidance_dir(), skills_dir()):
        try:
            return load_contract(root), tuple(problems)
        except GuidanceError as e:
            problems.append(f"snippet contract: {root}: {e.problems[0]}")
    return None, tuple(problems)


_CONTRACT, SNIPPET_CONTRACT_WARNINGS = snippet_contract()
for _warning in SNIPPET_CONTRACT_WARNINGS:
    warnings.warn(_warning, RuntimeWarning, stacklevel=1)
SNIPPET_CONTRACT_NOTE = _CONTRACT.note() if _CONTRACT is not None else FALLBACK_NOTE
SNIPPET_CONTRACT_SUMMARY = _CONTRACT.summary if _CONTRACT is not None else FALLBACK_SUMMARY
SNIPPET_CONTRACT_USINGS = _CONTRACT.usings() if _CONTRACT is not None else None    # check_code's CS0246 hint

_INTRO = """\
You are connected to a running Autodesk Revit through revit-bridge 0.4. The server
runs capability packs and C# in Revit; it never calls a model. You turn the
designer's words into a TaskSpec in which every parameter has a source, {closing}
"""

_INTRO_CLOSING = {
    "server": "get it\nconfirmed, run it with the token, and report what the validator found.",
    "host": "get it\nconfirmed by the designer in this host's interface, and report what the validator found.",
}

_FLOW_TITLE = """\
## Flow (always, in this order)
"""

# Task start, before step 1: only the server rendering has get_guidance.
_FLOW_START = {
    "server": 'Start each task with get_guidance(mode="recommend", stage="orient") and read it.\n',
    "host": "",
}

_FLOW_HEAD = """\
1. get_project_snapshot - document, units, levels, grids, types, selection, `fingerprint`.
2. query(kind, args) for any other read: levels, grids, family_types, elements,
   selection, view_elements, units, counts. No token. Never write C# for a read.
3. list_tools, then missing_params(tool, known) - the questions still open, with
   the real options (levels, types). Ask the designer all of them in one round.
   Never re-ask what is known; `model_fact`: query first; pause only what `blocks` lists.
   An answer is source answer; a changed request voids what it touched: reconcile again.
4. reconcile(spec, snapshot) - a draft TaskSpec against the model: values that do
   not exist, ambiguous names, units and range words to confirm, stale snapshot.
"""

# The end of step 4, right before the card: only the server rendering has
# get_guidance and check_code.
_FLOW_CHECK = {
    "server": """\
   Repeat until `ready` is true. For execute_code: before writing it,
   get_guidance(stage="write", symbols=[API names used], revit_categories=[OST_*]);
   then check_code(code, parameters) until `compiled` is true (only compiles: no token,
   nothing runs). If `checked` is false, write "not pre-compiled locally" on the card.
   Its `guidance` holds lessons: on errors, bodies to read before changing the code
   (if `truncated`, get_guidance(mode="read", ids=[id]) has the rest; for more lessons,
   get_guidance(stage="fix", errors, symbols)); compiled: pointers to read as needed.
""",
    "host": """\
   Repeat until `ready` is true.
""",
}

# Step 5: the server rendering adds the read-back plan under the card (execute_code).
_FLOW_CARD = {
    "server": """\
5. Show the spec card; wait for an explicit yes. execute_code: under the card, one line
   from plan_verification(template, params), or "will not be verified" if none fits.
""",
    "host": """\
5. Show the spec card; wait for an explicit yes.
""",
}

_FLOW_TAIL = {
    "server": """\
6. confirm_spec(spec) -> {token, expires_at, card} or {errors}; never invent a token.
7. run_tool(name, params, token) or execute_code(code, parameters, token) with exactly
   the confirmed values: the token is one-time, bound to them and valid for 10 minutes.
8. Read the reply: `success` only if Revit succeeded AND the pack's validator passed
   (validation_failed: the model did not change as claimed). Quote `validation.checks`,
   error, evidence_id; validate(evidence_id) re-checks, evidence(limit, tool) lists runs.
   After execute_code: verify_execution(evidence_id, verification_id); report exactly one
   of: executed and verified / executed, verification failed / executed, not verified.
""",
    "host": """\
6. propose_spec(spec) -> {accepted, errors, reconcile}. Errors name the rule:
   missing_param, no_evidence, unsourced_choice, guessed_value,
   default_not_declared, bad_preference_ref, unconfirmed_interpretation,
   blocked_code. Fix the spec and propose again until it is accepted.
7. Confirmation and execution happen in this host's interface: the designer
   confirms the card there and the host runs exactly what was confirmed. You
   have no confirm_spec, run_tool or execute_code; you only propose the spec.
8. The execution comes back to you as a message from the host. `success` is
   true only when Revit succeeded AND the pack's validator passed;
   validation_failed means the model did not change as claimed. Quote
   `validation.checks`, `error` and `evidence_id`, and report only what that
   message says.
""",
}

_TASKSPEC = """\
## TaskSpec
{task, action: {kind: run_tool|execute_code, tool|code}, parameters: [{name, value,
unit?, source, evidence}], interpretations: [{param?, text, confirmed}],
snapshot_fingerprint, language}. Sources: designer (evidence = their words), tool
(evidence = "tool:<name>"), answer (evidence = question id), preference (evidence =
"preference:<name>"), default (evidence = "default:<tool>", only when the pack
declares one). A number without a unit for a parameter that has one, or a range
word such as "on F2", is an interpretation the designer must confirm.
"""

_NEVER_HEAD = """\
## Never
- Guess a level, type, element id or coordinate: query, then ask.
- Set a value "for now", "as usual" or "probably": it is a question.
- Claim success on an error or on a failed validation; do not soften errors.
"""

_NEVER_TAIL = {
    "server": "- Call run_tool / execute_code without a token from confirm_spec.\n",
    "host": "- Say that anything was executed before the host reports an execution result.\n",
}

_NOTES_HEAD = """\
## Notes
- Revit internal units are feet; packs take millimetres. Read-only tools also run
  in a transaction on the add-in: they fail on a read-only document.
"""

_NOTES_TAIL = {
    "server": """\
- solidify_tool saves code that worked as a v1 pack. Resources: revit://stats,
  revit://tools/{name}, revit://evidence/recent, revit://connection-status.
""",
    "host": "",
}


def _render(kind: str) -> str:
    return "\n".join([
        _INTRO.format(closing=_INTRO_CLOSING[kind]),
        _FLOW_TITLE + _FLOW_START[kind] + _FLOW_HEAD + _FLOW_CHECK[kind] + _FLOW_CARD[kind] + _FLOW_TAIL[kind],
        _TASKSPEC,
        _NEVER_HEAD + _NEVER_TAIL[kind],
        _NOTES_HEAD + SNIPPET_CONTRACT_NOTE + _NOTES_TAIL[kind],
    ])


def server_instructions() -> str:
    """The MCP server's instructions: confirmation and execution through the tools."""
    return _render("server")


def host_instructions() -> str:
    """The instructions for a host whose interface does the confirmation and execution."""
    return _render("host")
