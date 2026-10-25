#!/usr/bin/env python3
"""Create a standalone Revit command project without building or installing it."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from uuid import uuid4


FRAMEWORKS = {"2024": "net48", "2025": "net8.0-windows", "2025.5": "net10.0-windows",
              "2026": "net8.0-windows", "2026.5": "net10.0-windows", "2027": "net10.0-windows"}
ASSETS = Path(__file__).resolve().parents[1] / "assets" / "addin"
# Optional template blocks: <!--name--> ... <!--/name-->, kept or dropped per release.
SECTION = re.compile(r"[ \t]*<!--(?P<name>[a-z0-9-]+)-->\n(?P<body>.*?)[ \t]*<!--/(?P=name)-->\n", re.S)
SECTIONS = {"net8-split", "net10-only"}


def _sections(content: str, keep: set[str]) -> str:
    def choose(match: re.Match) -> str:
        if match["name"] not in SECTIONS:
            raise ValueError(f"unknown template section {match['name']}")
        return match["body"] if match["name"] in keep else ""
    return SECTION.sub(choose, content)


def create_project(output: Path, name: str, revit_version: int | str, vendor_id: str) -> dict:
    if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", name):
        raise ValueError("name must start with an uppercase ASCII letter and contain only letters/digits")
    if name.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                       *(f"LPT{i}" for i in range(1, 10))}:
        raise ValueError("name is reserved on Windows")
    release = str(revit_version)
    if release not in FRAMEWORKS:
        raise ValueError("supported Revit versions: " + ", ".join(FRAMEWORKS))
    year = int(release.split(".")[0])
    # 2025/2026 have a .NET 8 release and a .NET 10 .5 update of the same API year;
    # a year whose base release already targets .NET 10 (2027) has no .NET 8 build.
    net10_only = FRAMEWORKS[str(year)] == "net10.0-windows"
    keep = {"net10-only"} if net10_only else {"net8-split"}
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", vendor_id):
        raise ValueError("vendor-id must be 1-64 ASCII letters/digits, starting with a letter")

    addin_id = str(uuid4()).upper()
    substitutions = {
        "__PROJECT_NAME__": name,
        "__REVIT_VERSION__": str(year),
        "__REVIT_RELEASE__": release,
        "__API_MAJOR__": str(year - 2000),
        "__NEXT_API_MAJOR__": str(year - 1999),
        "__TARGET_FRAMEWORK__": FRAMEWORKS[release],
        "__VENDOR_ID__": vendor_id,
        "__ADDIN_ID__": addin_id,
    }
    files = {}
    for template, destination in (
        ("Addin.csproj.template", f"{name}.csproj"),
        ("Command.cs.template", "Command.cs"),
        ("Addin.addin.template", f"{name}.addin"),
    ):
        content = _sections((ASSETS / template).read_text(encoding="utf-8"), keep)
        for marker, value in substitutions.items():
            content = content.replace(marker, value)
        if re.search(r"__[A-Z_]+__|<!--/?[a-z0-9-]+-->", content):
            raise ValueError(f"unresolved template marker in {template}")
        files[destination] = content
    files[".gitignore"] = "bin/\nobj/\n.vs/\n*.user\n*.suo\n"

    # Claim a new directory exclusively. Never merge with an existing project,
    # empty directory, symlink, or manifest, and never overwrite existing files.
    output = output.expanduser().absolute()
    output.mkdir(parents=True, exist_ok=False)
    for relative, content in files.items():
        with (output / relative).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    return {"project": str(output / f"{name}.csproj"), "revit_version": year,
            "revit_release": release, "target_framework": FRAMEWORKS[release], "addin_id": addin_id,
            "files": sorted(files), "installed": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="C# project/namespace, e.g. WallInspector")
    parser.add_argument("--revit-version", choices=sorted(FRAMEWORKS), required=True,
                        help="Use 2025.5/2026.5 for .NET 10; bare 2025/2026 select .NET 8; "
                             "2027 is .NET 10 only")
    parser.add_argument("--vendor-id", required=True, help="Your developer/vendor identity")
    parser.add_argument("--output", type=Path, required=True, help="New project directory (must not exist)")
    args = parser.parse_args()
    try:
        result = create_project(args.output, args.name, args.revit_version, args.vendor_id)
    except (OSError, ValueError) as error:
        parser.exit(2, f"Cannot create add-in: {error}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
