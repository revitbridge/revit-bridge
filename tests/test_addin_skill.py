"""The standalone add-in starter must stay portable and preserve existing work."""
from __future__ import annotations

import json
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
                                            ("2026.5", "net10.0-windows")])
def test_generated_project_has_matching_identity_framework_and_references(tmp_path, year, framework):
    output = tmp_path / "new project"
    result = create_project(output, "WallInspector", year, "MYCO")
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
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    with pytest.raises(FileExistsError):
        create_project(output, "WallInspector", 2026, "MYCO")
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before


@pytest.mark.parametrize("name,year,vendor", [
    ("../Sibling", 2026, "MYCO"),
    ("Wall-Inspector", 2026, "MYCO"),
    ("class", 2026, "MYCO"),
    ("CON", 2026, "MYCO"),
    ("WallInspector", 2027, "MYCO"),
    ("WallInspector", 2026, "<injected/>"),
    ("WallInspector", 2026, ""),
])
def test_invalid_input_writes_nothing(tmp_path, name, year, vendor):
    output = tmp_path / "new"
    with pytest.raises(ValueError):
        create_project(output, name, year, vendor)
    assert not output.exists()


@pytest.mark.parametrize("release", ["2026", "2026.5"])
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
    assert result["revit_version"] == 2026
    assert result["revit_release"] == release
    assert len(list(output.iterdir())) == 4
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    again = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert again.returncode == 2
    assert again.stdout == ""
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before
