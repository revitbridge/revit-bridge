"""``revit-bridge skills [list|install]``: the bundled agent skills, from the command line.

    list                      the skills shipped with this package: name and description.
    install [NAME ...] [--profile addin|bridge|all] --to DIR [--force]
                              copy complete skill folders (SKILL.md, references, scripts,
                              assets) into DIR/<name>, e.g. a project's .claude/skills,
                              ~/.claude/skills or .agents/skills. Without NAME or
                              --profile every skill is copied. --profile addin is
                              the four add-in phases, revit-task-brief +
                              revit-addin-dev + revit-addin-test + revit-addin-release
                              (a plain add-in repository without the bridge server);
                              --profile bridge is revit-bridge + revit-core. Names and
                              a profile add up; folders are copied in bundled
                              (alphabetical) order. An existing DIR/<name> is left
                              alone unless --force replaces it; a folder is copied in
                              full next to its destination first and then swapped in,
                              so a failed copy leaves the old one as it was. Exit 2 on
                              an unknown name or a DIR that is not a directory (or
                              cannot be created), 1 when a destination exists and
                              --force was not given or a copy failed.

The skills are self-contained folders; nothing here starts a server or needs Revit.
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from revit_bridge import cli_output
from revit_bridge.guidance.loader import read_skill
from revit_bridge.paths import skills_dir

PROFILES: dict[str, list[str]] = {
    # An add-in repository that does not talk to the bridge server: the two server
    # skills would only cost context there. Listed in phase order (brief, coding,
    # test, release); install copies in bundled order.
    "addin": ["revit-task-brief", "revit-addin-dev", "revit-addin-test", "revit-addin-release"],
    # An agent that drives Revit through the server.
    "bridge": ["revit-bridge", "revit-core"],
}


def available() -> dict[str, Path]:
    """Skill name -> folder, for every folder under the packaged skills directory with a SKILL.md."""
    root = skills_dir()
    return {p.name: p for p in sorted(root.iterdir()) if p.is_dir() and (p / "SKILL.md").is_file()}


def describe(folder: Path) -> str:
    meta = read_skill(folder / "SKILL.md")
    text = str(meta[0].get("description", "")) if meta else ""
    return " ".join(text.split())


def add_parser(sub) -> None:
    """Register ``skills`` and its subcommands on the main parser's subparsers."""
    parser = sub.add_parser("skills", help="list or install the bundled agent skills",
                            description="List the bundled agent skills or copy them into a skills directory.")
    commands = parser.add_subparsers(dest="skills_command", metavar="{list,install}")
    commands.required = True

    commands.add_parser("list", help="the skills shipped with this package")

    install = commands.add_parser("install", help="copy skill folders into a skills directory")
    install.add_argument("names", nargs="*", metavar="NAME", help="skill names (default: all)")
    install.add_argument("--profile", choices=[*PROFILES, "all"],
                         help="a set of skills: addin = %s (the add-in phases brief, coding, test and "
                              "release, for an add-in repository without the server), "
                              "bridge = %s, all = every skill (the default without NAME)"
                              % (" + ".join(PROFILES["addin"]), " + ".join(PROFILES["bridge"])))
    install.add_argument("--to", required=True, metavar="DIR",
                         help="destination skills directory, e.g. .claude/skills or .agents/skills")
    install.add_argument("--force", action="store_true", help="replace a skill folder that already exists")


def _write(text: str) -> None:
    cli_output.write(text)


def run(args: argparse.Namespace) -> int:
    # The CLI output rule (revit_bridge.cli_output): hosts and pipes read UTF-8; the
    # locale code page (GBK on a Chinese Windows) would garble the Chinese half of a
    # description.
    cli_output.configure()
    skills = available()
    if args.skills_command == "list":
        width = max(len(n) for n in skills) if skills else 0
        for name, folder in skills.items():
            _write(f"{name.ljust(width)}  {describe(folder)}")
        return 0

    names = list(args.names)
    if args.profile:
        names += PROFILES.get(args.profile, list(skills))
    unknown = [n for n in names if n not in skills]
    if unknown:
        _write(f"unknown skill(s): {', '.join(unknown)}; available: {', '.join(skills)}")
        return 2
    wanted = set(names) or set(skills)
    names = [n for n in skills if n in wanted]          # bundled order, no duplicates
    target = Path(args.to).expanduser()
    if target.exists() and not target.is_dir():
        _write(f"--to {target}: not a directory")
        return 2
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        _write(f"--to {target}: cannot be created: {error}")
        return 2
    code = 0
    for name in names:
        dest = target / name
        previous = _previous(dest)
        if previous.exists() and not dest.exists():      # a swap that was interrupted: the old copy goes back
            try:
                previous.rename(dest)
            except OSError as error:
                _write(f"{name}: not installed; the previous copy {previous} could not be put back: {error}")
                code = 1
                continue
        if dest.exists() and not args.force:
            _write(f"{name}: {dest} exists; use --force to replace it")
            code = 1
            continue
        try:
            removed = _install(skills[name], dest)
        except OSError as error:                         # shutil.Error is one too
            _write(f"{name}: not installed, {dest} left as it was: {error}")
            code = 1
            continue
        files = sum(1 for p in dest.rglob("*") if p.is_file())
        _write(f"{name}: {files} files -> {dest}")
        if not removed:
            _write(f"{name}: the previous copy could not be removed; delete {_previous(dest)}")
    return code


def _staging(dest: Path) -> Path:
    return dest.with_name(f".{dest.name}.installing")


def _previous(dest: Path) -> Path:
    return dest.with_name(f".{dest.name}.previous")


def _install(source: Path, dest: Path) -> bool:
    """Copy the skill folder ``source`` to ``dest`` through a sibling: the copy is made in
    full first and then swapped in, so a failure (a locked file, a path too long) leaves
    ``dest`` as it was - the old copy, or nothing - and raises. On success the old copy is
    removed as before; returns False when it could not be (it stays as ``_previous(dest)``)."""
    staging, previous = _staging(dest), _previous(dest)
    for leftover in (staging, previous):                 # left by an earlier run that failed
        if leftover.exists():
            shutil.rmtree(leftover)
    try:
        shutil.copytree(source, staging, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        if dest.exists():
            dest.rename(previous)
            try:
                staging.rename(dest)
            except BaseException:
                previous.rename(dest)
                raise
        else:
            staging.rename(dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if previous.exists():
        shutil.rmtree(previous, ignore_errors=True)
    return not previous.exists()
