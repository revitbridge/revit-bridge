"""`revit-bridge guidance check|index|explain`: exit codes and output, on temporary roots."""
from __future__ import annotations

import io
import shutil
import sys
from pathlib import Path

import pytest

import revit_bridge.mcp_server as server
from revit_bridge.guidance import index as guidance_index
from revit_bridge.guidance import indexer, load
from tests.test_guidance import CONTENT, lesson, make_root

WALL = "revit-geometry/wall-length-from-location"

SKILL = "---\nname: {name}\ndescription: d\n---\n\n# {name}\n\nOverview.\n\n## Lessons\n\n{lines}\n## Notes\n\nKeep me.\n"


def run(capsys, *argv: str) -> tuple[int, str]:
    code = server.main(["guidance", *argv])
    return code, capsys.readouterr().out


def copy_seed(tmp_path: Path) -> Path:
    root = tmp_path / "plugin" / "guidance"
    shutil.copytree(CONTENT, root)
    return root


# -- check ---------------------------------------------------------------------------

def test_check_the_seed_content(capsys):
    code, out = run(capsys, "check", str(CONTENT))
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == f"layer framework: loaded, 17 lessons: {CONTENT}"
    assert lines[1:4] == ["layer company: not set", "layer project: not set", "layer personal: not set"]
    assert lines[-1] == "result: 0 problems, 0 warnings, 17 lessons"                 # S3b: every seed <= 2300 bytes
    assert not any(line.startswith("problem:") for line in lines)


def test_check_reads_the_configured_layers_by_default(tmp_path, monkeypatch, capsys):
    company = tmp_path / "company"
    (company / "revit-core" / "lessons").mkdir(parents=True)
    (company / "revit-core" / "lessons" / "a.md").write_text(lesson("revit-core/b"), encoding="utf-8")
    (company / "revit-core" / "lessons" / "c.md").write_text(lesson("revit-core/c", revit='"soon"'), encoding="utf-8")
    monkeypatch.setenv("REVIT_BRIDGE_GUIDANCE_COMPANY", str(company))
    code, out = run(capsys, "check")
    assert code == 1
    assert f"layer company: invalid (2 problems): {company}" in out
    assert "problem: company: revit-core/lessons/a.md: id 'revit-core/b' does not match its path" in out
    assert "problem: company: revit-core/lessons/c.md: revit: bad version range" in out
    assert "warning: layer_invalid: company: revit-core/lessons/a.md: id 'revit-core/b'" in out
    assert "layer personal: missing, skipped: " in out and "layer project: not set" in out
    assert out.splitlines()[-1].startswith("result: 2 problems, ")


def test_check_given_directories_stacks_them(tmp_path, capsys):
    fw = make_root(tmp_path / "fw", {"revit-core/lessons/a.md": lesson("revit-core/a", enforced="true")})
    company = tmp_path / "c"
    (company / "revit-core" / "lessons").mkdir(parents=True)
    (company / "revit-core" / "lessons" / "a.md").write_text(lesson("revit-core/a", enforced="true"), encoding="utf-8")
    project = tmp_path / "p"
    (project / "revit-core" / "lessons").mkdir(parents=True)
    (project / "revit-core" / "lessons" / "a.md").write_text(lesson("revit-core/a"), encoding="utf-8")
    body = "x" * 2400 + "\n"
    (project / "revit-core" / "lessons" / "big.md").write_text(lesson("revit-core/big", body=body), encoding="utf-8")
    code, out = run(capsys, "check", str(fw), str(company), str(project))
    assert code == 0                                                   # warnings only
    assert "warning: enforced_ignored: revit-core/a: enforced counts only in the company layer" in out
    assert "warning: enforced_kept: revit-core/a ignored project" in out
    assert "warning: project: revit-core/lessons/big.md: body is 2401 UTF-8 bytes, over the 2300-byte" in out
    assert out.splitlines()[-1] == "result: 0 problems, 3 warnings, 2 lessons"

    code, out = run(capsys, "check", str(fw), str(tmp_path / "nowhere"))
    assert code == 1 and f"problem: company: {tmp_path / 'nowhere'}: not a directory" in out
    code, out = run(capsys, "check", str(fw), str(fw), str(fw), str(fw), str(fw))
    assert code == 2 and "at most 4 directories" in out


def test_check_an_invalid_framework(tmp_path, capsys):
    bad = make_root(tmp_path, {"revit-core/lessons/a.md": lesson("revit-core/b")})
    code, out = run(capsys, "check", str(bad))
    assert code == 1 and out.startswith(f"layer framework: invalid (1 problems): {bad}")
    assert "problem: framework: revit-core/lessons/a.md: id 'revit-core/b' does not match its path" in out


# -- index ---------------------------------------------------------------------------

def test_index_check_on_the_seed_content_is_clean(capsys):
    code, out = run(capsys, "index", str(CONTENT), "--check")
    assert code == 0 and out.strip() == f"result: up to date: {CONTENT}"
    assert indexer.plan_index(CONTENT) == ([], [])


def test_index_rebuilds_only_the_lessons_section(tmp_path, capsys):
    stale = SKILL.format(name="revit-core", lines="- [a](lessons/a.md) - stale line\n- [gone](lessons/gone.md) - x\n")
    root = make_root(tmp_path, {
        "revit-core/lessons/b.md": lesson("revit-core/b", order="1"),
        "revit-core/lessons/a.md": lesson("revit-core/a", order="2", summary='"the a lesson"'),
    }, skills={"revit-core": stale, "revit-views": SKILL.format(name="revit-views", lines="- [x](lessons/x.md) - y\n"),
               "revit-mep": "---\nname: revit-mep\ndescription: d\n---\n\n# revit-mep\n\nNo index yet.\n"})
    (root / "revit-params").mkdir()                                     # no SKILL.md: left alone
    before = (root / "revit-core" / "SKILL.md").read_bytes()

    code, out = run(capsys, "index", str(root), "--check")
    assert code == 1 and (root / "revit-core" / "SKILL.md").read_bytes() == before
    assert out.splitlines() == [
        f"out of date: {root / 'revit-core' / 'SKILL.md'} (lessons index)",
        f"out of date: {root / 'revit-mep' / 'SKILL.md'} (lessons index)",
        f"out of date: {root / 'revit-views' / 'SKILL.md'} (lessons index)",
        "result: 3 out of date"]

    code, out = run(capsys, "index", str(root))
    assert code == 0 and out.splitlines()[-1] == "result: 3 written"
    core = (root / "revit-core" / "SKILL.md").read_text(encoding="utf-8")
    assert core == SKILL.format(name="revit-core", lines=(
        "- [b](lessons/b.md) - Lesson revit-core/b\n- [a](lessons/a.md) - the a lesson\n"))
    assert (root / "revit-views" / "SKILL.md").read_text(encoding="utf-8") == SKILL.format(
        name="revit-views", lines="None yet.\n")
    assert (root / "revit-mep" / "SKILL.md").read_text(encoding="utf-8").endswith(
        "No index yet.\n\n## Lessons\n\nNone yet.\n")
    assert not (root / "revit-params" / "SKILL.md").exists()
    assert [x["summary"] for x in guidance_index(load(root))["items"]] == ["Lesson revit-core/b", "the a lesson"]

    code, out = run(capsys, "index", str(root), "--check")
    assert code == 0 and out.strip() == f"result: up to date: {root}"
    code, out = run(capsys, "index", str(root))
    assert code == 0 and out.strip() == f"result: up to date: {root}"


def test_index_keeps_a_file_s_line_endings(tmp_path, capsys):
    root = make_root(tmp_path, {"revit-core/lessons/a.md": lesson("revit-core/a")})
    skill = root / "revit-core" / "SKILL.md"
    crlf = SKILL.format(name="revit-core", lines="- [a](lessons/a.md) - Lesson revit-core/a\n")
    skill.write_bytes(crlf.replace("\n", "\r\n").encode("utf-8"))
    assert run(capsys, "index", str(root), "--check")[0] == 0              # line ends are not a difference
    skill.write_bytes(crlf.replace("Lesson revit-core/a", "old").replace("\n", "\r\n").encode("utf-8"))
    assert run(capsys, "index", str(root))[0] == 0
    assert skill.read_bytes() == crlf.replace("\n", "\r\n").encode("utf-8")


def test_index_writes_nothing_when_a_lesson_has_a_problem(tmp_path, capsys):
    root = make_root(tmp_path, {"revit-core/lessons/a.md": lesson("revit-core/a", revit='"soon"')},
                     skills={"revit-core": SKILL.format(name="revit-core", lines="None yet.\n")})
    before = (root / "revit-core" / "SKILL.md").read_bytes()
    code, out = run(capsys, "index", str(root))
    assert code == 1 and "problem: revit-core/lessons/a.md: revit: bad version range" in out
    assert out.splitlines()[-1] == "result: 1 problems; nothing written"
    assert (root / "revit-core" / "SKILL.md").read_bytes() == before
    code, out = run(capsys, "index", str(tmp_path / "nowhere"))
    assert code == 1 and "not a directory" in out


def test_index_on_a_copy_of_the_seed_regenerates_the_same_bytes(tmp_path, capsys):
    root = copy_seed(tmp_path)
    for path in root.glob("*/SKILL.md"):
        text = path.read_text(encoding="utf-8")
        if "## Lessons" in text:
            path.write_text(text.split("## Lessons")[0] + "## Lessons\n\nstale\n", encoding="utf-8", newline="\n")
    assert run(capsys, "index", str(root))[0] == 0
    for path in root.glob("*/SKILL.md"):
        original = (CONTENT / path.parent.name / "SKILL.md").read_bytes()
        if b"## Writing lessons" in original:                            # the core note after the index
            continue
        assert path.read_bytes() == original, path


def copy_plugin(tmp_path: Path) -> Path:
    """plugin/guidance and the two generated-into skills, in a temporary plugin layout."""
    plugin = tmp_path / "plugin"
    shutil.copytree(CONTENT, plugin / "guidance")
    for name in ("revit-core", "revit-bridge"):
        shutil.copytree(CONTENT.parent / "skills" / name, plugin / "skills" / name)
    return plugin


def test_index_exports_revit_core_and_writes_the_contract_block(tmp_path, capsys):
    plugin = copy_plugin(tmp_path)
    root, skills = plugin / "guidance", plugin / "skills"
    assert run(capsys, "index", str(root), "--check") == (0, f"result: up to date: {root}\n")

    units = root / "revit-core" / "lessons" / "internal-units-feet.md"
    units.write_bytes(units.read_bytes().replace(b"summary: \"lengths are", b"summary: \"Lengths are"))
    contract = root / "revit-core" / "lessons" / "snippet-contract.md"
    contract.write_bytes(contract.read_bytes().replace(b"categories are OST_*.", b"categories are OST_* names."))
    stray = skills / "revit-core" / "lessons" / "retired.md"
    stray.write_text("old\n", encoding="utf-8")
    (skills / "revit-core" / "SKILL.md").write_bytes(
        (skills / "revit-core" / "SKILL.md").read_bytes().replace(b"\n", b"\r\n"))      # CRLF is a difference here

    code, out = run(capsys, "index", str(root), "--check")
    assert code == 1
    assert out.splitlines() == [
        f"out of date: {root / 'revit-core' / 'SKILL.md'} (lessons index)",
        f"out of date: {skills / 'revit-core' / 'SKILL.md'} (revit-core export)",
        f"out of date: {skills / 'revit-core' / 'lessons' / 'internal-units-feet.md'} (revit-core export)",
        f"out of date: {skills / 'revit-core' / 'lessons' / 'snippet-contract.md'} (revit-core export)",
        f"to be removed: {stray} (revit-core export)",
        f"out of date: {skills / 'revit-bridge' / 'SKILL.md'} (snippet contract)",
        "result: 6 out of date"]

    code, out = run(capsys, "index", str(root))
    assert code == 0 and f"removed: {stray} (revit-core export)" in out and not stray.exists()
    assert run(capsys, "index", str(root), "--check")[0] == 0
    source = {p.relative_to(root / "revit-core"): p.read_bytes() for p in (root / "revit-core").rglob("*") if p.is_file()}
    export = {p.relative_to(skills / "revit-core"): p.read_bytes() for p in (skills / "revit-core").rglob("*")
              if p.is_file()}
    assert export == source
    assert b"- [internal-units-feet](lessons/internal-units-feet.md) - Lengths are" in source[Path("SKILL.md")]
    bridge = (skills / "revit-bridge" / "SKILL.md").read_text(encoding="utf-8")
    assert "and threading are refused; end with `return <object>;`; categories are OST_* names.\n" in bridge
    assert bridge.count("<!-- snippet-contract:begin -->") == 1


def test_index_needs_the_contract_markers(tmp_path, capsys):
    plugin = copy_plugin(tmp_path)
    skill = plugin / "skills" / "revit-bridge" / "SKILL.md"
    skill.write_bytes(skill.read_bytes().replace(b"<!-- snippet-contract:end -->", b""))
    before = skill.read_bytes()
    code, out = run(capsys, "index", str(plugin / "guidance"))
    assert code == 1 and "markers are missing" in out and skill.read_bytes() == before
    contract = plugin / "guidance" / "revit-core" / "lessons" / "snippet-contract.md"
    contract.write_bytes(contract.read_bytes().replace(b"## Contract", b"## Contracts"))
    code, out = run(capsys, "index", str(plugin / "guidance"), "--check")
    assert code == 1 and "problem: revit-core/lessons/snippet-contract.md: no ## Contract section" in out


# -- explain -------------------------------------------------------------------------

def test_explain_acceptance_fix_wall(capsys):
    code, out = run(capsys, "explain", "--stage", "fix", "--errors", "CS1061", "--symbols", "Wall", "LocationCurve")
    assert code == 0
    lines = out.splitlines()
    first = next(line for line in lines if line.startswith("1. "))
    assert first.startswith(f"1. {WALL} (framework, revision ")
    assert "   why: stage:fix, symbol:Wall, symbol:LocationCurve, error:CS1061+symbol:Wall,LocationCurve" in lines
    assert lines[0] == "stage: fix; revit: not given; inline: no"
    assert "## " not in out and "```" not in out                            # never a body


def test_explain_with_code_pairs_errors_only_with_symbols(tmp_path, capsys):
    code_file = tmp_path / "snippet.cs"
    code_file.write_text("var w = document.GetElement(id) as Wall; // Floor\n"
                         "var c = w.LocationCurve; var u = UnitUtils.ConvertFromInternalUnits(1, UnitTypeId.Meters);\n",
                         encoding="utf-8")
    code, out = run(capsys, "explain", "--stage", "fix", "--errors", "CS1061", "--code", str(code_file))
    assert code == 0
    assert "symbols: -; from the code: GetElement, Wall, LocationCurve, UnitUtils, ConvertFromInternalUnits, " \
           "UnitTypeId, Meters" in out
    assert "error:" not in out                                          # no --symbols: no error pairing
    assert f"1. revit-core/internal-units-feet" in out and f"2. {WALL}" in out
    code, out = run(capsys, "explain", "--stage", "fix", "--errors", "CS1061", "--code", str(code_file),
                    "--symbols", "LocationCurve")
    assert f"1. {WALL}" in out and "error:CS1061+symbol:LocationCurve" in out
    assert "from the code: GetElement, Wall, UnitUtils" in out


def test_explain_inline_version_and_omitted(capsys):
    code, out = run(capsys, "explain", "--stage", "orient", "--revit", "2023", "--inline")
    assert code == 0 and "budget: 2 items, 12000 bytes" in out
    assert "1. revit-core/snippet-contract" in out and "2. revit-core/transaction-owned-by-bridge" in out
    assert "omitted: revit-core/internal-units-feet (version)" in out
    code, out = run(capsys, "explain", "--stage", "write", "--categories", "OST_Roofs")
    assert code == 0 and "no lessons" in out
    code, out = run(capsys, "explain", "--stage", "fix", "--code", "no-such-file.cs")
    assert code == 2 and out.startswith("problem: --code no-such-file.cs")


def test_explain_rejects_an_unknown_stage(capsys):
    with pytest.raises(SystemExit) as info:
        server.main(["guidance", "explain", "--stage", "deploy"])
    assert info.value.code == 2
    with pytest.raises(SystemExit) as info:
        server.main(["guidance"])
    assert info.value.code == 2


def test_output_is_safe_on_a_gbk_console(tmp_path, monkeypatch):
    odd = tmp_path / ("layer-" + chr(0x1F600))
    odd.mkdir()
    (odd / "revit-core").mkdir()
    monkeypatch.setenv("REVIT_BRIDGE_GUIDANCE_PROJECT", str(odd))
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="gbk", errors="strict", newline="\n")
    monkeypatch.setattr(sys, "stdout", console)
    assert server.main(["guidance", "check"]) == 0
    console.flush()
    text = raw.getvalue().decode("gbk")
    assert "layer project: loaded, 0 lessons: " in text and "\\U0001f600" in text
