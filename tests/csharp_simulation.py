"""Compile and run shipped capability templates with small, offline C# test models."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from revit_bridge.capabilities.store import ToolStore


def simulate(tmp_path, fixture_dir, pack_name, scenarios):
    return simulate_packs(tmp_path, fixture_dir, {pack_name: scenarios})


def run_csharp_project(tmp_path, fixture_dir, *, roslyn=False):
    """Build and run a local test harness, using only assemblies shipped with the SDK."""
    dotnet = shutil.which("dotnet")
    env = {**os.environ, "DOTNET_CLI_HOME": str(tmp_path / "dotnet-home"), "DOTNET_NOLOGO": "true",
           "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE": "true"}

    def run(*args):
        proc = subprocess.run([dotnet, *args], cwd=tmp_path, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", timeout=120)
        assert proc.returncode == 0, proc.stdout
        return proc.stdout

    # A runtime-only installation also puts dotnet on PATH.
    major = int(run("--version").strip().split(".")[0]) if dotnet and run("--list-sdks").strip() else 0
    if major < 8:
        if not roslyn and os.environ.get("CI"):
            pytest.fail("The modeling simulation requires a .NET 8 or newer SDK on CI")
        pytest.skip("The C# test harness requires a .NET 8 or newer SDK")
    project = tmp_path / "Simulation.csproj"
    references = ""
    if roslyn:
        references = "<ItemGroup>" + "".join(
            f'<Reference Include="{name}" HintPath="$(MSBuildSDKsPath)/../Roslyn/bincore/{name}.dll" />'
            for name in ("Microsoft.CodeAnalysis", "Microsoft.CodeAnalysis.CSharp")
        ) + "</ItemGroup>"
    project.write_text(f'<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
                       f'<TargetFramework>net{major}.0</TargetFramework></PropertyGroup>{references}</Project>',
                       encoding="utf-8")
    (tmp_path / "NuGet.Config").write_text("<configuration><packageSources><clear/></packageSources></configuration>",
                                           encoding="utf-8")
    for source_file in (Path(__file__).parent / fixture_dir).glob("*.cs"):
        shutil.copyfile(source_file, tmp_path / source_file.name)
    run("build", project.name, "--configfile", "NuGet.Config", "-v:q")
    return run(str(tmp_path / "bin" / "Debug" / f"net{major}.0" / "Simulation.dll"))


def simulate_packs(tmp_path, fixture_dir, packs):
    store = ToolStore(user_dir=tmp_path / "user")
    methods = []
    for pack_name, scenarios in packs.items():
        for name, params in scenarios.items():
            code, errors = store.render(pack_name, params)
            assert not errors and code
            methods.append(f"public static object {name}(Document document) {{\n{code}\n}}")
    source = "using System;\nusing System.Linq;\nusing System.Collections.Generic;\nstatic class Rendered {\n"
    (tmp_path / "Rendered.cs").write_text(source + "\n".join(methods) + "\n}", encoding="utf-8")
    return run_csharp_project(tmp_path, fixture_dir)
