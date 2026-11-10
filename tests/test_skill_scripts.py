"""The shipped helper scripts of the add-in test and release skills, run as a user runs them.

The .ps1 scripts run under Windows PowerShell 5.1 (skipped where there is none) with the
module path set explicitly for each case (``module_path_env``), never the one of the shell
that started pytest. The Python scripts run in an isolated child (``-I``, UTF-8 mode off),
so neither PYTHON* variables nor the code page of the machine change the result.
"""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[1] / "plugin" / "skills"
COLLECT_LOGS = SKILLS / "revit-addin-release" / "scripts" / "collect_logs.ps1"
JOURNAL_TAIL = SKILLS / "revit-addin-test" / "scripts" / "journal_tail.py"


REVIT_PS = SKILLS / "revit-addin-test" / "scripts" / "revit_ps.ps1"
# A child Python that ignores PYTHON* variables (PYTHONIOENCODING, PYTHONUTF8, PYTHONPATH) and
# the user site, with UTF-8 mode off as on a default Chinese Windows.
PYTHON_CHILD = [sys.executable, "-I", "-X", "utf8=0"]


def child_env(tmp_path: Path) -> dict:
    """The child's environment: the scripts read LOCALAPPDATA (Revit's journal folder)."""
    return {**os.environ, "LOCALAPPDATA": str(tmp_path / "local")}


POWERSHELL = shutil.which("powershell") if sys.platform == "win32" else None
needs_windows_powershell = pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell 5.1 is not available")

# Built-in modules of Windows PowerShell whose commands the scripts use and which it loads on
# demand from PSModulePath, with those commands.
ON_DEMAND = (("Microsoft.PowerShell.Utility", "CmdletsToExport", "Get-FileHash"),
             ("Microsoft.PowerShell.Archive", "FunctionsToExport", "Compress-Archive"),
             ("CimCmdlets", "CmdletsToExport", "Get-CimInstance"))
MODULE_PATHS = ["own", "foreign", "pwsh7"]


def _foreign_modules(directory: Path) -> Path:
    """Copies of the on-demand modules that Windows PowerShell cannot load but that declare the
    commands, as PowerShell 7's own copies do (Core only, a .NET assembly it cannot load)."""
    for name, key, command in ON_DEMAND:
        (directory / name).mkdir(parents=True)
        (directory / name / "Core.dll").write_bytes(b"not an assembly Windows PowerShell can load")
        (directory / name / f"{name}.psd1").write_text(
            "@{ ModuleVersion = '7.0.0.0'; CompatiblePSEditions = @('Core'); PowerShellVersion = '3.0'\n"
            f"   NestedModules = @('Core.dll'); {key} = @('{command}') }}\n", encoding="ascii")
    return directory


@functools.cache
def _pwsh7_modules() -> Path | None:
    """The module directory of the PowerShell 7 on PATH, as that pwsh reports its $PSHOME (an MSI,
    Store or dotnet-tool install alike)."""
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        return None
    run = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-Command", "[Console]::Out.Write($PSHOME)"],
                         capture_output=True, timeout=120)
    modules = Path(run.stdout.decode("utf-8", "replace").strip()) / "Modules"
    return modules if (modules / "Microsoft.PowerShell.Utility").is_dir() else None


def module_path_env(case: str, root: Path, env: dict | None = None) -> dict:
    """``env`` (default: this process's) with the PSModulePath a Windows PowerShell child starts with.

    own     - none: Windows PowerShell builds its own, as when started from Explorer or cmd;
    foreign - copies of its on-demand modules that it cannot load come first (made here, so the
              failure reproduces on any Windows, with or without PowerShell 7);
    pwsh7   - an installed PowerShell 7's module directory first, as a pwsh 7 session leaves it to
              a program it starts (collect_logs.bat, a test runner); skipped without PowerShell 7.
    """
    env = {k: v for k, v in (os.environ if env is None else env).items() if k.upper() != "PSMODULEPATH"}
    if case == "own":
        return env
    first = _foreign_modules(root / "foreign-modules") if case == "foreign" else _pwsh7_modules()
    if first is None:
        pytest.skip("no PowerShell 7 (pwsh) on PATH")
    env["PSModulePath"] = f"{first};{os.environ.get('PSModulePath', '')}"
    return env

ADDIN = "TestAddin"
OTHER_USER = "someone-else"


def collect_logs_fixture(root: Path) -> dict:
    """A user's machine in miniature under ``root``: a profile whose path holds Chinese
    characters and a space, a BOM-less UTF-8 manifest pointing at a DLL in it, another
    user's manifest, and two same-named add-in logs in different folders."""
    profile = root / "\u5f20\u4e09 profile"                         # a Chinese user name
    roaming, local = profile / "AppData" / "Roaming", profile / "AppData" / "Local"
    dll = roaming / "Autodesk" / "Revit" / "Addins" / "2026" / ADDIN / f"{ADDIN}.dll"
    dll.parent.mkdir(parents=True)
    dll.write_bytes(b"MZ not a real assembly")
    manifest = dll.parent.parent / f"{ADDIN}.addin"
    manifest.write_bytes(('<?xml version="1.0" encoding="utf-8"?>\n<RevitAddIns>\n  <AddIn Type="Application">\n'
                          f"    <Name>{ADDIN}</Name>\n    <Assembly>{dll}</Assembly>\n"
                          "    <AddInId>0F6E2A1C-5B7D-4E8F-9A0B-1C2D3E4F5A6B</AddInId>\n"
                          "  </AddIn>\n</RevitAddIns>\n").encode("utf-8"))
    bundle = roaming / "Autodesk" / "ApplicationPlugins" / f"{ADDIN}.bundle"
    bundle.mkdir(parents=True)
    (bundle / "PackageContents.xml").write_bytes(
        ('<?xml version="1.0" encoding="utf-8"?>\n<ApplicationPackage AppVersion="1.2.3" '
         'Description="\u5899\u4f53\u68c0\u67e5"/>\n').encode("utf-8"))
    other = root / "sys" / "Users" / OTHER_USER / "AppData" / "Roaming" / "Autodesk" / "Revit" / "Addins" / "2026"
    other.mkdir(parents=True)
    (other / f"{ADDIN}.addin").write_text("<RevitAddIns/>", encoding="utf-8")
    data = local / ADDIN
    for folder, text in (("log", "new log"), ("old", "old log")):
        (data / folder).mkdir(parents=True)
        (data / folder / "app.log").write_text(text, encoding="utf-8")
    locked = data / "log" / "locked.log"
    locked.write_text("held", encoding="utf-8")
    for name in ("pd", "pf", "temp", "out"):
        (root / name).mkdir()
    env = {**os.environ, "APPDATA": str(roaming), "LOCALAPPDATA": str(local), "ProgramData": str(root / "pd"),
           "ProgramFiles": str(root / "pf"), "SystemDrive": str(root / "sys"), "TEMP": str(root / "temp"),
           "TMP": str(root / "temp")}
    return {"env": env, "dll": dll, "locked": locked, "out": root / "out"}


def run_collect_logs(script: Path, root: Path, modules: str = "own") -> tuple[str, dict[str, bytes], str]:
    """Run ``script`` on a fresh fixture with ``locked.log`` byte-range locked (a file another
    process holds) and the module path of case ``modules`` (``module_path_env``):
    ``(report.txt, {zip path: bytes} of the copied files, stdout)``."""
    import msvcrt

    fixture = collect_logs_fixture(root)
    env = module_path_env(modules, root, fixture["env"])
    with open(fixture["locked"], "r+b") as handle:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 4)
        try:
            run = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                  "-Name", ADDIN, "-OutDir", str(fixture["out"]), "-NoExplorer"],
                                 env=env, capture_output=True, timeout=300)
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 4)
    stdout = run.stdout.decode("utf-8", "replace")
    zips = list(fixture["out"].glob("*.zip"))
    assert len(zips) == 1, stdout + run.stderr.decode("utf-8", "replace")
    with zipfile.ZipFile(zips[0]) as archive:
        entries = {name.replace("\\", "/"): name for name in archive.namelist()}
        report = archive.read(entries["report.txt"]).decode("utf-8-sig")
        copies = {name: archive.read(entry) for name, entry in entries.items()
                  if name.startswith("files/") and not name.endswith("/")}
    return report, copies, stdout


# -- journal_tail.py ------------------------------------------------------------------------

JOURNAL = ("' Build: 20250101_1515(x64)\n"
           "' 0:< \u6d4b\u8bd5 TaskDialog \u663e\u793a\u4e2d\u6587\n"
           'Jrn.Command "Ribbon" , "\u6253\u5f00\u6587\u4ef6"\n'
           "' 0:< still writing \u672b\u5c3e")


@pytest.mark.parametrize("codec", ["gbk", "utf-8"])
def test_journal_tail_reads_a_journal_cut_mid_character(tmp_path, codec):
    """B4: a live journal (a hung Revit) ends mid-character; that one cut character must not
    send the whole file to cp1252/latin-1 junk."""
    journal = tmp_path / "journal.0001.txt"
    journal.write_bytes(JOURNAL.encode(codec)[:-1])                  # the last character is cut
    run = subprocess.run([*PYTHON_CHILD, str(JOURNAL_TAIL), "2026", "--all", "--file", str(journal)],
                         env=child_env(tmp_path), capture_output=True, timeout=60)
    assert run.returncode == 0, run.stderr
    out = run.stdout.decode("utf-8")
    assert "\u6d4b\u8bd5 TaskDialog \u663e\u793a\u4e2d\u6587" in out, out
    assert 'Jrn.Command "Ribbon" , "\u6253\u5f00\u6587\u4ef6"' in out
    assert "still writing \u672b" in out


def test_journal_tail_keeps_reading_utf16_and_whole_files(tmp_path):
    for codec, data in (("utf-16", JOURNAL.encode("utf-16")), ("gbk", JOURNAL.encode("gbk")),
                        ("cp1252", "Jrn.Data caf\xe9 TaskDialog\n".encode("cp1252"))):
        journal = tmp_path / f"journal.{codec}.txt"
        journal.write_bytes(data)
        run = subprocess.run([*PYTHON_CHILD, str(JOURNAL_TAIL), "2026", "--all", "--file", str(journal)],
                             env=child_env(tmp_path), capture_output=True, timeout=60)
        out = run.stdout.decode("utf-8")
        expected = "caf\xe9 TaskDialog" if codec == "cp1252" else "still writing \u672b\u5c3e"
        assert run.returncode == 0 and expected in out, (codec, out)


# -- addin_snapshot.py ------------------------------------------------------------------------

SNAPSHOT = SKILLS / "revit-addin-test" / "scripts" / "addin_snapshot.py"


def test_addin_snapshot_never_keeps_an_incomplete_backup(tmp_path, capsys, monkeypatch):
    """B8: copytree created the backup folder and raised at the end when one file failed (a DLL
    Revit holds); the next snapshot saw the folder, kept it as "the first one", and
    hashes-before.txt was never written. Now the copy is made aside and renamed when whole."""
    import runpy

    main = runpy.run_path(str(SNAPSHOT))["main"]              # capsys holds the stdout it reconfigures
    install, backup = tmp_path / "MyAddin", tmp_path / "backup"
    (install / "sub").mkdir(parents=True)
    for rel in ("MyAddin.dll", "Locked.dll", "sub/settings.json"):
        (install / rel).write_text(rel, encoding="utf-8")
    real_copytree = shutil.copytree

    def revit_holds_one_dll(src, dst, *args, **kwargs):
        if args:                                              # shutil's own recursion into a subfolder
            return real_copytree(src, dst, *args, **kwargs)

        def copy(a, b, **kw):
            if Path(a).name == "Locked.dll":
                raise PermissionError(13, "used by another process", a)
            return shutil.copy2(a, b, **kw)
        return real_copytree(src, dst, copy_function=copy, **kwargs)

    argv = ["addin_snapshot.py", "snapshot", str(install), str(backup)]
    monkeypatch.setattr(shutil, "copytree", revit_holds_one_dll)
    try:
        code = main(argv)
    except (OSError, SystemExit) as exc:
        code = exc
    assert not (backup / "MyAddin").exists(), "an incomplete backup was kept"
    assert code == 1 and "snapshot failed" in capsys.readouterr().out
    assert not (backup / "hashes-before.txt").exists()
    monkeypatch.undo()

    assert main(argv) == 0
    assert "backed up 3 files" in capsys.readouterr().out
    hashes = (backup / "hashes-before.txt").read_text(encoding="utf-8").splitlines()
    assert sorted(line.split("  ")[1] for line in hashes) == ["Locked.dll", "MyAddin.dll", "sub/settings.json"]
    assert sorted(p.name for p in backup.iterdir()) == ["MyAddin", "hashes-before.txt"]
    assert main(["addin_snapshot.py", "verify", str(install), str(backup)]) == 0

    # a backup left incomplete by the old script (no hashes-before.txt) is not taken for a whole one
    (backup / "hashes-before.txt").unlink()
    assert main(argv) == 1
    assert "incomplete" in capsys.readouterr().out


def section(report: str, title: str) -> str:
    return report.split(f"=== {title} ===", 1)[1].split("\n=== ", 1)[0]


@needs_windows_powershell
@pytest.mark.parametrize("modules", MODULE_PATHS)
def test_collect_logs_on_a_chinese_profile(tmp_path, modules):
    """A4: the manifest is read as the XML says (UTF-8), so the DLL in a Chinese profile is found;
    the other profile is counted; a copy that fails says so; same-named logs are both kept.
    The same report whatever module path Windows PowerShell inherits: started from a PowerShell 7
    session through another program, Get-FileHash went missing (the manifest became a parse
    error) and Compress-Archive could too (no zip at all)."""
    report, copies, _ = run_collect_logs(COLLECT_LOGS, tmp_path, modules)
    for _, _, command in ON_DEMAND:
        assert command not in report, report                   # an error names the command in any language
    fixture_dll = next(tmp_path.rglob(f"{ADDIN}.dll"))
    manifests = section(report, "classic manifests (.addin)")
    assert f"dll {fixture_dll}" in manifests and "MISSING" not in manifests, manifests
    assert "[error]" not in section(report, "Revit running")
    assert "AddInId 0F6E2A1C-5B7D-4E8F-9A0B-1C2D3E4F5A6B" in manifests

    assert "    AppVersion 1.2.3" in section(report, "autoloader bundles")

    others = section(report, "manifests in other user profiles (not copied)")
    assert OTHER_USER in others and "(none readable)" not in others, others

    data = section(report, "add-in data folders")
    locked = next(tmp_path.rglob("locked.log"))
    assert f"copy FAILED {locked}" in data and f"copied {locked}" not in data, data
    logs = {name: content for name, content in copies.items() if name.startswith(f"files/addin-data/{ADDIN}/")}
    assert sorted(logs.values()) == [b"new log", b"old log"], sorted(logs)
    for name in logs:                                          # the report names each copy as it is in the zip
        assert "-> files\\" + name[len("files/"):].replace("/", "\\") in data, (name, data)


@needs_windows_powershell
@pytest.mark.parametrize("modules", MODULE_PATHS)
def test_revit_ps_lists_revit_whatever_module_path_it_inherits(tmp_path, modules):
    """Get-CimInstance (CimCmdlets, loaded on demand) went missing under a foreign module path and
    the script then said "no Revit running" whatever ran. It only lists processes; none is touched."""
    run = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(REVIT_PS)],
                         env=module_path_env(modules, tmp_path), capture_output=True, timeout=120)
    out, err = run.stdout.decode("utf-8", "replace"), run.stderr.decode("utf-8", "replace")
    assert run.returncode == 0 and "Get-CimInstance" not in out + err, err
    assert out.startswith(("no Revit running", "pid=")), out
