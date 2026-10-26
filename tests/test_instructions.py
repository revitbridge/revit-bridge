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
        assert "revit-bridge 0.3." in text and "revit-bridge 0.2" not in text
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
    for path in (package / "instructions.py", package / "mcp_server.py"):
        text = path.read_text(encoding="utf-8")
        for phrase in phrases:
            assert phrase not in text, (path.name, phrase)


def test_the_tool_descriptions_carry_the_contract_summary():
    import asyncio

    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    sentence = f"Snippet contract: {_contract().summary}."
    for name in ("check_code", "execute_code"):
        assert sentence in tools[name].description, name
        assert "<snippet contract>" not in tools[name].description


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
