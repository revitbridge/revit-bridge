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

Step 8 of the server rendering also says what to do with a failed run whose
``hint`` names get_guidance (E3: Revit threw an exception the hint names):
call it before fixing, then check_code and confirm again. To stay within 60
lines, the Notes name only the revit://connection-status resource.

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

``host_instructions(role=None, code=True, mentions=False)`` also renders,
from the same pieces, the host text without code-writing (``code=False``:
no execute_code, snippet contract or blocked_code). That host embeds the card
in the proposing reply, offers choices with ``ask_designer`` and labels the
confirmation button "确认并执行". The ``mentions=True`` option adds the designer's
selections block. A host that splits the work can request one shorter text per
role: ``analyst`` (route a message, one JSON object), ``planner`` (fill and
propose the spec) and ``supervisor`` (read and report the execution result).
With no arguments it returns the 0.8.2 host text, only the version in its
first line changed.
"""
from __future__ import annotations

import re
import textwrap
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
You are connected to a running Autodesk Revit through revit-bridge 0.9. The server
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
   If `hint` names get_guidance: call it before fixing, then check_code and confirm again.
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
- solidify_tool saves code that worked as a v1 pack. Resources: revit://connection-status.
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


# -- the host variants: no code, the designer's selections, one text per role ----------------
#
# host_instructions() with no arguments is _render("host"), the 0.8.2 text but for
# the version in its first line. Every variant below is cut from the same pieces
# (the flow steps are split out of _FLOW_HEAD and _FLOW_TAIL["host"], and _swap
# fails at import if a piece it edits has changed), so the variants cannot drift
# from it either.

ROLES = ("analyst", "planner", "supervisor")


def _swap(text: str, old: str, new: str) -> str:
    if old not in text:
        raise RuntimeError(f"instructions: {old!r} is no longer in the text it edits")
    return text.replace(old, new, 1)


def _steps(text: str) -> dict[str, str]:
    """The numbered steps of a flow piece, by number (a step runs to the next one)."""
    return {piece[0]: piece for piece in re.split(r"(?m)^(?=\d\. )", text) if piece[:1].isdigit()}


_HEAD_STEPS = _steps(_FLOW_HEAD)                 # 1 snapshot, 2 query, 3 questions, 4 reconcile
_HOST_STEPS = _steps(_FLOW_TAIL["host"])         # 6 propose_spec, 7 the host confirms, 8 the result


def _query_step(code: bool) -> str:
    """Step 2 with the kinds added after 0.8.2 (links, type_categories, args.link_id) and the rule
    that ids read through a link are the link's, not the open model's."""
    step = """\
2. query(kind, args) for any other read, no token: levels, grids, family_types, elements,
   selection, view_elements, units, counts, links, type_categories. With args.link_id (a
   picked link's id) levels, family_types, elements, counts, type_categories read that link;
   its ids are not the open model's: never pass them to a tool.
"""
    return _swap(step, "never pass them to a tool.\n",
                 "never pass them to a tool. Never write C# for a read.\n") if code else step


def _propose_step(code: bool, number: str = "6") -> str:
    step = _HOST_STEPS["6"]
    if not code:
        step = _swap(step, "unconfirmed_interpretation,\n   blocked_code. Fix", "unconfirmed_interpretation.\n   Fix")
    return _swap(step, "6. ", f"{number}. ")


_CARD_ACTION = 'Ask the designer to click "确认并执行" on the card inside the reply that submitted it.'
_CARD_LOCATION = ('If they cannot find it, say "卡片在我上一条提交它的回复里" in their language. '
                  'Do not submit it again.')


def _questions_step(code: bool) -> str:
    step = _HEAD_STEPS["3"]
    if code:
        return step
    return (step + "   Call ask_designer when you can offer choices.\n"
            "   Use options grounded in the facts you read. Present them on the form and end the turn.\n")


def _host_confirms_step(code: bool, number: str = "7") -> str:
    if code:
        return _swap(_HOST_STEPS["7"], "7. ", f"{number}. ")
    return (f"{number}. {_CARD_ACTION}\n"
            "   Only that click authorizes execution. A typed yes only points back to the card.\n"
            f"   {_CARD_LOCATION}\n"
            "   You have no confirm_spec or run_tool; you only propose the spec.\n")


def _intro(code: bool) -> str:
    if code:
        return _INTRO.format(closing=_INTRO_CLOSING["host"])
    text = _INTRO.format(closing="and propose it\nas a card in this chat.")
    return _swap(text, "capability packs and C# in Revit", "capability packs in Revit")


def _taskspec(code: bool) -> str:
    if code:
        return _TASKSPEC
    return _swap(_TASKSPEC, "{kind: run_tool|execute_code, tool|code}", "{kind: run_tool, tool}")


_NEVER_CODE = "- Write code: if no tool fits, say that no existing tool can do it.\n"

# Section 2.4 as amended: a picked value bound to a pack parameter that takes its value from the
# model (choices_from / source tool:*) is source tool, any other binding source designer; the
# evidence is the mention either way (spec/rules.py accepts exactly this).
# (A picked link's queries carry args.link_id: step 2 says so, next to the rule on its ids.)
_SELECTIONS = """\
## Designer's selections
Picks in "Designer's selections": element value = id as a decimal string; level/type/link value = the name.
Use evidence "mention:<kind>#<id>", source tool if the pack parameter has choices_from or tool:* source, else designer; never re-ask.
"""

_NEVER_CLAIM = "- Claim success on an error or on a failed validation; do not soften errors.\n"


def _never(code: bool) -> str:
    """The never-list (the planner keeps the reporting rule: it answers query-route requests and
    sees query errors)."""
    return _NEVER_HEAD + _NEVER_TAIL["host"] + ("" if code else _NEVER_CODE)


def _notes(code: bool) -> str:
    return _NOTES_HEAD + (SNIPPET_CONTRACT_NOTE if code else "")


def _render_host(code: bool, mentions: bool) -> str:
    """The whole host text without code-writing and/or with the selections rule."""
    card = _FLOW_CARD["host"] if code else "5. Summarize the ready spec for the card embedded in this reply.\n"
    flow = (_FLOW_TITLE + _HEAD_STEPS["1"] + _query_step(code) + _questions_step(code) + _HEAD_STEPS["4"]
            + _FLOW_CHECK["host"] + card + _propose_step(code) + _host_confirms_step(code)
            + _HOST_STEPS["8"])
    sections = [_intro(code), flow, _taskspec(code)]
    if mentions:
        sections.append(_SELECTIONS)
    sections += [_never(code), _notes(code)]
    return "\n".join(sections)


def _render_analyst(code: bool) -> str:
    none_rule = "" if code else " Never offer to write code."
    card_rule = "" if code else (
        " With a pending card, route plain assent without changes or a question about its location to chat.\n"
        f"  {_CARD_ACTION} {_CARD_LOCATION}\n"
        "  If the reply asks for a change, route it by what it requests: query, tool, or none.")
    return f"""\
You are the analyst of a Revit host built on revit-bridge 0.9. You read one message
from the designer and route it; you call no tools. You get the message, a
"Designer's selections" block when the designer picked values in the host, a
digest of the open model and the tool list (a name and one line each).

## Routes
- chat: small talk, or a question about Revit or this host that needs nothing from
  the model. Answer it in `note`.{card_rule}
- query: the designer only wants to know something about the model (levels, types,
  counts, elements, links, selection); nothing changes.
- tool: a tool in the list does what is asked. Name it in `tool`.
- none: the model has to change but no tool in the list does it; `note` tells the
  designer that no existing tool does it and what is missing.{none_rule}

## Output
Your reply is exactly one JSON object and nothing else, no text before or after it:
{{"route": "chat" | "query" | "tool" | "none", "tool": "<name>", "facts_needed": ["..."], "note": "..."}}
- `tool` only with route tool, and only a name from the list; never invent one.
- `facts_needed`: what still has to be read from the model or asked of the designer
  before the request can run (a level, a type, an element, a point, a dimension).
  What the designer said, and every value in the selections block, is known: leave it out.
- `note`: with chat and none, the reply the host shows the designer, in their language;
  with query and tool, at most two sentences for the next step.

## Never
- Guess a level, type, element id or coordinate: put it in facts_needed.
- Say that anything was executed or changed.
"""


def _render_planner(code: bool) -> str:
    intro = _intro(code)
    if code:
        intro = _swap(intro, _INTRO_CLOSING["host"],
                      "and propose\nit; the designer confirms it in this host's interface and the host runs it.")
    intro = _swap(intro, "You are connected to a running Autodesk Revit through revit-bridge 0.9.",
                  "You are the planner of a Revit host built on revit-bridge 0.9.")
    intro += ("You start from the analyst's routing {route, tool?, facts_needed, note}. Route\n"
              "query: read with get_project_snapshot and query, answer, no spec. Route tool:\n"
              "start from its tool and facts_needed; list_tools decides.\n")
    flow = (_FLOW_TITLE + _HEAD_STEPS["1"] + _query_step(code) + _questions_step(code) + _HEAD_STEPS["4"]
            + _FLOW_CHECK["host"] + _propose_step(code, "5") + _host_confirms_step(code, "6"))
    return "\n".join([intro, flow, _taskspec(code), _SELECTIONS, _never(code), _notes(code)])


def _render_supervisor(code: bool) -> str:
    report = " ".join(_HOST_STEPS["8"].split()[1:])            # step 8 without its number, on one line
    report = _swap(report, "The execution comes back to you as a message from the host. ", "")
    location = "in this host's interface" if code else 'by clicking "确认并执行" on the card in this chat'
    ask_form = "" if code else " The host shows your question as a form in this reply."
    return f"""\
You are the supervisor of a Revit host built on revit-bridge 0.9. The designer
confirmed a TaskSpec {location} and the host ran exactly what was
confirmed. You get the confirmed spec and the execution result as a message from
the host, and you tell the designer what happened.

## Reading the result
{textwrap.fill(report, width=84, initial_indent="- ", subsequent_indent="  ")}
- query(kind, args) may check a fact in the model; it only reads.

## Output
Your reply is exactly one JSON object and nothing else, no text before or after it:
{{"verdict": "done" | "retry" | "ask", "report": "...", "fix": "...", "question": "..."}}
- `report`: what happened, for the designer, in their language, faithful to the result
  and the validator.
- done: Revit succeeded and the validator passed.
- retry: it failed and a different parameter value or tool choice can fix it (a level,
  type or element the model does not have, a unit, a value out of range). `fix` says
  what the planner should change. The host sends one task back at most twice.
- ask: anything else, after the second retry, or a validator failure you cannot
  explain (never done). `question` is what to ask the designer; never choose a new
  value yourself.{ask_form}
- `fix` only with retry, `question` only with ask.

## Never
{_NEVER_CLAIM}- Say that anything was executed beyond what the host's result reports.
- Propose, confirm or run a spec yourself.
"""


_ROLE_RENDERERS = {"analyst": _render_analyst, "planner": _render_planner, "supervisor": _render_supervisor}


def host_instructions(role: str | None = None, code: bool = True, mentions: bool = False, *,
                      confirm_button_label: str = "确认并执行") -> str:
    """The instructions for a host whose interface does the confirmation and execution.

    With no arguments: the whole text, as in 0.8.2 but for the version in its
    first line. ``code=False`` leaves out writing code (execute_code, the
    snippet contract, blocked_code) and describes a card embedded in the proposing
    reply. The planner offers choices with ``ask_designer``, and the designer
    clicks the host's ``confirm_button_label`` on the card to confirm and run it.
    ``mentions=True`` adds the rule for a "Designer's selections" block. Any
    variant other than the default also names the query kinds added later
    (links, type_categories, link_id).

    ``role`` gives one role's text instead: ``analyst`` (route the message:
    one JSON object ``{route: chat | query | tool | none, tool?, facts_needed,
    note}``, ``note`` being the designer's reply with chat and none),
    ``planner`` (read the model, fill the parameters, propose the spec; always
    with the selections rule) or ``supervisor`` (read the execution result and
    the validator's verdict: one JSON object ``{verdict: done | retry | ask,
    report, fix?, question?}``). ``mentions`` does not apply to a role;
    ``code`` does.

    The selections rule uses the decimal id string for an element and the
    name for a level, type or link. Each pick carries evidence
    ``mention:<kind>#<id>``, source tool when the pack parameter has
    ``choices_from`` or a ``tool:*`` source, else designer; never asked again.
    """
    if role is None:
        if code and not mentions:
            return _render("host")
        text = _render_host(bool(code), bool(mentions))
    else:
        if role not in _ROLE_RENDERERS:
            raise ValueError(f"unknown role {role!r}: expected one of {', '.join(ROLES)}")
        text = _ROLE_RENDERERS[role](bool(code))
    return text if code else text.replace("确认并执行", confirm_button_label)
