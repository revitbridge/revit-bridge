r"""Snapshot an installed add-in directory before a test session and verify it afterwards.

  python addin_snapshot.py snapshot <install dir> <backup dir>
      Copies <install dir> into <backup dir>/<its name> (once; an existing backup is kept)
      and writes <backup dir>/hashes-before.txt with a SHA-256 per file. The copy is made
      aside and renamed when complete; a file that cannot be copied (Revit holding it)
      fails the snapshot (exit 1) and keeps nothing.
  python addin_snapshot.py verify <install dir> <backup dir> [--ignore <glob> ...]
      Compares the directory with the snapshot: changed, added and missing files.
      --ignore excludes paths (relative, forward slashes) such as "Logs/*" that the
      user's own Revit sessions may legitimately change. Exit 0 when nothing outside the
      ignored paths changed and nothing is missing; 1 otherwise.

A machine-level classic install has two directories, the DLL folder (such as
%ProgramData%\<Name>) and the manifest folder (Addins\<year>): snapshot and verify
each with its own backup directory.

Read-only apart from writing the backup. Restoring is deliberate and manual: reinstall
the base build, then run verify.
"""
import fnmatch
import hashlib
import os
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def listing(root):
    return {p.relative_to(root).as_posix(): sha(p) for p in sorted(root.rglob("*")) if p.is_file()}


def main(argv):
    if len(argv) < 4 or argv[1] not in ("snapshot", "verify"):
        sys.exit(__doc__)
    mode, install, backup = argv[1], Path(argv[2]), Path(argv[3])
    if not install.is_dir():
        sys.exit("not a directory: %s" % install)
    copy = backup / install.name
    if mode == "snapshot":
        hashes = backup / "hashes-before.txt"
        if copy.exists():
            if not hashes.exists():
                print("incomplete backup at %s (no %s): an earlier snapshot failed; "
                      "delete it and snapshot again" % (copy, hashes.name))
                return 1
            print("backup already exists at %s (keeping the first one)" % copy)
            return 0
        # The copy is made aside and renamed only when whole, after its hashes are written:
        # a folder named like the install directory is always a complete backup.
        backup.mkdir(parents=True, exist_ok=True)
        partial = backup / (install.name + ".partial")
        if partial.exists():
            shutil.rmtree(partial)  # left by a snapshot that failed
        try:
            shutil.copytree(install, partial)
        except (OSError, shutil.Error) as error:
            shutil.rmtree(partial, ignore_errors=True)
            failed = error.args[0] if isinstance(error, shutil.Error) and error.args else None
            if isinstance(failed, list) and failed:
                error = "%d file(s) not copied, first %s: %s" % (len(failed), failed[0][0], failed[0][-1])
            print("snapshot failed, no backup kept (close what holds the files and run it again): %s" % error)
            return 1
        before = listing(partial)
        with open(backup / "hashes-before.txt.partial", "w", encoding="utf-8") as f:
            for rel, digest in before.items():
                f.write("%s  %s\n" % (digest, rel))
        os.replace(backup / "hashes-before.txt.partial", hashes)
        partial.rename(copy)
        print("backed up %d files to %s" % (len(before), copy))
        return 0
    ignore = []
    if "--ignore" in argv:
        ignore = argv[argv.index("--ignore") + 1:]
    if not copy.is_dir():
        sys.exit("no snapshot at %s" % copy)
    before, now = listing(copy), listing(install)

    def ignored(rel):
        return any(fnmatch.fnmatch(rel, pat) for pat in ignore)

    changed = sorted(r for r in before if r in now and before[r] != now[r] and not ignored(r))
    added = sorted(r for r in now if r not in before and not ignored(r))
    missing = sorted(r for r in before if r not in now)
    skipped = sorted(r for r in set(before) | set(now) if ignored(r) and before.get(r) != now.get(r))
    for label, items in (("changed", changed), ("added", added), ("missing", missing),
                         ("ignored but different", skipped)):
        print("%s: %s" % (label, ", ".join(items) if items else "none"))
    ok = not (changed or added or missing)
    print("RESTORED" if ok else "NOT RESTORED - see above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
