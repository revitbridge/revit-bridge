"""The shipped helper scripts of the add-in test and release skills, run as a user runs them.

collect_logs.ps1 runs under Windows PowerShell 5.1 (skipped where there is none); the
Python scripts run in a child process with the code page set explicitly, so the result does
not depend on the machine running the tests.
"""
from __future__ import annotations

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


def child_env(tmp_path: Path, **extra: str) -> dict:
    """A child Python in a GBK locale whatever the host: no UTF-8 mode, no PYTHONIOENCODING."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    env.update({"PYTHONUTF8": "0", "LOCALAPPDATA": str(tmp_path / "local"), **extra})
    return env

POWERSHELL = shutil.which("powershell") if sys.platform == "win32" else None
needs_windows_powershell = pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell 5.1 is not available")

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


def run_collect_logs(script: Path, root: Path) -> tuple[str, dict[str, bytes], str]:
    """Run ``script`` on a fresh fixture with ``locked.log`` byte-range locked (a file another
    process holds): ``(report.txt, {zip path: bytes} of the copied files, stdout)``."""
    import msvcrt

    fixture = collect_logs_fixture(root)
    with open(fixture["locked"], "r+b") as handle:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 4)
        try:
            run = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                  "-Name", ADDIN, "-OutDir", str(fixture["out"]), "-NoExplorer"],
                                 env=fixture["env"], capture_output=True, timeout=300)
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
    run = subprocess.run([sys.executable, str(JOURNAL_TAIL), "2026", "--all", "--file", str(journal)],
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
        run = subprocess.run([sys.executable, str(JOURNAL_TAIL), "2026", "--all", "--file", str(journal)],
                             env=child_env(tmp_path), capture_output=True, timeout=60)
        out = run.stdout.decode("utf-8")
        expected = "caf\xe9 TaskDialog" if codec == "cp1252" else "still writing \u672b\u5c3e"
        assert run.returncode == 0 and expected in out, (codec, out)


def section(report: str, title: str) -> str:
    return report.split(f"=== {title} ===", 1)[1].split("\n=== ", 1)[0]


@needs_windows_powershell
def test_collect_logs_on_a_chinese_profile(tmp_path):
    """A4: the manifest is read as the XML says (UTF-8), so the DLL in a Chinese profile is found;
    the other profile is counted; a copy that fails says so; same-named logs are both kept."""
    report, copies, _ = run_collect_logs(COLLECT_LOGS, tmp_path)
    fixture_dll = next(tmp_path.rglob(f"{ADDIN}.dll"))
    manifests = section(report, "classic manifests (.addin)")
    assert f"dll {fixture_dll}" in manifests and "MISSING" not in manifests, manifests
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
