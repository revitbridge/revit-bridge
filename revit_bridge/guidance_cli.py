"""``revit-bridge guidance check|index|explain``: the guidance layers from the command line.

    check [DIR ...]   validate guidance; without DIR the four layers the server
                      loads (the packaged framework, REVIT_BRIDGE_GUIDANCE_COMPANY,
                      REVIT_BRIDGE_GUIDANCE_PROJECT, <data root>/guidance); with
                      DIRs, the first is the framework and the next ones the
                      company, project and personal layers. Prints every problem
                      and warning; exit 1 on a problem, 0 with warnings only.
    index [DIR] [--check]
                      rebuild the generated parts of a guidance root (default:
                      the packaged framework): the "## Lessons" section of each
                      category SKILL.md; --check only compares (exit 1 when
                      something is out of date).
    explain --stage S [--code FILE] [--errors CS...] [--symbols ...]
            [--categories OST_...] [--exceptions ...] [--revit 2026] [--inline]
                      what get_guidance(mode="recommend") would return for these
                      signals - ids, order, why, bytes, omitted - without a model
                      or Revit. --code adds the API names scanned from the file
                      (check_code's scanner); error codes pair only with
                      --symbols, as check_code pairs them with the diagnostics'
                      symbols.

The environment is read here, once, and passed to the loader. Output is
plain text: ids, paths, titles and reasons, never a lesson body; a character
the console cannot show is written as a backslash escape.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from revit_bridge import guidance
from revit_bridge.guidance import indexer
from revit_bridge.guidance.model import LAYERS, STAGES
from revit_bridge.paths import guidance_dir, guidance_layer_dirs


def add_parser(sub) -> None:
    """Register ``guidance`` and its subcommands on the main parser's subparsers."""
    parser = sub.add_parser("guidance", help="check, index or explain the guidance lessons",
                            description="Check, index or explain the guidance lessons (no model, no Revit).")
    commands = parser.add_subparsers(dest="guidance_command", metavar="{check,index,explain}")
    commands.required = True

    check = commands.add_parser("check", help="validate the guidance layers; exit 1 on a problem")
    check.add_argument("dirs", nargs="*", metavar="DIR",
                       help="framework, then company, project, personal (default: the configured layers)")

    index = commands.add_parser("index", help="rebuild the generated parts of a guidance root")
    index.add_argument("dir", nargs="?", metavar="DIR", help="guidance root (default: the packaged framework)")
    index.add_argument("--check", action="store_true", help="only compare; exit 1 when out of date")

    explain = commands.add_parser("explain", help="what get_guidance would recommend for these signals")
    explain.add_argument("--stage", required=True, choices=STAGES)
    explain.add_argument("--code", metavar="FILE", help="C# file whose API names join the symbols")
    for name, help_text in (("--errors", "compiler error ids such as CS1061"),
                            ("--symbols", "API names (the ones error ids pair with)"),
                            ("--categories", "OST_* categories"),
                            ("--exceptions", "runtime exception type names")):
        explain.add_argument(name, nargs="+", action="extend", default=[], metavar="NAME", help=help_text)
    explain.add_argument("--revit", metavar="VERSION", help="Revit version such as 2026 or 2026.5")
    explain.add_argument("--inline", action="store_true", help="check_code's inline budget for a compile error")


def _safe_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass


def _say(line: str = "") -> None:
    print(line)


def run(args: argparse.Namespace) -> int:
    """Run ``revit-bridge guidance <subcommand>``; returns the exit code."""
    _safe_streams()
    if args.guidance_command == "check":
        return _check(args.dirs)
    if args.guidance_command == "index":
        return _index(Path(args.dir) if args.dir else guidance_dir(), args.check)
    return _explain(args)


# -- check ---------------------------------------------------------------------------

def _layer_line(layer: guidance.Layer) -> str:
    if layer.state == "unset":
        return f"layer {layer.name}: not set"
    if layer.state == "missing":
        return f"layer {layer.name}: missing, skipped: {layer.root}"
    if layer.state == "invalid":
        return f"layer {layer.name}: invalid ({len(layer.problems)} problems): {layer.root}"
    return f"layer {layer.name}: loaded, {layer.lessons} lessons: {layer.root}"


def _check(dirs: list[str]) -> int:
    if len(dirs) > len(LAYERS):
        _say(f"problem: at most {len(LAYERS)} directories: {', '.join(LAYERS)}")
        return 2
    if dirs:
        framework = Path(dirs[0])
        uppers = dict(zip(LAYERS[1:], (Path(d) for d in dirs[1:])))
        problems = [f"{name}: {path}: not a directory" for name, path in uppers.items() if not path.is_dir()]
    else:
        framework, uppers, problems = guidance_dir(), guidance_layer_dirs(), []
    try:
        library = guidance.load_layers(framework, **uppers)
    except guidance.GuidanceError as e:
        _say(f"layer framework: invalid ({len(e.problems)} problems): {framework}")
        for problem in e.problems:
            _say(f"problem: framework: {problem}")
        _say(f"result: {len(e.problems)} problems")
        return 1
    for layer in library.layers:
        _say(_layer_line(layer))
        problems.extend(f"{layer.name}: {p}" for p in layer.problems)
    for problem in problems:
        _say(f"problem: {problem}")
    for warning in library.warnings:
        _say(f"warning: {warning}")
    _say(f"result: {len(problems)} problems, {len(library.warnings)} warnings, {len(library.lessons)} lessons")
    return 1 if problems else 0


# -- index ---------------------------------------------------------------------------

def _index(root: Path, check_only: bool) -> int:
    changes, problems = indexer.plan_index(root)
    if problems:
        for problem in problems:
            _say(f"problem: {problem}")
        _say(f"result: {len(problems)} problems; nothing written")
        return 1
    if check_only:
        for change in changes:
            state = "to be removed" if change.content is None else "out of date"
            _say(f"{state}: {change.path} ({change.what})")
        _say(f"result: {len(changes)} out of date" if changes else f"result: up to date: {root}")
        return 1 if changes else 0
    indexer.apply(changes)
    for change in changes:
        _say(f"{'removed' if change.content is None else 'wrote'}: {change.path} ({change.what})")
    _say(f"result: {len(changes)} written" if changes else f"result: up to date: {root}")
    return 0


# -- explain -------------------------------------------------------------------------

def _explain(args: argparse.Namespace) -> int:
    code_names: list[str] = []
    if args.code:
        try:
            code = Path(args.code).read_text(encoding="utf-8-sig", errors="replace")
        except OSError as e:
            _say(f"problem: --code {args.code}: {e.strerror or e}")
            return 2
        code_names = [name for name in guidance.code_symbols(code) if name not in args.symbols]
    try:
        library = guidance.load_layers(guidance_dir(), **guidance_layer_dirs())
    except guidance.GuidanceError as e:
        for problem in e.problems:
            _say(f"problem: framework: {problem}")
        return 1
    out = guidance.recommend(
        library, args.stage, revit_categories=args.categories, symbols=list(args.symbols) + code_names,
        errors=args.errors, exceptions=args.exceptions, revit_version=args.revit, inline=args.inline,
        error_symbols=list(args.symbols))
    _say(f"stage: {args.stage}; revit: {args.revit or 'not given'}; inline: {'yes' if args.inline else 'no'}")
    _say("layers: " + ", ".join(f"{x.name} {x.state}" for x in library.layers))
    _say(f"symbols: {', '.join(args.symbols) or '-'}; from the code: {', '.join(code_names) or '-'}")
    budget = out["budget"]
    _say(f"budget: {budget['items']} items, {budget['utf8_bytes']} bytes; used {budget['used_bytes']} bytes")
    for number, item in enumerate(out["items"], 1):
        cut = ", truncated" if item["truncated"] else ""
        _say(f"{number}. {item['id']} ({item['layer']}, revision {item['revision']}, {item['hash']}, "
             f"{item['bytes']} bytes{cut}, {item['applicability']})")
        _say(f"   title: {item['title']}")
        _say(f"   why: {', '.join(item['why'])}")
    if not out["items"]:
        _say("no lessons")
    for item in out["omitted"]:
        _say(f"omitted: {item['id']} ({item['reason']})")
    for warning in out["warnings"]:
        _say(f"warning: {warning}")
    return 0
