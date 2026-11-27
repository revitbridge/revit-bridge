"""Inspect official ADIX library payloads and verify their installed file sizes."""
import argparse
import json
import re
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote
from zipfile import ZipFile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("year")
    parser.add_argument("--verify-root", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"20[0-9]{2}", args.year):
        parser.error("expected a four-digit Revit release year")
    prefix = f"VFS/Common AppData/Autodesk/RVT {args.year}/Libraries/"
    counts = {"Rfa": 0, "Other": 0, "Total": 0, "Bytes": 0}
    groups = {}
    missing = []
    mismatches = []
    with ZipFile(args.archive) as source:
        for entry in source.infolist():
            name = unquote(entry.filename).replace("\\", "/")
            if entry.is_dir() or name.endswith("/") or not name.startswith(prefix):
                continue
            relative = PureWindowsPath(name[len(prefix):])
            if relative.anchor or ".." in relative.parts or ":" in str(relative):
                raise ValueError("Unexpected library path in content payload")
            counts["Total"] += 1
            counts["Bytes"] += entry.file_size
            is_family = relative.suffix.lower() == ".rfa"
            counts["Rfa" if is_family else "Other"] += 1
            group = "/".join(relative.parts[:2])
            groups[group] = groups.get(group, 0) + int(is_family)
            if args.verify_root:
                target = args.verify_root / "Libraries" / Path(*relative.parts)
                if not target.is_file():
                    missing.append(str(relative))
                elif target.stat().st_size != entry.file_size:
                    mismatches.append(str(relative))
    if not counts["Rfa"]:
        raise ValueError("ADIX payload does not contain the expected year library paths")
    result = {
        "AdixPath": str(args.archive.resolve()),
        "Year": args.year,
        "FileCounts": counts,
        "LibraryRfaCounts": groups,
        "VerifiedRoot": str(args.verify_root) if args.verify_root else None,
        "MissingCount": len(missing),
        "SizeMismatchCount": len(mismatches),
        "MissingExamples": missing[:5],
        "SizeMismatchExamples": mismatches[:5],
    }
    print(json.dumps(result, ensure_ascii=True, indent=2))
    if missing or mismatches:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
