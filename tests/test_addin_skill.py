"""The standalone add-in starter must stay portable and preserve existing work."""
from __future__ import annotations

import json
import re
import runpy
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import UUID

import pytest


SKILL = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "revit-addin-dev"
GENERATOR = SKILL / "scripts" / "new_addin.py"
create_project = runpy.run_path(str(GENERATOR))["create_project"]


@pytest.mark.parametrize("year,framework", [(2024, "net48"), (2025, "net8.0-windows"),
                                            (2026, "net8.0-windows"),
                                            ("2025.5", "net10.0-windows"),
                                            ("2026.5", "net10.0-windows"),
                                            (2027, "net10.0-windows")])
def test_generated_project_has_matching_identity_framework_and_references(tmp_path, year, framework):
    output = tmp_path / "new project"
    result = create_project(output, "WallInspector", year, "MYCO")
    assert result["target_framework"] == framework
    assert "<!--" not in Path(result["project"]).read_text(encoding="utf-8")
    project = ET.parse(result["project"]).getroot()
    manifest = ET.parse(output / "WallInspector.addin").getroot().find("AddIn")
    assert project.findtext("PropertyGroup/TargetFramework") == framework
    assert project.findtext("PropertyGroup/RevitVersion") == str(year).split(".")[0]
    assert project.findtext("PropertyGroup/RevitRelease") == str(year)
    assert manifest.attrib["Type"] == "Command"
    assert manifest.findtext("FullClassName") == "WallInspector.Command"
    assert manifest.findtext("VendorId") == "MYCO"
    assert str(UUID(manifest.findtext("AddInId"))).upper() == result["addin_id"]
    assert manifest.findtext("Assembly") == "WallInspector.dll"
    references = project.findall("ItemGroup/Reference")
    assert {ref.attrib["Include"] for ref in references} == {"RevitAPI", "RevitAPIUI"}
    assert all(ref.findtext("Private") == "false" for ref in references)
    assert not project.findall("ItemGroup/PackageReference")
    assert sorted(p.name for p in output.iterdir()) == result["files"]
    assert result["installed"] is False


COMPARISON = re.compile(r"'([^']*)' *(==|!=|<=|>=|<|>) *'([^']*)'")


def _operand(text):
    parts = text.split(".")
    return tuple(map(int, parts)) if len(parts) > 1 and all(p.isdigit() for p in parts) else text.lower()


def _holds(condition, values):
    # The starter's reference checks are comparisons joined by And/Or without
    # parentheses; MSBuild compares dotted numbers as versions.
    for name, value in values.items():
        condition = condition.replace(name, value)

    def compare(clause):
        left, op, right = COMPARISON.fullmatch(clause.strip()).groups()
        a, b = _operand(left), _operand(right)
        return {"==": a == b, "!=": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]
    return any(all(compare(c) for c in term.split(" And ")) for term in condition.split(" Or "))


def _rejections(project_file, framework, reference_version):
    target = ET.parse(project_file).getroot().find("Target[@Name='CheckRevitReferences']")
    values = {"$(TargetFramework)": framework, "%(RevitReferenceIdentity.Version)": reference_version}
    return [error.get("Text") for error in target.findall("Error")
            if "Exists(" not in error.get("Condition") and _holds(error.get("Condition"), values)]


# Reference versions are AssemblyVersion values seen locally: Revit 2027 RevitAPI.dll
# 27.0.10.0, Revit 2026.5 26.5.0.0, Nice3point 2027.2.0 / 2026.0.4 / 2025.4.50 / 2024.3.60.
@pytest.mark.parametrize("release,framework,reference,rejected", [
    ("2027", "net10.0-windows", "27.0.10.0", False),
    ("2027", "net10.0-windows", "27.2.0.0", False),
    ("2027", "net8.0-windows", "27.0.10.0", True),
    ("2027", "net48", "27.0.10.0", True),
    ("2027", "net10.0-windows", "26.5.0.0", True),
    ("2027", "net10.0-windows", "28.0.0.0", True),
    ("2026", "net8.0-windows", "26.0.4.0", False),
    ("2026", "net8.0-windows", "26.5.0.0", True),
    ("2026.5", "net10.0-windows", "26.5.0.0", False),
    ("2026.5", "net10.0-windows", "26.0.4.0", True),
    ("2026.5", "net10.0-windows", "27.0.10.0", True),
    ("2025", "net8.0-windows", "25.4.50.0", False),
    ("2025.5", "net10.0-windows", "25.4.50.0", True),
    ("2024", "net48", "24.3.60.0", False),
])
def test_reference_checks_reject_mismatched_year_and_runtime(tmp_path, release, framework, reference, rejected):
    result = create_project(tmp_path / "new", "WallInspector", release, "MYCO")
    assert bool(_rejections(result["project"], framework, reference)) is rejected


def test_revit_2027_project_is_dotnet10_only(tmp_path):
    result = create_project(tmp_path / "new", "WallInspector", "2027", "MYCO")
    text = Path(result["project"]).read_text(encoding="utf-8")
    assert "2027.5" not in text and "net8.0-windows" not in text
    assert _rejections(result["project"], "net8.0-windows", "27.0.10.0") == [
        "Revit 2027 runs on .NET 10 and has no .NET 8 build. "
        "Target net10.0-windows and build with a .NET 10 SDK."]
    split = Path(create_project(tmp_path / "split", "WallInspector", "2026", "MYCO")["project"])
    assert "has no .NET 8 build" not in split.read_text(encoding="utf-8")


NET8_GLOBAL_JSON = '{\n  "sdk": {\n    "version": "8.0.100",\n    "rollForward": "latestMajor"\n  }\n}\n'
# Exactly the global.json of Autodesk's Revit 2027 sample.
NET10_GLOBAL_JSON = '{\n  "sdk": {\n    "version": "10.0.100",\n    "rollForward": "minor"\n  }\n}\n'


@pytest.mark.parametrize("release,expected", [("2025", NET8_GLOBAL_JSON), ("2025.5", NET10_GLOBAL_JSON),
                                              ("2026", NET8_GLOBAL_JSON), ("2026.5", NET10_GLOBAL_JSON),
                                              ("2027", NET10_GLOBAL_JSON)])
def test_global_json_selects_an_sdk_for_the_target(tmp_path, release, expected):
    result = create_project(tmp_path / "new", "WallInspector", release, "MYCO")
    assert (tmp_path / "new" / "global.json").read_text(encoding="utf-8") == expected
    assert result["sdk"] == json.loads(expected)["sdk"]["version"]
    assert "global.json" in result["files"]


def test_revit_2024_project_has_no_global_json(tmp_path):
    result = create_project(tmp_path / "new", "WallInspector", 2024, "MYCO")
    assert not (tmp_path / "new" / "global.json").exists()
    assert result["sdk"] is None
    assert "global.json" not in result["files"]


def test_generated_addin_id_is_unique_across_projects(tmp_path):
    first = create_project(tmp_path / "one", "WallInspector", 2026, "MYCO")
    second = create_project(tmp_path / "two", "WallInspector", 2026, "MYCO")
    assert first["addin_id"] != second["addin_id"]


@pytest.mark.parametrize("populated", [False, True])
def test_existing_directory_is_never_modified(tmp_path, populated):
    output = tmp_path / "existing"
    output.mkdir()
    if populated:
        (output / "WallInspector.addin").write_bytes(b"existing identity")
        (output / "Command.cs").write_bytes(b"user work")
        (output / "global.json").write_bytes(b'{"sdk": {"version": "9.0.100"}}')
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    with pytest.raises(FileExistsError):
        create_project(output, "WallInspector", 2026, "MYCO")
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before


@pytest.mark.parametrize("name,year,vendor", [
    ("../Sibling", 2026, "MYCO"),
    ("Wall-Inspector", 2026, "MYCO"),
    ("class", 2026, "MYCO"),
    ("CON", 2026, "MYCO"),
    ("WallInspector", 2028, "MYCO"),
    ("WallInspector", "2027.5", "MYCO"),
    ("WallInspector", 2023, "MYCO"),
    ("WallInspector", 2026, "<injected/>"),
    ("WallInspector", 2026, ""),
])
def test_invalid_input_writes_nothing(tmp_path, name, year, vendor):
    output = tmp_path / "new"
    with pytest.raises(ValueError):
        create_project(output, name, year, vendor)
    assert not output.exists()


@pytest.mark.parametrize("release", ["2026", "2026.5", "2027"])
def test_copied_skill_cli_works_from_an_unrelated_working_directory(tmp_path, release):
    installed = tmp_path / "installed skill"
    shutil.copytree(SKILL, installed, ignore=shutil.ignore_patterns("__pycache__"))
    output = tmp_path / "user project"
    command = [sys.executable, str(installed / "scripts" / "new_addin.py"),
               "--name", "WallInspector", "--revit-version", release, "--vendor-id", "MYCO",
               "--output", str(output)]
    run = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=True)
    result = json.loads(run.stdout)
    assert Path(result["project"]).is_file()
    assert result["revit_version"] == int(release.split(".")[0])
    assert result["revit_release"] == release
    assert sorted(p.name for p in output.iterdir()) == result["files"]
    assert (output / "global.json").is_file()
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    again = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert again.returncode == 2
    assert again.stdout == ""
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before


def test_cli_rejects_an_unknown_update_without_guessing(tmp_path):
    output = tmp_path / "new"
    run = subprocess.run([sys.executable, str(GENERATOR), "--name", "WallInspector",
                          "--revit-version", "2027.5", "--vendor-id", "MYCO", "--output", str(output)],
                         capture_output=True, text=True)
    assert run.returncode == 2
    assert "invalid choice: '2027.5'" in run.stderr
    assert run.stdout == ""
    assert not output.exists()
