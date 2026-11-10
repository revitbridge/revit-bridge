"""Shows what Revit was doing: decodes the newest journal of a Revit year (a localized
Revit may write it in GBK or another ANSI code page, not UTF-16) and prints the lines
that matter when a test hangs or fails - TaskDialogs (modal dialogs), transactions,
ExternalEvent runs, background (NOBLE) work, add-in loading, errors - plus whether each
RevitWorker log of that journal says "Worker accepting requests". Read-only.

Usage: python journal_tail.py <year> [--lines N] [--all] [--file <journal.txt>]
  --lines N  how many matching lines from the end (default 60)
  --all      print the last N lines unfiltered instead
  --file F   read this journal instead of the newest one
"""
import argparse
import os
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ap = argparse.ArgumentParser()
ap.add_argument("year")
ap.add_argument("--lines", type=int, default=60)
ap.add_argument("--all", action="store_true")
ap.add_argument("--file")
args = ap.parse_args()

folder = Path(os.environ["LOCALAPPDATA"]) / "Autodesk" / "Revit" / ("Autodesk Revit %s" % args.year) / "Journals"
if args.file:
    journal = Path(args.file)
else:
    journals = sorted(folder.glob("journal.*.txt"), key=lambda p: p.stat().st_mtime)
    if not journals:
        sys.exit("no journal in %s" % folder)
    journal = journals[-1]


def decode_cut(raw, codec):
    """raw in codec, strictly - except one character cut off at the very end, as in a journal
    a hung Revit is still writing, which becomes U+FFFD. None when raw is not in codec."""
    try:
        return raw.decode(codec)
    except UnicodeDecodeError as error:
        if error.end != len(raw) or len(raw) - error.start > 3:
            return None  # a bad byte before the end: another encoding
        try:
            return raw[:error.start].decode(codec) + chr(0xFFFD)
        except UnicodeDecodeError:
            return None


def read(path):
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    for codec in ("utf-8", "gbk", "cp1252"):
        text = decode_cut(raw, codec)
        if text is not None:
            return text
    return raw.decode("latin-1", errors="replace")


lines = read(journal).splitlines()
print("journal: %s (%d lines)" % (journal, len(lines)))
KEEP = re.compile(r"TaskDialog|Id : TaskDialog_|Transaction|ExternalEvent|NOBLE|Wait mode|Error|Exception|"
                  r"Jrn\.Data|Jrn\.Command|DialogBox|Export with|Document\.Export|Starting External Application|Added pushbutton|"
                  r"Duplicate addins|AddInLoadFailureMessage|won't be loaded", re.I)
picked = lines if args.all else [l for l in lines if KEEP.search(l)]
for line in picked[-args.lines:]:
    print(line.rstrip()[:300])
workers = sorted(folder.glob(journal.stem + ".worker*.log"))
for w in workers:
    text = read(w)
    last = text.strip().splitlines()[-1][:200] if text.strip() else "(empty)"
    print("%s: %s; last: %s" % (w.name, "accepting" if "Worker accepting requests" in text else "NOT accepting", last))
