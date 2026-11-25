"""The two renderings of the model instructions: MCP server and confirming host."""
from __future__ import annotations

import re

import pytest

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


def test_both_renderings_carry_the_snippet_contract_and_the_version():
    for text in (server_instructions(), host_instructions()):
        assert "revit-bridge 0.9." in text and "revit-bridge 0.8" not in text
        notes = text[text.index("## Notes"):]
        for term in ("Snippet contract (execute_code)", "`document` and `parameters`",
                     "already inside a transaction (never open one)", "System, System.Linq",
                     "System.Collections.Generic", "Autodesk.Revit.DB", "Autodesk.Revit.UI",
                     "RevitAPI and RevitAPIUI", "(no JSON library)", "IO, Process, Assembly, DllImport",
                     "File, Registry, reflection", "threading are refused", "`return <object>;`",
                     "categories are OST_*"):
            assert term in notes, term


def test_the_server_checks_code_before_the_card_the_host_has_no_check_code():
    srv, host = server_instructions(), host_instructions()
    check = srv.index("check_code(code, parameters)")
    assert srv.index("4. reconcile(") < check < srv.index("5. Show the spec card")
    assert "until `compiled` is true" in srv and '"not pre-compiled locally" on the card' in srv
    assert "check_code" not in host and "Repeat until `ready` is true." in host


def test_the_server_calls_get_guidance_at_three_points_the_host_never():
    srv, host = server_instructions(), host_instructions()
    orient = srv.index('get_guidance(mode="recommend", stage="orient")')
    write = srv.index('get_guidance(stage="write", symbols=[API names used], revit_categories=[OST_*])')
    check = srv.index("check_code(code, parameters)")
    fix = srv.index('get_guidance(stage="fix", errors, symbols)')
    assert srv.index("## Flow (always, in this order)") < orient < srv.index("1. get_project_snapshot")
    assert srv.index("4. reconcile(") < write < check < fix < srv.index("5. Show the spec card")
    assert "get_guidance" not in host


def test_the_skill_carries_the_same_guidance_calls():
    import pathlib

    skill = (pathlib.Path(__file__).resolve().parents[1] / "plugin" / "skills" / "revit-bridge" / "SKILL.md")
    text = skill.read_text(encoding="utf-8")
    orient = text.index('get_guidance(mode="recommend", stage="orient")')
    write = text.index('get_guidance(stage="write"')
    check = text.index("call `check_code(code, parameters)`")
    fix = text.index('get_guidance(stage="fix"')
    assert text.index("## The bridge flow") < orient < text.index("1. **Snapshot**")
    assert text.index("5. **Reconcile**") < write < check < fix < text.index("6. **Confirm**")


def test_the_server_reads_check_code_guidance_before_changing_the_code():
    srv, host = server_instructions(), host_instructions()
    check = srv.index("check_code(code, parameters)")
    lessons = srv.index("Its `guidance` holds lessons: on errors, bodies to read before changing the code")
    read = srv.index('(if `truncated`, get_guidance(mode="read", ids=[id]) has the rest')
    fix = srv.index('get_guidance(stage="fix", errors, symbols)')
    pointers = srv.index("compiled: pointers to read as needed.")
    assert check < lessons < read < fix < pointers < srv.index("5. Show the spec card")
    assert "`guidance`" not in host and "truncated" not in host


def test_the_skill_reads_check_code_guidance_before_changing_the_code():
    import pathlib

    skill = (pathlib.Path(__file__).resolve().parents[1] / "plugin" / "skills" / "revit-bridge" / "SKILL.md")
    text = skill.read_text(encoding="utf-8")
    check = text.index("call `check_code(code, parameters)`")
    lessons = text.index("Its `guidance` holds lessons")
    bodies = text.index("the bodies of up to two fix lessons - read them before changing the code")
    truncated = text.index('a `truncated` one ends with the `get_guidance(mode="read", ids=[...])` call')
    fix = text.index('get_guidance(stage="fix"')
    pointers = text.index("When the code compiles, `guidance` lists pointers (`summary`, no body)")
    assert check < lessons < bodies < truncated < fix < pointers < text.index("6. **Confirm**")


# -- P9: read-back verification of execute_code (server rendering and skill) --------------

def test_the_server_plans_and_verifies_execute_code_the_host_does_not():
    srv, host = server_instructions(), host_instructions()
    plan = srv.index("plan_verification(template, params)")
    assert srv.index("5. Show the spec card") < plan < srv.index("6. confirm_spec(spec)")
    assert 'or "will not be verified" if none fits.' in srv
    verify = srv.index("verify_execution(evidence_id, verification_id)")
    assert srv.index("7. run_tool(name, params, token)") < verify < srv.index("## TaskSpec")
    flat = " ".join(srv.split())
    assert ("report exactly one of: executed and verified / executed, verification failed / executed, "
            "not verified.") in flat
    for name in ("plan_verification", "verify_execution", "will not be verified"):
        assert name not in host, name
    host_lines = lines(host)
    step5 = host_lines.index("5. Show the spec card; wait for an explicit yes.")
    assert host_lines[step5 + 1].startswith("6. propose_spec")
    for text in (srv, host):
        rendered = lines(text)
        assert len(rendered) <= 60
        for i, line in enumerate(rendered):
            if line.startswith("## "):
                assert rendered[i + 1].strip(), line                  # no blank line under a heading


def test_the_skill_plans_and_reports_the_verification():
    import pathlib

    skill = (pathlib.Path(__file__).resolve().parents[1] / "plugin" / "skills" / "revit-bridge" / "SKILL.md")
    text = skill.read_text(encoding="utf-8")
    plan = text.index("`plan_verification(template, params)`")
    assert text.index("6. **Confirm**") < plan < text.index("Then show the spec card")
    assert text.index("Then show the spec card") < text.index("Then `confirm_spec(spec)`")
    for template in ("`created_ids`", "`count_delta`", "`param_equals`"):
        assert plan < text.index(template, plan) < text.index("7. **Execute**"), template
    assert "`Verification: none - will not be verified`" in text
    verify = text.index("`verify_execution(evidence_id, verification_id)`")
    assert text.index("8. **Verify and report**") < verify < text.index("## Parameter source protocol")
    report = text[verify:text.index("## Parameter source protocol")]
    for outcome in ("**executed and verified**", "**executed but verification failed**",
                    "**executed, not verified**"):
        assert outcome in report, outcome
    assert "never call an execution verified unless `verify_execution` returned `verified: true`" in text
    assert 'is there a verification line under the card, or "will not be verified"?' in text


# -- P10: asking the designer (both renderings and both skills) ---------------------------

def test_both_renderings_carry_the_clarification_rules_in_step_3():
    rules = ("Never re-ask what is known; `model_fact`: query first; pause only what `blocks` lists.",
             "An answer is source answer; a changed request voids what it touched: reconcile again.")
    for text in (server_instructions(), host_instructions()):
        rendered = lines(text)
        step3 = next(i for i, line in enumerate(rendered) if line.startswith("3. list_tools"))
        assert rendered[step3 + 1].endswith("Ask the designer all of them in one round.")
        assert [line.strip() for line in rendered[step3 + 2:step3 + 4]] == list(rules)
        assert rendered[step3 + 4].startswith("4. reconcile(")
        assert len(rendered) <= 60


def test_the_skill_asks_by_kind_and_pauses_only_what_blocks():
    import pathlib

    skill = (pathlib.Path(__file__).resolve().parents[1] / "plugin" / "skills" / "revit-bridge" / "SKILL.md")
    text = skill.read_text(encoding="utf-8")
    questions = text[text.index("## Questions"):text.index("## The TaskSpec")]
    for phrase in ("`kind`, `blocks`", "`user_intent` - only the designer can: ask",
                   "`model_fact` - a value you or the designer gave is not in the model",
                   "`technical` is reserved: the bridge never asks one", "pause only those",
                   "Never ask again what the designer already said or what the spec already holds",
                   "`source: answer` with the question id as `evidence`",
                   "silence, a timeout or your own guess is not an answer",
                   "every value, interpretation and answer the change touches is void",
                   "`reconcile` again", "never offer names you made up"):
        assert phrase in questions, phrase
    assert "Do not run a step an open question `blocks`" in text[text.index("4. **Ask**"):text.index("5. **Reconcile**")]


def test_the_addin_skill_settles_five_things_before_building():
    import pathlib

    skill = (pathlib.Path(__file__).resolve().parents[1] / "plugin" / "skills" / "revit-addin-dev" / "SKILL.md")
    text = skill.read_text(encoding="utf-8")
    start = text.index("Before building, these five must be settled")
    assert text.index("## Continue the user's project") < start < text.index("## Use the bridge where it helps")
    block = text[start:text.index("## Use the bridge where it helps")]
    items = ["1. **Output rules**", "2. **Scope**", "3. **Target Revit version**", "4. **Install location**",
             "5. **Cancel and empty selection**"]
    assert [block.index(item) for item in items] == sorted(block.index(item) for item in items)
    assert "ask about it - all open ones in one round - before you build" in block
    assert "a project that compiles has not settled them" in block
    assert "runtime input" in block


# -- E3: a run that threw (server rendering and skill) -------------------------------------

E3_LINE = "If `hint` names get_guidance: call it before fixing, then check_code and confirm again."


def test_the_server_reads_the_lessons_of_a_run_that_threw_the_host_does_not():
    srv, host = server_instructions(), host_instructions()
    rendered = [line.strip() for line in lines(srv)]
    step8 = next(i for i, line in enumerate(rendered) if line.startswith("8. Read the reply"))
    verify = next(i for i, line in enumerate(rendered) if line.startswith("After execute_code: verify_execution"))
    assert rendered[step8:verify].count(E3_LINE) == 1 and rendered[verify - 1] == E3_LINE    # one line
    assert E3_LINE not in host and "get_guidance" not in host
    assert "revit://connection-status" in srv and len(lines(srv)) <= 60
    # the hint the bridge writes starts the way both texts say
    from revit_bridge.execution import EXCEPTION_HINT
    assert EXCEPTION_HINT.startswith('call get_guidance(stage="fix", exceptions=')


def test_the_skill_says_what_to_do_after_a_run_that_threw():
    import pathlib

    skill = (pathlib.Path(__file__).resolve().parents[1] / "plugin" / "skills" / "revit-bridge" / "SKILL.md")
    text = skill.read_text(encoding="utf-8")
    step8 = text[text.index("8. **Verify and report**"):text.index("## Dry run: try it without changing anything")]
    paragraph = step8[step8.index("**A run that threw.**"):]
    assert len(paragraph.strip().splitlines()) <= 5
    for phrase in ('`call get_guidance(stage="fix", exceptions=[...])`', "Report the failure as it is",
                   "read the lessons before you change anything", "`check_code` until it compiles",
                   "plan the verification again", "get a new confirmation", "the old token is spent",
                   "write `execute_code` C# instead"):
        assert phrase in paragraph, phrase


# -- S5: API lookup (revit-api-docs) and the version the skill needs ------------------------

def test_the_skill_needs_0_4_and_says_how_to_work_with_a_0_3_server():
    import pathlib

    skill = (pathlib.Path(__file__).resolve().parents[1] / "plugin" / "skills" / "revit-bridge" / "SKILL.md")
    text = skill.read_text(encoding="utf-8")
    intro = text[text.index("# revit-bridge"):text.index("## The bridge flow")]
    assert "**0.4 or newer**" in intro and "0.3 or newer" not in intro
    assert "`check_code`, `get_guidance` and `plan_verification` need 0.4" in intro
    for fallback in ('"not pre-compiled locally"', '"Verification: none - will not be verified"',
                     "there is no guidance to read", "**executed, not verified**",
                     "older than 0.3: tell the designer to update with `uvx --refresh revit-bridge` and stop"):
        assert fallback in intro, fallback


def test_the_skill_says_how_to_use_and_read_the_api_lookup():
    import pathlib

    skill = (pathlib.Path(__file__).resolve().parents[1] / "plugin" / "skills" / "revit-bridge" / "SKILL.md")
    text = skill.read_text(encoding="utf-8")
    section = text[text.index("## API lookup"):text.index("## References")]
    assert len(section.strip().splitlines()) <= 8
    for phrase in ("`revit-api-docs` is an optional second MCP server", "`search_revit_api`",
                   "cite the data set version from its first output line", "Without the tool, say the API is unverified",
                   "`Rerank: jev | cohere | none; data set: Revit 2026`", "`Relevance:`",
                   "It is the first line, or the second after an abstention",
                   "not that the member does not exist", "for reference only",
                   "Whether a member exists in the connected Revit is what `check_code` says",
                   "is not the data set's release", "unverified until `check_code` compiles them"):
        assert phrase in section, phrase


# -- P2: the snippet contract has one source -----------------------------------------

def _contract():
    from revit_bridge.guidance import load_contract
    from revit_bridge.paths import guidance_dir

    return load_contract(guidance_dir())


def test_the_snippet_contract_is_rendered_from_its_lesson():
    import revit_bridge.instructions as instructions

    contract = _contract()
    assert 1 <= len(contract.lines) <= 6 and contract.summary
    assert instructions.SNIPPET_CONTRACT_NOTE == contract.note()
    assert instructions.SNIPPET_CONTRACT_SUMMARY == contract.summary
    assert instructions.SNIPPET_CONTRACT_WARNINGS == ()
    assert contract.note().startswith("- Snippet contract (execute_code): ")
    for text in (server_instructions(), host_instructions()):
        notes = text[text.index("## Notes"):]
        assert contract.note() in notes
        assert len(lines(text)) <= 60


def test_no_python_source_restates_the_contract():
    import pathlib

    package = pathlib.Path(revit_bridge.__file__).parent
    contract = _contract()
    phrases = [line.strip() for line in contract.lines] + [contract.summary, "already inside a transaction",
                                                             "IO, Process, Assembly, DllImport", "(no JSON library)"]
    for path in (package / "instructions.py", package / "mcp_server.py", package / "checking.py"):
        text = path.read_text(encoding="utf-8")
        for phrase in phrases:
            assert phrase not in text, (path.name, phrase)


def test_the_cs0246_hint_names_the_usings_of_the_contract():
    """S3c: checking.SNIPPET_USINGS is rendered from the lesson; the hint text is byte for byte as before."""
    from revit_bridge import checking
    from revit_bridge.guidance.contract import SnippetContract

    usings = "System, System.Linq, System.Collections.Generic, Autodesk.Revit.DB, Autodesk.Revit.UI"
    assert _contract().usings() == usings == checking.SNIPPET_USINGS
    hint = checking.hint_for({"id": "CS0246", "message": "The type or namespace name 'Foo' could not be found"},
                             "Revit 2026.5")
    assert hint["text"].startswith(f"Revit 2026.5: type or namespace 'Foo' is not found with the snippet's usings "
                                   f"({usings}) and references (base library, RevitAPI, RevitAPIUI).")
    assert SnippetContract(summary="s", lines=("no list here",), path="p").usings() is None
    wrapped = SnippetContract(summary="s", lines=("a body; usings A,", "  B.C, D; references", "x"), path="p")
    assert wrapped.usings() == "A, B.C, D"


def test_the_tool_descriptions_carry_the_contract_summary():
    import asyncio

    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    sentence = f"Snippet contract: {_contract().summary}."
    for name in ("check_code", "execute_code"):
        assert sentence in tools[name].description, name
        assert "<snippet contract>" not in tools[name].description


def test_no_tool_description_carries_the_source_indentation(monkeypatch):
    """A tool description is its docstring cleaned, on every Python version: 3.13+ strip the source
    indentation from __doc__ when compiling, 3.11 and 3.12 keep it, and the SDK passes __doc__ on as is."""
    import asyncio
    import inspect

    from mcp.server.mcpserver import MCPServer

    for tool in asyncio.run(server.mcp.list_tools()):
        assert tool.description and tool.description == inspect.cleandoc(tool.description), tool.name
    # the registration cleans it, not only the compiler: a docstring as 3.11 keeps it
    def probe() -> str:
        return ""

    probe.__doc__ = "First line,\n    second line,\n      indented once more."
    monkeypatch.setattr(server, "mcp", MCPServer("probe"))
    server._tool()(probe)
    (tool,) = asyncio.run(server.mcp.list_tools())
    assert tool.description == "First line,\nsecond line,\n  indented once more."


def test_the_skill_carries_the_contract_block():
    import pathlib

    from revit_bridge.guidance.contract import BEGIN_MARK, END_MARK

    skill = (pathlib.Path(__file__).resolve().parents[1] / "plugin" / "skills" / "revit-bridge" / "SKILL.md")
    text = skill.read_text(encoding="utf-8")
    block = text[text.index(BEGIN_MARK) + len(BEGIN_MARK):text.index(END_MARK)]
    assert block == "\n" + _contract().note()
    assert text.index("## Notes on the tools") < text.index(BEGIN_MARK)


def test_a_contract_that_does_not_load_falls_back_with_a_warning(tmp_path, monkeypatch):
    import revit_bridge.instructions as instructions
    from revit_bridge.paths import guidance_dir, skills_dir

    broken = tmp_path / "broken"
    (broken / "revit-core" / "lessons").mkdir(parents=True)
    (broken / "revit-core" / "lessons" / "snippet-contract.md").write_text("no front matter\n", encoding="utf-8")
    contract, warnings = instructions.snippet_contract([broken, guidance_dir()])
    assert contract == _contract() and len(warnings) == 1
    assert warnings[0].startswith(f"snippet contract: {broken}: revit-core/lessons/snippet-contract.md: no front")
    contract, warnings = instructions.snippet_contract([skills_dir()])             # the exported copy
    assert contract.note() == _contract().note() and warnings == ()
    contract, warnings = instructions.snippet_contract([broken, tmp_path / "nowhere"])
    assert contract is None and len(warnings) == 2 and "missing" in warnings[1]
    monkeypatch.setattr(instructions, "SNIPPET_CONTRACT_NOTE", instructions.FALLBACK_NOTE)
    for text in (instructions.server_instructions(), instructions.host_instructions()):
        assert "- Snippet contract (execute_code): see the revit-core/snippet-contract lesson." in text
        assert len(lines(text)) <= 60


# -- B1 (hostv2): the default host text is pinned; variants without code, with selections, per role --

# host_instructions() as released in 0.8.2 (origin/main 9cd3b13), byte for byte.
HOST_0_8_2 = """\
You are connected to a running Autodesk Revit through revit-bridge 0.8. The server
runs capability packs and C# in Revit; it never calls a model. You turn the
designer's words into a TaskSpec in which every parameter has a source, get it
confirmed by the designer in this host's interface, and report what the validator found.

## Flow (always, in this order)
1. get_project_snapshot - document, units, levels, grids, types, selection, `fingerprint`.
2. query(kind, args) for any other read: levels, grids, family_types, elements,
   selection, view_elements, units, counts. No token. Never write C# for a read.
3. list_tools, then missing_params(tool, known) - the questions still open, with
   the real options (levels, types). Ask the designer all of them in one round.
   Never re-ask what is known; `model_fact`: query first; pause only what `blocks` lists.
   An answer is source answer; a changed request voids what it touched: reconcile again.
4. reconcile(spec, snapshot) - a draft TaskSpec against the model: values that do
   not exist, ambiguous names, units and range words to confirm, stale snapshot.
   Repeat until `ready` is true.
5. Show the spec card; wait for an explicit yes.
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

## TaskSpec
{task, action: {kind: run_tool|execute_code, tool|code}, parameters: [{name, value,
unit?, source, evidence}], interpretations: [{param?, text, confirmed}],
snapshot_fingerprint, language}. Sources: designer (evidence = their words), tool
(evidence = "tool:<name>"), answer (evidence = question id), preference (evidence =
"preference:<name>"), default (evidence = "default:<tool>", only when the pack
declares one). A number without a unit for a parameter that has one, or a range
word such as "on F2", is an interpretation the designer must confirm.

## Never
- Guess a level, type, element id or coordinate: query, then ask.
- Set a value "for now", "as usual" or "probably": it is a question.
- Claim success on an error or on a failed validation; do not soften errors.
- Say that anything was executed before the host reports an execution result.

## Notes
- Revit internal units are feet; packs take millimetres. Read-only tools also run
  in a transaction on the add-in: they fail on a read-only document.
- Snippet contract (execute_code): a method body with `document` and `parameters`
  (object[]) in scope, already inside a transaction (never open one); usings
  System, System.Linq, System.Collections.Generic, Autodesk.Revit.DB,
  Autodesk.Revit.UI; references only the base library, RevitAPI and RevitAPIUI
  (no JSON library); IO, Process, Assembly, DllImport, File, Registry, reflection
  and threading are refused; end with `return <object>;`; categories are OST_*.
"""
# server_instructions() of 0.8.2: sha256 of its UTF-8 bytes.
SERVER_0_8_2_SHA256 = "0be951bd4eb5f63a994dc4a699db14f6c6012063cdcc423f85c3c5441aad3d6d"
# Since 0.8.2 only the version in the first line has changed (0.9.0).
VERSION_0_8_2, VERSION_NOW = "revit-bridge 0.8.", "revit-bridge 0.9."
HOST_DEFAULT = HOST_0_8_2.replace(VERSION_0_8_2, VERSION_NOW, 1)

CODE_WORDS = ("C#", "execute_code", "Snippet contract", "snippet", "get_guidance", "check_code", "blocked_code",
              "dry_run", "solidify", "`code`", "|code")


def _variants():
    yield {}, host_instructions()
    for code in (True, False):
        for mentions in (False, True):
            yield {"code": code, "mentions": mentions}, host_instructions(code=code, mentions=mentions)
        for role in ("analyst", "planner", "supervisor"):
            yield {"role": role, "code": code}, host_instructions(role=role, code=code)


def test_the_default_host_text_is_the_0_8_2_text_but_for_the_version():
    import hashlib

    assert HOST_0_8_2.count(VERSION_0_8_2) == 1 and HOST_DEFAULT != HOST_0_8_2
    assert host_instructions() == HOST_DEFAULT
    assert host_instructions(None, True, False) == HOST_DEFAULT
    assert host_instructions(role=None, code=True, mentions=False) == HOST_DEFAULT
    assert revit_bridge.host_instructions() == HOST_DEFAULT
    server = server_instructions()
    assert server.count(VERSION_NOW) == 1 and VERSION_0_8_2 not in server
    assert hashlib.sha256(server.replace(VERSION_NOW, VERSION_0_8_2).encode("utf-8")).hexdigest() == SERVER_0_8_2_SHA256


def test_every_variant_keeps_the_budget_and_the_heading_rule():
    for args, text in _variants():
        rendered = lines(text)
        assert text.strip() and len(rendered) <= 60, args
        for i, line in enumerate(rendered):
            if line.startswith("## "):
                assert rendered[i + 1].strip(), (args, line)          # no blank line under a heading
        assert "revit-bridge 0.9." in text and "revit-bridge 0.8" not in text, args


def test_without_code_there_is_no_code_writing_and_no_get_guidance():
    for args, text in _variants():
        if args.get("code") is False:
            for word in CODE_WORDS:
                assert word not in text, (args, word)
    full = host_instructions(code=False)
    assert "{kind: run_tool, tool}" in full
    assert "- Write code: if no tool fits, say that no existing tool can do it." in full
    assert "have no confirm_spec or run_tool; you only propose the spec." in full
    assert "unconfirmed_interpretation.\n   Fix the spec" in full
    assert "Never write C# for a read." in host_instructions(mentions=True)
    assert SNIPPET_NOTE_START in host_instructions(mentions=True)
    assert SNIPPET_NOTE_START in host_instructions(role="planner")
    assert "Never offer to write code." in host_instructions(role="analyst", code=False)
    assert "Never offer to write code." not in host_instructions(role="analyst")


SNIPPET_NOTE_START = "- Snippet contract (execute_code): "


def test_the_variants_share_the_default_steps_verbatim():
    default = host_instructions()
    step3 = default[default.index("3. list_tools"):default.index("4. reconcile(")]
    step4 = default[default.index("4. reconcile("):default.index("5. Show the spec card")]
    for args, text in _variants():
        if args.get("role") in (None, "planner"):
            assert step3 in text, args
            if args.get("code") is False:
                assert "Call ask_designer when you can offer choices." in text, args
            assert step4 in text, args
            extended = LINK_STEP in " ".join(text.split())
            assert extended is (text != HOST_DEFAULT), args          # every variant but the default
            assert '"on F2", is an interpretation the designer must confirm.' in text, args


# Step 2 of every variant: the kinds link_id reads, type_categories included, and the rule that ids
# read through a link are the link's (section 3 of the B1 review).
LINK_STEP = ("links, type_categories. With args.link_id (a picked link's id) levels, family_types, elements, "
             "counts, type_categories read that link; its ids are not the open model's: never pass them to a tool.")

SELECTIONS_RULE = ('Picks in "Designer\'s selections": element value = id as a decimal string; '
                   'level/type/link value = the name. Use evidence "mention:<kind>#<id>", source '
                   'tool if the pack parameter has choices_from or tool:* source, else designer; never re-ask.')


def test_the_selections_rule_only_where_asked():
    assert "Designer's selections" not in host_instructions()
    for text in (host_instructions(mentions=True), host_instructions(code=False, mentions=True),
                 host_instructions(role="planner"), host_instructions(role="planner", code=False)):
        section = text[text.index("## Designer's selections"):text.index("## Never")]
        assert SELECTIONS_RULE in " ".join(section.split())
        assert LINK_STEP in " ".join(text.split())                       # a picked link: args.link_id
    assert "## Designer's selections" not in host_instructions(code=False)
    assert "every value in the selections block, is known" in host_instructions(role="analyst")


def test_the_planner_and_the_link_note_say_link_ids_are_not_the_open_models():
    from revit_bridge.snapshot.query import LINK_NOTE

    for code in (True, False):
        flat = " ".join(host_instructions(role="planner", code=code).split())
        assert "its ids are not the open model's: never pass them to a tool." in flat, code
        assert "levels, family_types, elements, counts, type_categories read that link" in flat, code
    assert "ids read through link_id belong to the linked document" in LINK_NOTE
    assert "must not be passed to tools that act on the open model" in LINK_NOTE


def _wall_spec(level_source: str, level_evidence: str, **start_x):
    from revit_bridge.spec.models import TaskSpec

    params = [{"name": "start_x", "value": 0, "unit": "mm", "source": "designer", "evidence": "from 0,0",
               **start_x},
              {"name": "start_y", "value": 0, "unit": "mm", "source": "designer", "evidence": "from 0,0"},
              {"name": "end_x", "value": 6000, "unit": "mm", "source": "designer", "evidence": "to 6000,0"},
              {"name": "end_y", "value": 0, "unit": "mm", "source": "designer", "evidence": "to 6000,0"},
              {"name": "level_name", "value": "F2", "source": level_source, "evidence": level_evidence}]
    return TaskSpec.model_validate({"task": "a wall on F2", "action": {"kind": "run_tool", "tool": "create_wall"},
                                    "parameters": params, "interpretations": [],
                                    "snapshot_fingerprint": "0" * 16, "language": "en"})


def test_the_selections_rule_is_the_rule_validate_spec_applies():
    """Section 2.4 as amended, pinned against spec/rules.py: a picked level bound to create_wall's
    level_name (choices_from: levels) is source tool with the mention as evidence; source designer
    there is still unsourced_choice; a designer-sourced parameter takes source designer."""
    from revit_bridge.capabilities.store import ToolStore
    from revit_bridge.spec.rules import validate_spec

    pack = ToolStore().load("create_wall")
    level = next(p for p in pack.parameters if p["name"] == "level_name")
    assert level.get("choices_from") == "levels" and str(level.get("source")).startswith("tool:")
    codes = [(e.code, e.param) for e in validate_spec(_wall_spec("tool", "mention:level#312"), pack)]
    assert ("unsourced_choice", "level_name") not in codes and codes == []
    codes = [(e.code, e.param) for e in validate_spec(_wall_spec("designer", "mention:level#312"), pack)]
    assert codes == [("unsourced_choice", "level_name")]
    # a parameter the pack declares designer-sourced: source designer with a mention as evidence
    spec = _wall_spec("tool", "mention:level#312", evidence="mention:level#312")
    assert validate_spec(spec, pack) == []


def test_the_analyst_routes_with_one_json_object_and_no_tools():
    text = host_instructions(role="analyst")
    for route in ("- chat:", "- query:", "- tool:", "- none:"):
        assert route in text, route
    assert ('{"route": "chat" | "query" | "tool" | "none", "tool": "<name>", "facts_needed": ["..."], '
            '"note": "..."}') in text
    assert "Your reply is exactly one JSON object and nothing else, no text before or after it:" in text
    assert "you call no tools" in text
    flat = " ".join(text.split())
    assert ("`note`: with chat and none, the reply the host shows the designer, in their language; "
            "with query and tool, at most two sentences for the next step.") in flat
    assert "`note` tells the designer that no existing tool does it and what is missing." in flat
    for call in ("propose_spec", "missing_params", "reconcile(", "query(", "get_project_snapshot", "list_tools"):
        assert call not in text, call
    assert len(text) < len(host_instructions()) / 2


def test_the_planner_reads_fills_and_proposes_and_reports_errors_as_they_are():
    for code in (True, False):
        text = host_instructions(role="planner", code=code)
        for part in ("get_project_snapshot", "query(kind, args)", "missing_params(tool, known)",
                     "reconcile(spec, snapshot)", "5. propose_spec(spec)",
                     "the analyst's routing {route, tool?, facts_needed, note}", "## TaskSpec", "## Notes",
                     # it answers query-route requests itself and sees query errors
                     "- Claim success on an error or on a failed validation; do not soften errors."):
            assert part in text, (code, part)
        if code:
            assert "6. Confirmation and execution happen in this host's interface" in text
        else:
            assert '6. Ask the designer to click "确认并执行"' in text
        assert [int(m) for m in re.findall(r"^(\d)\. ", text, flags=re.M)] == [1, 2, 3, 4, 5, 6]
        for absent in ("Show the spec card", "comes back to you", "validation.checks"):
            assert absent not in text, (code, absent)


def test_the_supervisor_reports_faithfully_and_answers_with_one_verdict_object():
    text = host_instructions(role="supervisor")
    flat = " ".join(text.split())
    for part in ("`success` is true only when Revit succeeded AND the pack's validator passed",
                 "validation_failed means the model did not change as claimed",
                 "`validation.checks`, `error` and `evidence_id`", "at most twice", "ask the designer",
                 "Claim success on an error or on a failed validation",
                 "query(kind, args) may check a fact in the model; it only reads.",
                 "Your reply is exactly one JSON object and nothing else, no text before or after it:",
                 '{"verdict": "done" | "retry" | "ask", "report": "...", "fix": "...", "question": "..."}',
                 "`report`: what happened, for the designer, in their language, faithful to the result and "
                 "the validator.",
                 "- done: Revit succeeded and the validator passed.",
                 "- retry: it failed and a different parameter value or tool choice can fix it",
                 "`fix` says what the planner should change.",
                 "- ask: anything else, after the second retry, or a validator failure you cannot explain "
                 "(never done). `question` is what to ask the designer",
                 "- `fix` only with retry, `question` only with ask."):
        assert part in flat, part
    for verdict in ('"done"', '"retry"', '"ask"'):
        assert verdict in text, verdict
    for key in ('"verdict"', '"report"', '"fix"', '"question"'):
        assert key in text, key
    for absent in ("propose_spec(", "missing_params", "reconcile(", "## TaskSpec"):
        assert absent not in text, absent
    assert len(text) < len(host_instructions()) / 2


@pytest.mark.parametrize("role", [None, "planner", "analyst", "supervisor"])
def test_web_chat_role_directs_the_designer_to_the_embedded_card(role):
    prompt = host_instructions(role=role, code=False, mentions=True)
    flat = " ".join(prompt.split())
    assert "确认并执行" in prompt
    for obsolete in ("this host's interface", "wait for an explicit yes", "Revit panel", "plugin panel"):
        assert obsolete.casefold() not in flat.casefold()
    assert not re.search(r"\b(?:confirm|click|approve|open)\b[^.!?]{0,80}\bin Revit\b", flat, re.I)
    if role == "supervisor":
        assert "on the card in this chat" in flat and "question as a form" in flat
    else:
        assert '卡片在我上一条提交它的回复里' in flat and "Do not submit it again" in flat
        if role == "analyst":
            assert "route plain assent without changes or a question about its location to chat" in flat
            assert flat.index("Do not submit it again") < flat.index(
                "If the reply asks for a change, route it by what it requests: query, tool, or none.")
        else:
            assert "Only that click authorizes execution" in flat
            assert "ask_designer" in flat and "options grounded in the facts you read" in flat
            assert "Present them on the form and end the turn." in flat


def test_an_unknown_role_is_refused():
    with pytest.raises(ValueError, match="unknown role 'executor'"):
        host_instructions(role="executor")


@pytest.mark.parametrize("role", [None, "analyst", "planner", "supervisor"])
@pytest.mark.parametrize("mentions", [False, True])
def test_custom_button_label_only_changes_no_code_prompt(role, mentions):
    for code in (False, True):
        default = host_instructions(role, code, mentions)
        text = host_instructions(role, code, mentions, confirm_button_label="Run approved task")
        assert text == (default if code else default.replace("确认并执行", "Run approved task"))


def test_load_contract_reports_what_is_wrong(tmp_path):
    from revit_bridge.guidance import GuidanceError, load_contract
    from tests.test_guidance import lesson

    def problems(body: str, **meta) -> list[str]:
        root = tmp_path / str(len(list(tmp_path.iterdir())))
        (root / "revit-core" / "lessons").mkdir(parents=True)
        (root / "revit-core" / "lessons" / "snippet-contract.md").write_text(
            lesson("revit-core/snippet-contract", body=body, **meta), encoding="utf-8")
        try:
            load_contract(root)
        except GuidanceError as e:
            return e.problems
        return []

    ok = "## Contract\n\nLine one\nline two\n\n## Scenario\n\nText.\n"
    assert problems(ok, summary='"s"') == []
    assert problems(ok) == ["revit-core/lessons/snippet-contract.md: summary is required (the tool descriptions use it)"]
    assert problems("## Scenario\n\nText.\n", summary='"s"') == [
        "revit-core/lessons/snippet-contract.md: no ## Contract section"]
    seven = "## Contract\n\n" + "".join(f"line {i}\n" for i in range(7))
    assert problems(seven, summary='"s"') == [
        "revit-core/lessons/snippet-contract.md: the ## Contract section has 7 lines (1 to 6)"]
    assert problems("## Contract\n\n## Scenario\n", summary='"s"') == [
        "revit-core/lessons/snippet-contract.md: the ## Contract section has 0 lines (1 to 6)"]
    with pytest.raises(GuidanceError) as info:
        load_contract(tmp_path / "empty")
    assert info.value.problems == ["revit-core/lessons/snippet-contract.md: missing"]
