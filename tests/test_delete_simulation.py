"""Run the actual rendered delete_elements C# against a document with independently
specified ownership, dependency queries and deletion cascades. No Revit installation."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from revit_bridge.capabilities.store import ToolStore


def test_classification_precedes_the_only_deletion_and_postchecks_precede_success(tmp_path):
    code, errors = ToolStore(user_dir=tmp_path / "user").render("delete_elements", {"element_ids": "10"})
    assert errors == [] and code.count("document.Delete(") == 1
    deletion = code.index("document.Delete(")
    for read in ("GetSubComponentIds(", "GetPanelIds(", "GetMullionIds(", "GetStackedWallMemberIds(",
                 "GetMemberIds(", "GetDependentElements(", "dependent.Category"):
        assert code.index(read) < deletion
    for check in ("modelDependents.Contains(id)", "!allowedExtras.Contains(id)", "missing.Count > 0", "survivors.Count > 0"):
        assert deletion < code.index(check) < code.index('Status = "Deleted"')
    assert "FilteredElementCollector" not in code and "Settings.Categories" not in code


def test_rendered_delete_and_transaction_rollback(tmp_path):
    dotnet = shutil.which("dotnet")
    if not dotnet:
        if os.environ.get("CI"):
            pytest.fail("The delete_elements simulation requires a .NET 8 or newer SDK on CI")
        pytest.skip("Install a .NET 8 or newer SDK to run the delete_elements simulation")
    env = {**os.environ, "DOTNET_CLI_HOME": str(tmp_path / "dotnet-home"), "DOTNET_NOLOGO": "true",
           "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE": "true"}

    def run(*args):
        proc = subprocess.run([dotnet, *args], cwd=tmp_path, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", timeout=120)
        assert proc.returncode == 0, proc.stdout
        return proc.stdout

    major = int(run("--version").strip().split(".")[0])
    if major < 8:
        if os.environ.get("CI"):
            pytest.fail("The delete_elements simulation requires a .NET 8 or newer SDK on CI")
        pytest.skip("The delete_elements simulation requires a .NET 8 or newer SDK")
    project = tmp_path / "DeleteSimulation.csproj"
    project.write_text(f'<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
                       f'<TargetFramework>net{major}.0</TargetFramework></PropertyGroup></Project>', encoding="utf-8")
    (tmp_path / "NuGet.Config").write_text("<configuration><packageSources><clear/></packageSources></configuration>",
                                           encoding="utf-8")
    fixtures = Path(__file__).parent / "delete_simulation"
    for name in ("RevitModel.cs", "Scenarios.cs"):
        shutil.copyfile(fixtures / name, tmp_path / name)

    store = ToolStore(user_dir=tmp_path / "user")
    methods = []
    for name, ids in (("One", "10"), ("Both", "10,11"), ("Empty", "")):
        code, errors = store.render("delete_elements", {"element_ids": ids})
        assert not errors and code
        methods.append(f"public static object {name}(Document document) {{\n{code}\n}}")
    source = "using System;\nusing System.Linq;\nusing System.Collections.Generic;\nstatic class Rendered {\n"
    (tmp_path / "Rendered.cs").write_text(source + "\n".join(methods) + "\n}", encoding="utf-8")
    run("build", project.name, "--configfile", "NuGet.Config", "-v:q")
    output = run(str(tmp_path / "bin" / "Debug" / f"net{major}.0" / "DeleteSimulation.dll"))
    assert len([line for line in output.splitlines() if line.startswith("PASS ")]) == 28, output
