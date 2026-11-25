"""Compile and run shipped capability templates with small, offline C# test models."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from revit_bridge.capabilities.store import ToolStore


def simulate(tmp_path, fixture_dir, pack_name, scenarios):
    return simulate_packs(tmp_path, fixture_dir, {pack_name: scenarios})


def simulate_packs(tmp_path, fixture_dir, packs):
    dotnet = shutil.which("dotnet")
    if not dotnet:
        if os.environ.get("CI"):
            pytest.fail("The modeling simulation requires a .NET 8 or newer SDK on CI")
        pytest.skip("Install a .NET 8 or newer SDK to run the modeling simulation")
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
            pytest.fail("The modeling simulation requires a .NET 8 or newer SDK on CI")
        pytest.skip("The modeling simulation requires a .NET 8 or newer SDK")
    project = tmp_path / "Simulation.csproj"
    project.write_text(f'<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
                       f'<TargetFramework>net{major}.0</TargetFramework></PropertyGroup></Project>', encoding="utf-8")
    (tmp_path / "NuGet.Config").write_text("<configuration><packageSources><clear/></packageSources></configuration>",
                                           encoding="utf-8")
    for source_file in (Path(__file__).parent / fixture_dir).glob("*.cs"):
        shutil.copyfile(source_file, tmp_path / source_file.name)

    store = ToolStore(user_dir=tmp_path / "user")
    methods = []
    for pack_name, scenarios in packs.items():
        for name, params in scenarios.items():
            code, errors = store.render(pack_name, params)
            assert not errors and code
            methods.append(f"public static object {name}(Document document) {{\n{code}\n}}")
    source = "using System;\nusing System.Linq;\nusing System.Collections.Generic;\nstatic class Rendered {\n"
    (tmp_path / "Rendered.cs").write_text(source + "\n".join(methods) + "\n}", encoding="utf-8")
    run("build", project.name, "--configfile", "NuGet.Config", "-v:q")
    return run(str(tmp_path / "bin" / "Debug" / f"net{major}.0" / "Simulation.dll"))
